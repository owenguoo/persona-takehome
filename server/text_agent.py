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

from . import config, prompts
from .session import Session

MAX_TOOL_ROUNDS = 4
MAX_BUBBLES = 3

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_agent_name",
            "description": "Save the name the user has chosen for you. Call as soon as they give one.",
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
            msgs.append({"role": "system", "content": f"[{t.content}]"})
        else:
            msgs.append({"role": t.role, "content": t.content})
    return msgs


def split_bubbles(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if len(parts) > MAX_BUBBLES:
        parts = parts[: MAX_BUBBLES - 1] + ["\n\n".join(parts[MAX_BUBBLES - 1 :])]
    return parts


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

    bubbles = split_bubbles(text)
    for i, bubble in enumerate(bubbles):
        await session.set_typing(True)
        wait = _typing_time(bubble) - (time.monotonic() - started if i == 0 else 0)
        if wait > 0:
            await asyncio.sleep(wait)
        await session.set_typing(False)
        await session.add_message("agent", "text", bubble)
        session.remember("assistant", bubble)
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
        agent_name = str(args.get("name", "")).strip()[:32]
        if not agent_name:
            return "error: empty name"
        await session.set_agent_name(agent_name)
        session.remember("system", f"The user named you {agent_name}")
        return f"saved. you are now {agent_name}"

    if name == "call_user":
        from . import calls  # avoid an import cycle

        if session.call.status != "idle":
            return "a call is already in progress"

        async def ring_after_texting():
            await asyncio.sleep(1.0)
            await calls.ring(session)

        after.append(ring_after_texting)
        return "the phone will start ringing right after your message is sent"

    return f"error: unknown tool {name}"
