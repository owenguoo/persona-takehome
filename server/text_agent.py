"""The iMessage side.

Each user message goes: listener (extract.py) → flow plan (flow.py) → reply here.
A reply is the model's own words (only when the user said something that needs
a real answer) followed by the plan's beats: approved lines sent word for word,
each running its action (contact card, Gmail link, ringing) as it goes out.

Replies are debounced: if the user double-texts (or texts while Persona is still
"typing"), the pending reply is cancelled and one reply covers it all.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import openai
from loguru import logger
from openai import AsyncOpenAI

from . import config, extract, flow, mail, prompts
from .flow import Plan, tidy_name  # noqa: F401  (tidy_name is re-exported for other modules)
from .lines import LINES, render
from .session import Session

MAX_TOOL_ROUNDS = 4
MAX_BUBBLES = 3  # for one piece of the model's own text

# Only for answering questions about the inbox; everything else is the flow's job.
TOOLS = [
    {"type": "function", "function": {
        "name": "inbox_overview",
        "description": "Their connected inbox at a glance: counts, top senders, the 10 most recent emails.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_inbox",
        "description": "Read every email in their connected inbox in full. Use it for anything that needs "
                       "judgment across the inbox: to-dos, what's urgent, deadlines, who's waiting on them.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "search_inbox",
        "description": "Find emails mentioning specific words (a sender, a company, a topic). "
                       "Not for judgment questions like to-dos: use read_inbox for those.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_email",
        "description": "Read one email in full, by id.",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
]

_client: AsyncOpenAI | None = None
_client_key: str | None = None


def _openai(key: str) -> AsyncOpenAI:
    global _client, _client_key
    if _client is None or _client_key != key:
        _client, _client_key = AsyncOpenAI(api_key=key), key
    return _client


# ── entry points ────────────────────────────────────────────────
def schedule_reply(session: Session, delay: float = 0.9) -> None:
    """(Re)start Persona's reply to the user's latest message(s) after a short pause."""
    _schedule(session, _reply(session), delay)


def send_beats(session: Session, beats: list[str], answer: bool = False, note: str = "",
               complete: tuple[str, str | None] | None = None) -> None:
    """Send lines on Persona's own initiative (a call event, the name timeout, a stall)."""
    _schedule(session, _deliver(session, Plan(beats, answer, note, complete)), 0.4)


def schedule_line(session: Session, key: str, delay: float = 0.9) -> None:
    """Send an approved line directly (the opener).

    It can't be cancelled by the user texting: replies wait for it instead, so a
    fast "nova" never lands before "what do you want to call me?" has been asked.
    """
    async def run():
        await asyncio.sleep(delay)
        await _send(session, [(b, key) for b in render(key, session).split("\n") if b.strip()])
        await session.set_typing(False)
        flow.after_agent_turn(session)

    session.line_task = asyncio.create_task(run())


def _schedule(session: Session, work, delay: float) -> None:
    """Debounce: a newer message replaces a reply that's still being worked out. But a reply
    that has started sending is let finish (its questions must be on record), then this runs."""
    cur = session.reply_task
    if cur and not cur.done() and cur is not session.sending_task:
        cur.cancel()
    after = session.sending_task if session.sending_task and not session.sending_task.done() else None
    session.reply_task = asyncio.create_task(_run(session, work, delay, after))


async def _run(session: Session, work, delay: float, after: asyncio.Task | None = None) -> None:
    try:
        for earlier in (after, session.line_task):  # a reply mid-send, or the opener: let it finish
            if earlier and not earlier.done():
                try:
                    await asyncio.shield(earlier)
                except Exception:
                    pass
        await asyncio.sleep(delay)
        await work
    except asyncio.CancelledError:
        work.close()
    except Exception as e:
        logger.exception("reply failed")
        await session.add_message("system", "event", f"Agent error: {_describe(e)}")
    finally:
        if session.reply_task is asyncio.current_task():
            await session.set_typing(False)


def _describe(e: Exception) -> str:
    code = getattr(e, "code", None) or ""
    if isinstance(e, openai.AuthenticationError):
        return "OpenAI rejected the API key. Check OPENAI_API_KEY in .env"
    if isinstance(e, openai.NotFoundError) or code == "model_not_found":
        return f"model {config.settings().text_model} isn't available on this key. Set OPENAI_TEXT_MODEL"
    if isinstance(e, openai.RateLimitError):
        return "OpenAI rate limit or quota hit" + (" (out of credit)" if code == "insufficient_quota" else "")
    if isinstance(e, openai.APIConnectionError):
        return "couldn't reach OpenAI"
    return f"{type(e).__name__}: {e}"


# ── a reply ─────────────────────────────────────────────────────
async def _reply(session: Session) -> None:
    if not config.settings().openai_api_key:
        await session.add_notice("No OPENAI_API_KEY yet. Add it to .env and send another message.")
        return
    await session.mark_read()
    await session.set_typing(True)
    started = time.monotonic()
    said, asked = _unheard(session)
    heard = await extract.listen(session, said, asked, pending=session.ob["pending"])
    await _deliver(session, await flow.plan(session, heard), started)


def _unheard(session: Session) -> tuple[list[str], str]:
    """User lines the listener hasn't processed yet (even if Persona's bubbles landed after them, when
    they typed mid-reply), plus what Persona last said before them."""
    upto = session.ob.get("heard_upto", 0)
    new = [t for t in session.history if t.role == "user" and t.channel == "text" and t.at > upto]
    if not new:
        return [], ""
    session.ob["heard_upto"] = new[-1].at
    asked = next((t.content for t in reversed(session.history)
                  if t.role == "assistant" and t.at < new[0].at), "")
    return [t.content for t in new], asked


async def _deliver(session: Session, p: Plan, started: float | None = None) -> None:
    bubbles: list[tuple[str, str]] = []
    if p.answer and config.settings().openai_api_key:
        await session.set_typing(True)
        started = started or time.monotonic()
        bubbles += [(b, "custom") for b in split_bubbles(await _write_answer(session, p))]
        if any(LINES[k].asks for k in p.beats):
            # A scripted question follows: the model's part mustn't ask (or echo) one itself.
            beat_text = " ".join(_norm(render(k, session)) for k in p.beats)
            bubbles = [(b, src) for b, src in bubbles
                       if not b.rstrip().endswith("?") and _norm(b) not in beat_text]
    for key in p.beats:
        bubbles += [(b.strip(), key) for b in render(key, session).split("\n") if b.strip()]
    deduped: list[tuple[str, str]] = []
    for bubble, src in bubbles:
        if not deduped or bubble.lower() != deduped[-1][0].lower():
            deduped.append((bubble, src))
    logger.info(f"[{session.id[:8]}] reply: " + ", ".join(src for _, src in deduped))
    session.sending_task = asyncio.current_task()
    try:
        await _send(session, deduped, started)
    finally:
        session.sending_task = None
    if not any(src in LINES and LINES[src].asks for _, src in deduped):
        # "Never twice in a row": a dodged question is blocked only until Persona has had a turn
        # without asking it. After that, one follow-up is fine (within MAX_ASKS).
        session.ob["last_ask"] = None
    if p.complete:
        await flow.complete(session, *p.complete)
    flow.after_agent_turn(session)


async def _send(session: Session, bubbles: list[tuple[str, str]], started: float | None = None) -> None:
    """Send bubbles with a human typing rhythm, running each line's action after its last bubble."""
    for i, (bubble, src) in enumerate(bubbles):
        await session.set_typing(True)
        spent = time.monotonic() - started if (i == 0 and started) else 0
        wait = _typing_time(bubble) - spent
        if wait > 0:
            await asyncio.sleep(wait)
        await session.set_typing(False)
        await session.add_message("agent", "text", bubble, line=src)
        session.remember("assistant", bubble)
        flow.arm_name_timer(session)
        line_done = src in LINES and (i + 1 == len(bubbles) or bubbles[i + 1][1] != src)
        if line_done:
            flow.note_sent(session, src)
            await _act(session, LINES[src].action)
        if i < len(bubbles) - 1:
            await asyncio.sleep(0.35)


async def _act(session: Session, action: str | None) -> None:
    if action == "contact_card" and not session.ob["contact_saved"]:
        await asyncio.sleep(0.4)
        await session.add_message("agent", "contact", name=session.agent_name or "Persona")
        session.ob["card_sent"] = True
        session.changed()
    elif action == "gmail_card" and not mail.connected(session):
        await asyncio.sleep(0.4)
        await mail.send_link(session)
    elif action == "ring":
        from . import calls  # avoid an import cycle

        async def ring():
            await asyncio.sleep(1.0)
            await calls.ring(session)

        asyncio.create_task(ring())


# ── the model's own words ───────────────────────────────────────
async def _write_answer(session: Session, p: Plan) -> str:
    cfg = config.settings()
    client = _openai(cfg.openai_api_key)
    messages = [{"role": "system", "content": prompts.text_system(session, p)}]
    for t in session.history[-60:]:
        messages.append({"role": "system", "content": f"Event: {t.content}"} if t.role == "system"
                        else {"role": t.role, "content": t.content})
    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.chat.completions.create(
            model=cfg.text_model, messages=messages, temperature=0.7,
            tools=TOOLS if mail.connected(session) else openai.NOT_GIVEN,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": _run_tool(session, tc.function.name, tc.function.arguments)})
    return ""


def _run_tool(session: Session, name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        args = {}
    logger.info(f"[{session.id[:8]}] tool {name} {args}")
    if name == "inbox_overview":
        return json.dumps(mail.overview(session))
    if name == "read_inbox":
        return json.dumps(mail.read_all(session))
    if name == "search_inbox":
        return json.dumps(mail.search(session, str(args.get("query", ""))))
    if name == "read_email":
        try:
            return json.dumps(mail.read(session, int(args.get("id", 0))))
        except (TypeError, ValueError):
            return json.dumps({"error": "id must be a number"})
    return json.dumps({"error": f"unknown tool {name}"})


# ── bubbles ─────────────────────────────────────────────────────
# Models sometimes imitate event notes ("[Phone call ended after 1:02]", "Event: …").
# Those must never reach the thread: they'd be Persona inventing what happened.
_FAKE_EVENT = re.compile(r"\[[^\]]*\b(call|ended|declined|missed|event|system|note|ringing)\b[^\]]*\]"
                         r"|^\s*(event|note|system)\s*:.*$", re.IGNORECASE | re.MULTILINE)
BUBBLE_CHARS = 80
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[a-z0-9\"'(])", re.IGNORECASE)
_LIST_ITEM = re.compile(r"^(\d+[.)]|[-•*])\s+")


def _chunk(paragraph: str) -> list[str]:
    """Break a long paragraph into text-sized bubbles at sentence boundaries."""
    if len(paragraph) <= BUBBLE_CHARS or "\n" in paragraph:  # lists stay whole
        return [paragraph]
    chunks: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        if chunks and len(chunks[-1]) + 1 + len(sentence) <= BUBBLE_CHARS:
            chunks[-1] += " " + sentence
        else:
            chunks.append(sentence)
    return chunks


def split_bubbles(text: str, max_bubbles: int = MAX_BUBBLES) -> list[str]:
    text = _FAKE_EVENT.sub("", text)
    # Any line break is a new text, except a list (and its intro line) stays one message.
    paragraphs: list[str] = []
    for line in (ln.strip() for ln in text.strip().splitlines() if ln.strip()):
        if _LIST_ITEM.match(line) and paragraphs and (
                _LIST_ITEM.match(paragraphs[-1].splitlines()[-1]) or paragraphs[-1].endswith(":")):
            paragraphs[-1] += "\n" + line
        else:
            paragraphs.append(line)
    parts: list[str] = []
    for chunk in (c for p in paragraphs for c in _chunk(p)):
        if not parts or chunk.lower() != parts[-1].lower():  # drop exact repeats
            parts.append(chunk)
    if len(parts) > max_bubbles:
        parts = parts[: max_bubbles - 1] + ["\n".join(parts[max_bubbles - 1:])]
    return parts


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _typing_time(text: str) -> float:
    return min(2.4, 0.6 + len(text) * 0.028)
