"""The listener: pulls onboarding facts out of what the user said, separately from
the model that chats.

The chatting model is unreliable at bookkeeping (it answers "call you nova" or
"i'm owen" nicely and forgets to save either), so one small single-purpose call
reads the user's latest lines first and updates the session. The chat model then
talks from that state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from loguru import logger
from openai import AsyncOpenAI

from . import config
from .session import Session

_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "heard",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["assistant_name", "assistant_name_declined", "user_name", "help_need", "declines_call"],
            "properties": {
                "assistant_name": {"type": ["string", "null"]},
                "assistant_name_declined": {"type": "boolean"},
                "user_name": {"type": ["string", "null"]},
                "help_need": {"type": ["string", "null"]},
                "declines_call": {"type": "boolean"},
            },
        },
    },
}

_PROMPT = """You read the latest lines of a conversation between an AI assistant and a user, \
and report facts the USER stated. Only use what the lines marked USER say.

- assistant_name: a name the user gives the ASSISTANT, including renames ("call you nova", "how about juno?", \
"rename to bob", "actually call yourself max", or just "nova" when the assistant asked what to call it). \
Null if none. Null if it looks like a keyboard mash or an accident ("asdfjkl") rather than a name.
- assistant_name_declined: true if, asked to name the assistant, they won't pick ("you pick", "idk", \
"whatever", "doesn't matter").
- user_name: the user's OWN name ("i'm owen", "owen here", or just "owen" when asked their name). \
Never a name they're giving the assistant ("rename to bob" is the assistant's name). Null if none.
- help_need: what they want help with in their life or work, as a short phrase in their terms (max 8 words, \
e.g. "staying on top of email"). Not a request about this conversation ("call me", "send the link", \
"rename yourself"). Null if they didn't say.
- declines_call: true only if they clearly say they don't want a phone call ("not big on calls", \
"let's just text"). False if they're asking for a call ("call me", "call again") or it's ambiguous.

Capitalize names like names."""


@dataclass
class Heard:
    assistant_name: str | None = None
    assistant_name_declined: bool = False
    user_name: str | None = None
    help_need: str | None = None
    declines_call: bool = False


def missing(session: Session) -> bool:
    return not (session.agent_name and session.user_name and session.help_need and session.call_offer_done)


async def listen(session: Session, user_lines: list[str], context: str = "") -> Heard:
    cfg = config.settings()
    if not cfg.openai_api_key or not user_lines or not missing(session):
        return Heard()
    state = []
    state.append("The assistant has no name yet and has asked the user to name it." if not session.agent_name
                 else f"The assistant is already named {session.agent_name}.")
    if session.user_name:
        state.append(f"The user's name is already known ({session.user_name}).")
    snippet = "\n".join(state) + "\n\n" + (f"ASSISTANT: {context}\n" if context else "") + \
        "\n".join(f"USER: {t}" for t in user_lines)
    try:
        r = await AsyncOpenAI(api_key=cfg.openai_api_key).chat.completions.create(
            model=cfg.text_model,
            messages=[{"role": "system", "content": _PROMPT}, {"role": "user", "content": snippet}],
            response_format=_FORMAT,
            temperature=0,
        )
        data = json.loads(r.choices[0].message.content or "{}")
    except Exception:
        logger.exception("listener failed")
        return Heard()

    def clean(v):
        return v.strip()[:60] if isinstance(v, str) and v.strip() else None

    heard = Heard(clean(data.get("assistant_name")), bool(data.get("assistant_name_declined")),
                  clean(data.get("user_name")), clean(data.get("help_need")), bool(data.get("declines_call")))
    logger.info(f"[{session.id[:8]}] heard: {heard}")
    return heard


def since_last_reply(session: Session) -> tuple[list[str], str]:
    """The user's lines since the agent last spoke, and what the agent last said."""
    lines: list[str] = []
    for t in reversed(session.history):
        if t.role == "assistant":
            return list(reversed(lines)), t.content
        if t.role == "user":
            lines.append(t.content)
    return list(reversed(lines)), ""
