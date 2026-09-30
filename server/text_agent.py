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

from . import config, flow, prompts
from .session import Session

MAX_TOOL_ROUNDS = 4
MAX_BUBBLES = 3

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


def _chunk(paragraph: str) -> list[str]:
    """Break a long paragraph into text-sized bubbles at sentence boundaries."""
    if len(paragraph) <= BUBBLE_CHARS:
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
    # Any line break is a new text; drop exact repeats ("calling you now!" twice).
    paragraphs = [p.strip() for p in text.strip().splitlines() if p.strip()]
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

    client = _openai(cfg.openai_api_key)
    messages = _chat_messages(session)
    after: list[Callable[[], Awaitable[None]]] = []
    text = ""

    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.chat.completions.create(
            model=cfg.text_model,
            messages=messages,
            tools=TOOLS,
            temperature=0.8,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            text = msg.content or ""
            break
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls:
            result = await _run_tool(session, tc.function.name, tc.function.arguments, after)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        # Tools can move the flow on (e.g. a name was just saved): re-brief the model.
        messages[0] = {"role": "system", "content": prompts.text_system(session)}

    bubbles = split_bubbles(text)
    if session.call.status == "idle" and any(getattr(fn, "rings", False) for fn in after):
        # Placing a call: say "calling you now" and nothing that pretends it already happened.
        bubbles = bubbles[:1]
    for i, bubble in enumerate(bubbles):
        await session.set_typing(True)
        wait = _typing_time(bubble) - (time.monotonic() - started if i == 0 else 0)
        if wait > 0:
            await asyncio.sleep(wait)
        await session.set_typing(False)
        await session.add_message("agent", "text", bubble)
        session.remember("assistant", bubble)
        flow.arm_name_timer(session)
        if i < len(bubbles) - 1:
            await asyncio.sleep(0.35)

    for fn in after:
        await fn()


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
                return (f"saved: they didn't pick, so you go by {agent_name}. In this reply, say in a few words "
                        "that they can rename you anytime, then ask if you can give them a quick call.")
            return (f"saved: you're {agent_name}. In this reply, react to the name in two to four words, "
                    "then ask if you can give them a quick call.")
        return f"saved: you're {agent_name}"

    if name == "set_user_name":
        user_name = tidy_name(str(args.get("name", "")))
        session.set_user_name(user_name)
        return f"saved: the user is {user_name}" if user_name else "error: empty name"

    if name == "keep_texting":
        if not session.call_offer_done:
            session.call_offer_done = True
            session.changed()
            session.remember("system", "The user would rather text than call for now")
        return "ok, staying on text"

    if name == "call_user":
        from . import calls  # avoid an import cycle

        if session.call.status != "idle":
            return "a call is already in progress"

        async def ring_after_texting():
            await asyncio.sleep(1.0)
            await calls.ring(session)

        ring_after_texting.rings = True
        after.append(ring_after_texting)
        return ("The phone starts ringing right after this reply. Say you're calling now in one short text "
                "and stop there. Don't describe or imagine the call; you'll get an Event about how it went.")

    return f"error: unknown tool {name}"
