"""The iMessage side: an OpenAI chat model that texts back.

Replies are debounced: if the user double-texts (or texts while the agent is
still "typing"), the pending reply is cancelled and one reply covers it all.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable

from loguru import logger
import openai
from openai import AsyncOpenAI

from . import config, extract, flow, lines, mail, prompts
from .session import Session

MAX_TOOL_ROUNDS = 4
MAX_BUBBLES = 3        # for one piece of the model's own text
MAX_REPLY_BUBBLES = 4  # for a whole reply

# Every reply is an ordered list of parts: an approved line (sent word for word)
# or the model's own text.
REPLY_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "reply",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["messages"],
            "properties": {
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "value"],
                        "properties": {
                            "kind": {"type": "string", "enum": ["line", "text"]},
                            "value": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_agent_name",
            "description": "Save the name the user has chosen for YOU, the assistant (never the user's own name). "
                           "Call as soon as they give one, or when they rename you. Capitalize it like a name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "The name, as the user would write it."}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_user_name",
            "description": "Save the USER's own name once they tell you (e.g. \"i'm owen\").",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Their name, capitalized."}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keep_texting",
            "description": "The user would rather not have a call right now; carry on over text.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_gmail_link",
            "description": "Text the user a link card to connect their Gmail. Only once they've agreed.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decline_gmail",
            "description": "The user doesn't want to connect Gmail right now.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inbox_overview",
            "description": "Their connected inbox at a glance: counts, top senders, the 10 most recent emails.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_inbox",
            "description": "Read every email in their connected inbox in full. Use it for anything that needs "
                           "judgment across the inbox: to-dos, what's urgent, deadlines, who's waiting on them.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_inbox",
            "description": "Find emails mentioning specific words (a sender, a company, a topic). "
                           "Not for judgment questions like to-dos: use read_inbox for those.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read one email in full, by id from inbox_overview or search_inbox.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_user",
            "description": "Ring the user's phone for a quick voice call. Only when they've agreed to (or asked for) a call.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_client: AsyncOpenAI | None = None
_client_key: str | None = None


def _openai(key: str) -> AsyncOpenAI:
    global _client, _client_key
    if _client is None or _client_key != key:
        _client, _client_key = AsyncOpenAI(api_key=key), key
    return _client


def schedule_reply(session: Session, delay: float = 0.9) -> None:
    """(Re)start the agent's reply after a short pause."""
    if session.reply_task and not session.reply_task.done():
        session.reply_task.cancel()
    session.reply_task = asyncio.create_task(_reply_after(session, delay))


async def _reply_after(session: Session, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        if session.line_task and not session.line_task.done():
            await asyncio.shield(session.line_task)  # let a scripted line finish first
        await _reply(session)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception("text reply failed")
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


def _chat_messages(session: Session) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": prompts.text_system(session)}]
    for t in session.history[-60:]:
        if t.role == "system":
            msgs.append({"role": "system", "content": f"Event: {t.content}"})
        else:
            msgs.append({"role": t.role, "content": t.content})
    return msgs


# Models sometimes imitate event notes ("[Phone call ended after 1:02]", "Event: …").
# Those must never reach the thread: they'd be the agent inventing what happened.
_FAKE_EVENT = re.compile(r"\[[^\]]*\b(call|ended|declined|missed|event|system|note|ringing)\b[^\]]*\]|^\s*(event|note|system)\s*:.*$",
                         re.IGNORECASE | re.MULTILINE)


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
    # Drop exact repeats ("calling you now!" twice).
    paragraphs: list[str] = []
    for line in (ln.strip() for ln in text.strip().splitlines() if ln.strip()):
        if _LIST_ITEM.match(line) and paragraphs and (
                _LIST_ITEM.match(paragraphs[-1].splitlines()[-1]) or paragraphs[-1].endswith(":")):
            paragraphs[-1] += "\n" + line
        else:
            paragraphs.append(line)
    parts: list[str] = []
    for chunk in (c for p in paragraphs for c in _chunk(p)):
        if not parts or chunk.lower() != parts[-1].lower():
            parts.append(chunk)
    if len(parts) > max_bubbles:
        parts = parts[: max_bubbles - 1] + ["\n".join(parts[max_bubbles - 1 :])]
    return parts


def tidy_name(raw: str) -> str:
    """Trim quotes/punctuation; capitalize names typed all in lowercase ("nova" → "Nova")."""
    name = raw.strip().strip("\"'“”‘’").strip().rstrip(".!?,").strip()[:32]
    if name and name == name.lower() and any(c.isalpha() for c in name):
        name = " ".join(w[:1].upper() + w[1:] for w in name.split())
    return name


_ITEM = re.compile(r'"kind"\s*:\s*"(line|text)"\s*,\s*"value"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _parse_items(session: Session, content: str) -> list[dict]:
    """The model's reply parts. Raw JSON must never reach the thread."""
    text = content.strip()
    try:
        # raw_decode reads the first object and ignores the rest: the model
        # occasionally repeats the whole object twice.
        obj, _ = json.JSONDecoder(strict=False).raw_decode(text)
        return list(obj["messages"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    if not text.startswith("{"):
        return [{"kind": "text", "value": text}]  # plain words, not JSON at all
    items = [{"kind": k, "value": json.loads(f'"{v}"')} for k, v in _ITEM.findall(text)]
    logger.warning(f"[{session.id[:8]}] malformed reply JSON, salvaged {len(items)} part(s): {text[:200]!r}")
    return items


_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _match_line(session: Session, value: str) -> str | None:
    """A line id, from either the id itself or the line's wording (models mix them up)."""
    avail = lines.available(session)
    if value in avail:
        return value
    want = _norm(value)
    for key, text in avail.items():
        if want and (want == _norm(text) or want in {_norm(b) for b in text.split("\n")}):
            return key
    return None


def plan_reply(session: Session, content: str) -> list[tuple[str, str]]:
    """Expand the model's reply into (bubble, source) pairs; source is a line id or "custom"."""
    items = _parse_items(session, content)
    out: list[tuple[str, str]] = []
    for item in items:
        value = _CONTROL.sub("", str(item.get("value", ""))).strip()
        if item.get("kind") == "line":
            key = _match_line(session, value)
            if key is None:
                logger.warning(f"[{session.id[:8]}] model picked unavailable line {value!r}")
                if " " in value:  # it wrote words, not an id: keep them as its own text
                    out += [(b, "custom") for b in split_bubbles(value)]
                continue
            text = lines.render(key, session) or ""
            out += [(b.strip(), key) for b in text.split("\n") if b.strip()]
        else:
            # Typed a line's wording out by hand? Count it as that line.
            known = {b.strip().lower(): k for k, t in lines.available(session).items() for b in t.split("\n")}
            out += [(b, known.get(b.strip().lower(), "custom")) for b in split_bubbles(value)]
    deduped: list[tuple[str, str]] = []
    for bubble, src in out:
        prev = deduped[-1] if deduped else None
        if prev and bubble.lower() == prev[0].lower():
            continue
        # The model often sends each list item as its own part: keep a list in one message.
        if (prev and src == prev[1] == "custom" and _LIST_ITEM.match(bubble)
                and (_LIST_ITEM.match(prev[0].splitlines()[-1]) or prev[0].endswith(":"))):
            deduped[-1] = (prev[0] + "\n" + bubble, "custom")
            continue
        deduped.append((bubble, src))
    return deduped[:MAX_REPLY_BUBBLES]


async def _send(session: Session, bubbles: list[tuple[str, str]], started: float | None = None) -> None:
    """Send bubbles with a human typing rhythm; the first one absorbs time already spent thinking."""
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
        if i < len(bubbles) - 1:
            await asyncio.sleep(0.35)


def schedule_line(session: Session, key: str, delay: float = 0.9) -> None:
    """Send an approved line directly, without the model (e.g. the opener).

    It can't be cancelled by the user texting: replies wait for it instead, so a
    fast "nova" never lands before "what do you want to call me?" has been asked.
    """
    async def run():
        await asyncio.sleep(delay)
        text = lines.render(key, session)
        if text:
            await _send(session, [(b, key) for b in text.split("\n") if b.strip()])
        await session.set_typing(False)
        flow.after_agent_turn(session)

    session.line_task = asyncio.create_task(run())


def _typing_time(text: str) -> float:
    return min(2.4, 0.6 + len(text) * 0.028)


async def _reply(session: Session) -> None:
    cfg = config.settings()
    if not cfg.openai_api_key:
        await session.add_notice("No OPENAI_API_KEY yet. Add it to .env and send another message.")
        return

    await session.mark_read()
    await session.set_typing(True)
    started = time.monotonic()

    if extract.missing(session):  # listen first, so the reply talks from up-to-date state
        said, asked = extract.since_last_reply(session)
        await flow.apply_heard(session, await extract.listen(session, said, asked))

    client = _openai(cfg.openai_api_key)
    messages = _chat_messages(session)
    after: list[Callable[[], Awaitable[None]]] = []
    content = ""

    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.chat.completions.create(
            model=cfg.text_model,
            messages=messages,
            tools=TOOLS,
            response_format=REPLY_FORMAT,
            temperature=0.8,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            content = msg.content or ""
            break
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            result = await _run_tool(session, tc.function.name, tc.function.arguments, after)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        # Tools can move the flow on (e.g. a name was just saved): re-brief the model.
        messages[0] = {"role": "system", "content": prompts.text_system(session)}

    bubbles = plan_reply(session, content)
    if session.call.status == "idle" and any(getattr(fn, "rings", False) for fn in after):
        # Placing a call: say "calling you now" and nothing that pretends it already happened.
        calling = [b for b in bubbles if b[1] == "calling_now"]
        bubbles = calling or [(lines.render("calling_now", session) or "calling you now", "calling_now")]
    rings = any(getattr(fn, "rings", False) for fn in after)
    if not rings and session.call.status == "idle" and any(src == "calling_now" for _, src in bubbles):
        # "calling you now" is a promise: if the model said it without calling, call anyway.
        from . import calls

        async def ring():
            await asyncio.sleep(1.0)
            await calls.ring(session)

        after.append(ring)
        logger.warning(f"[{session.id[:8]}] sent calling_now without call_user; ringing anyway")
    logger.info(f"[{session.id[:8]}] reply: " + ", ".join(src for _, src in bubbles))
    await _send(session, bubbles, started)
    session.call_outcome = None  # the agent has now responded to it

    for fn in after:
        await fn()
    flow.after_agent_turn(session)


async def _run_tool(session: Session, name: str, raw_args: str, after: list) -> str:
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        args = {}
    logger.info(f"[{session.id[:8]}] tool {name} {args}")

    if name == "set_agent_name":
        agent_name = tidy_name(str(args.get("name", "")))
        if not agent_name:
            return "error: empty name"
        if agent_name == session.agent_name:
            return f"that's already your name ({agent_name}); carry on"
        first_time = session.agent_name is None
        flow.cancel_name_timer(session)
        await session.set_agent_name(agent_name)
        session.remember("system", f"The user named you {agent_name}")
        if first_time and not session.call_offer_done:
            if agent_name == flow.DEFAULT_AGENT_NAME:
                return f"saved: you go by {agent_name}. Reply with the default_named line."
            return f"saved: you're {agent_name}. Reply with the named line (it reacts and offers the call)."
        return f"saved: you're {agent_name}. The renamed line fits."

    if name == "set_user_name":
        user_name = tidy_name(str(args.get("name", "")))
        session.set_user_name(user_name)
        return (f"saved: the user is {user_name}. The nice_to_meet line fits." if user_name
                else "error: empty name")

    if name == "keep_texting":
        if not session.call_offer_done:
            session.call_offer_done = True
            session.changed()
            session.remember("system", "The user would rather text than call for now")
        return "ok, staying on text. The keep_texting line fits."

    if name == "send_gmail_link":
        if mail.connected(session):
            return "Gmail is already connected; use the inbox tools."

        async def send_card():
            await mail.send_link(session)

        mail.mark_link_sent(session)  # the card goes out after the reply, but the line fits now
        after.append(send_card)
        return "The link card is sent right after your reply. The gmail_link_sent line fits."

    if name == "decline_gmail":
        await mail.deny(session)
        return "noted. The gmail_declined line fits; don't push."

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

    if name == "call_user":
        from . import calls  # avoid an import cycle

        if session.call.status != "idle":
            return "a call is already in progress"

        async def ring_after_texting():
            await asyncio.sleep(1.0)
            await calls.ring(session)

        ring_after_texting.rings = True
        after.append(ring_after_texting)
        return ("The phone starts ringing right after this reply. Reply with just the calling_now line. "
                "Don't describe or imagine the call; you'll get an Event about how it went.")

    return f"error: unknown tool {name}"
