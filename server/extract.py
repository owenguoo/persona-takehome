"""Pull facts out of what the user said, separately from the model that chats.

The chatting model is unreliable at bookkeeping (it answers "i'm owen" nicely and
forgets to save it), so a small single-purpose call does the listening instead.
"""
from __future__ import annotations

import json

from loguru import logger
from openai import AsyncOpenAI

from . import config
from .session import Session

_NAME_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "user_name",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {"name": {"type": ["string", "null"]}},
        },
    },
}

_NAME_PROMPT = """You read a snippet of a conversation between an AI assistant and a user.
Return the user's OWN name if the user stated it in the lines marked USER (e.g. "i'm owen", \
"owen here", "it's sarah", or just "owen" when the assistant had asked their name).
Return null if they didn't, or if the name is one they're giving the ASSISTANT \
("call you nova", "your name is jarvis"). Capitalize it like a name."""


async def user_name(session: Session, user_lines: list[str], context: str = "") -> str | None:
    cfg = config.settings()
    if not cfg.openai_api_key or not user_lines:
        return None
    snippet = (f"ASSISTANT: {context}\n" if context else "") + "\n".join(f"USER: {t}" for t in user_lines)
    try:
        r = await AsyncOpenAI(api_key=cfg.openai_api_key).chat.completions.create(
            model=cfg.text_model,
            messages=[{"role": "system", "content": _NAME_PROMPT}, {"role": "user", "content": snippet}],
            response_format=_NAME_FORMAT,
            temperature=0,
        )
        name = json.loads(r.choices[0].message.content or "{}").get("name")
    except Exception:
        logger.exception("name extraction failed")
        return None
    return name.strip()[:32] if isinstance(name, str) and name.strip() else None


def since_last_reply(session: Session) -> tuple[list[str], str]:
    """The user's lines since the agent last spoke, and what the agent last said."""
    lines: list[str] = []
    for t in reversed(session.history):
        if t.role == "assistant":
            return list(reversed(lines)), t.content
        if t.role == "user":
            lines.append(t.content)
    return list(reversed(lines)), ""
