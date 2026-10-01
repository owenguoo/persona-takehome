"""The listener: reads what the user just said and reports onboarding facts.

The chat model is good at conversation and bad at bookkeeping, so understanding
is a separate, single-purpose call. The flow engine (flow.py) turns what it hears
into state and beats; the chat model only answers free-form questions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from loguru import logger
from openai import AsyncOpenAI

from . import config
from .session import Session

_YES_NO = {"type": "string", "enum": ["yes", "no", "none"]}
_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "heard",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["assistant_name", "assistant_name_declined", "user_name", "user_name_declined",
                         "help_need", "help_is_email", "no_idea", "call", "gmail", "name_confirmed",
                         "asks_capabilities", "needs_answer"],
            "properties": {
                "assistant_name": {"type": ["string", "null"]},
                "assistant_name_declined": {"type": "boolean"},
                "user_name": {"type": ["string", "null"]},
                "user_name_declined": {"type": "boolean"},
                "help_need": {"type": ["string", "null"]},
                "help_is_email": {"type": "boolean"},
                "no_idea": {"type": "boolean"},
                "call": _YES_NO,
                "gmail": _YES_NO,
                "name_confirmed": _YES_NO,
                "asks_capabilities": {"type": "boolean"},
                "needs_answer": {"type": "boolean"},
            },
        },
    },
}

PENDING = {
    "agent_name": "what the user wants to name the assistant",
    "call": "whether the assistant can give them a quick phone call",
    "user_name": "the user's own name",
    "confirm_name": "whether their name is {owner} (as their email says)",
    "first_action": "what they'd like help with / to start with",
    "gmail": "whether to connect their Gmail",
}

_PROMPT = """You read what a USER just said to an AI assistant during onboarding, and report facts. \
Use only the USER lines. If a field doesn't apply, use null / false / "none".

- assistant_name: a name the user gives the ASSISTANT, including renames ("call you nova", "rename to bob", \
or just "nova" when it asked what to call it). Null if it looks like a keyboard mash.
- assistant_name_declined: asked to name the assistant, they won't pick ("you pick", "idk", "whatever").
- user_name: the user's OWN name ("i'm owen", or just "owen" when asked their name). Never a name for the \
assistant. If they confirm the name the assistant proposed ("yep that's me"), leave this null and set \
name_confirmed instead.
- user_name_declined: they explicitly refuse to share their name ("i'd rather not say", "no names"). Not \
answering, or changing the subject, is NOT declining.
- help_need: something concrete they want help with or to start with, as a short clean phrase (max 8 \
words, e.g. "staying on top of email"). Not a request about this conversation ("call me", "send the link").
- help_is_email: true if that help_need is about their email/inbox (reading, sorting, replying, \
summarizing email). False for anything else (Slack, calendar, research…) or if there's no help_need.
- no_idea: asked what they need help with, they don't have anything in mind ("not sure", "nothing really").
- call: "yes" if they agree to a phone call or ask to be called; "no" if they decline one; else "none".
- gmail: "yes" if they agree to connect Gmail / ask for the link; "no" if they decline; else "none".
- name_confirmed: "yes"/"no" if they answered whether a proposed name is theirs; else "none".
- asks_capabilities: they ask what the assistant can do / help with.
- needs_answer: they asked a question or said something that deserves a real reply beyond these facts \
(greetings, questions, jokes, a new topic). False when they're simply answering the assistant's question, \
even with a bit of color ("nova", "sure", "i'm owen", "email for sure, it's a mess", "no thanks").

A bare "sure"/"yes"/"no" answers the assistant's pending question, given below."""


@dataclass
class Heard:
    assistant_name: str | None = None
    assistant_name_declined: bool = False
    user_name: str | None = None
    user_name_declined: bool = False
    help_need: str | None = None
    help_is_email: bool = False
    no_idea: bool = False
    call: str = "none"
    gmail: str = "none"
    name_confirmed: str = "none"
    asks_capabilities: bool = False
    needs_answer: bool = False


async def listen(session: Session, user_lines: list[str], context: str = "",
                 pending: str | None = None, transcript: bool = False) -> Heard:
    """What the user's lines tell us. `transcript` mode reads a whole call (both sides)."""
    cfg = config.settings()
    if not cfg.openai_api_key or not user_lines:
        return Heard()
    from .lines import INBOX_OWNER

    known = [f"The assistant is {'named ' + session.agent_name if session.agent_name else 'not named yet'}."]
    if session.user_name:
        known.append(f"The user's name is {session.user_name}.")
    if pending:
        known.append(f"The assistant's pending question is about: {PENDING.get(pending, pending)}."
                     .format(owner=INBOX_OWNER))
    body = "\n".join(user_lines) if transcript else (
        (f"ASSISTANT: {context}\n" if context else "") + "\n".join(f"USER: {t}" for t in user_lines))
    try:
        r = await AsyncOpenAI(api_key=cfg.openai_api_key).chat.completions.create(
            model=cfg.text_model,
            messages=[{"role": "system", "content": _PROMPT},
                      {"role": "user", "content": " ".join(known) + "\n\n" + body}],
            response_format=_FORMAT,
            temperature=0,
        )
        data = json.loads(r.choices[0].message.content or "{}")
    except Exception:
        logger.exception("listener failed")
        return Heard()

    def clean(v):
        return v.strip()[:60] if isinstance(v, str) and v.strip() else None

    heard = Heard(
        clean(data.get("assistant_name")), bool(data.get("assistant_name_declined")),
        clean(data.get("user_name")), bool(data.get("user_name_declined")),
        clean(data.get("help_need")), bool(data.get("help_is_email")), bool(data.get("no_idea")),
        data.get("call", "none"), data.get("gmail", "none"), data.get("name_confirmed", "none"),
        bool(data.get("asks_capabilities")), bool(data.get("needs_answer")),
    )
    logger.info(f"[{session.id[:8]}] heard (pending {pending}): {heard}")
    return heard
