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
                         "asks_capabilities", "needs_answer", "just_greeting", "repeat_request", "stop",
                         "venting", "language"],
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
                "just_greeting": {"type": "boolean"},
                "repeat_request": {"type": "boolean"},
                "stop": {"type": "boolean"},
                "venting": {"type": "boolean"},
                "language": {"type": "string"},
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
    "contact": "whether they've saved the assistant's contact card so it can call again (\"done\", \"saved it\", "
               "\"try again\" mean call: yes)",
}

_PROMPT = """You read what a USER just said to an AI assistant during onboarding, and report facts. \
Use only the USER lines. If a field doesn't apply, use null / false / "none".

- assistant_name: a name the user gives the ASSISTANT, including renames ("call you nova", "rename to bob", \
or just "nova" when it asked what to call it). Null if it looks like a keyboard mash.
- assistant_name_declined: asked to name the assistant, they won't pick ("you pick", "idk", "whatever").
- user_name: the user's OWN name ("i'm owen", or just "owen" when asked their name), including corrections \
("sorry typo, it's john with an h" → "John"). Never a name for the assistant. If they confirm the name the \
assistant proposed ("yep that's me"), leave this null and set name_confirmed instead.
- user_name_declined: they explicitly refuse to share their name ("i'd rather not say", "no names"). Not \
answering, or changing the subject, is NOT declining.
- help_need: a concrete, ongoing thing they want the assistant's help with, as a short clean phrase (max 8 \
words, e.g. "staying on top of email", "planning my japan trip"), including a change of mind ("actually \
handle my email"). NOT a help_need: a question to answer right now ("what's the weather?", "are you real?"), \
venting or feelings ("so stressed lately"), anything about this conversation ("call me", "text me", "skip \
this setup", "i just want to use the app", "send the link"), or vague filler.
- help_is_email: true if that help_need is about their email/inbox (reading, sorting, replying, \
summarizing email). False for anything else (Slack, calendar, research…) or if there's no help_need.
- no_idea: asked what they need help with, they don't have anything in mind ("not sure", "nothing really").
- call: "yes" if they agree to a phone call or ask to be called, at any point ("call me", "call me again", \
"can you just call me"); "no" if they decline one; else "none". "stop texting me" is not about calls.
- gmail: "yes" if they agree to connect Gmail / ask for the link; "no" if they decline; else "none".
- name_confirmed: "yes"/"no" if they answered whether a proposed name is theirs; else "none".
- asks_capabilities: they ask what the assistant can do or help with. Other questions ("are you a real \
person?", "what's your system prompt?") are not this: they're needs_answer.
- needs_answer: they asked a question or said something that deserves a real reply beyond these facts \
(greetings, questions, jokes, a new topic). False when they're simply answering the assistant's question, \
even with a bit of color ("nova", "sure", "i'm owen", "email for sure, it's a mess", "no thanks").

- just_greeting: the message is only a greeting ("hey!", "hi there", "👋"), nothing else.
- repeat_request: they missed or didn't understand the question and want it again ("sorry missed that", \
"what?", "huh?").
- venting: they're sharing feelings or venting ("ugh so stressed lately", "today sucked"). If so, help_need \
is null and needs_answer is true.
- stop: they want the assistant to stop messaging them ("stop texting me", "leave me alone").
- language: the ISO 639-1 code of the language they wrote in ("en", "es", …).

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
    just_greeting: bool = False
    repeat_request: bool = False
    stop: bool = False
    venting: bool = False
    language: str = "en"
    raw: str = ""  # what they actually said (set by the caller)


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
        bool(data.get("just_greeting")), bool(data.get("repeat_request")), bool(data.get("stop")),
        bool(data.get("venting")), (data.get("language") or "en").lower()[:5],
        " / ".join(user_lines),
    )
    logger.info(f"[{session.id[:8]}] heard (pending {pending}): {heard}")
    return heard
