"""The approved lines: exact wording for the key beats of onboarding.

Edit the wording here. The model sees these (with a note on when each fits) and
prefers them; it writes its own text only when the moment calls for something
they don't cover. Each line is sent word for word; "\\n" splits it into bubbles.
Placeholders: {agent_name}, {user_name}. A line whose placeholder isn't known yet
is not offered.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .session import Session


@dataclass(frozen=True)
class Line:
    text: str
    when: str
    show: Callable[[Session], bool] | None = None  # only offered when this is true


def _after(outcome: str):
    return lambda s: s.call_outcome == outcome


def _gmail_status(s: Session) -> str:
    return (s.mail or {}).get("status", "none")


def _gmail_open(s: Session) -> bool:
    return _gmail_status(s) != "connected"


def _gmail_link_out(s: Session) -> bool:
    return _gmail_open(s) and bool((s.mail or {}).get("link_sent"))


def _gmail_offerable(s: Session) -> bool:
    return bool(s.help_need) and _gmail_status(s) == "none"


def _need_unknown(s: Session) -> bool:
    return not s.help_need


def _user_unknown(s: Session) -> bool:
    return not s.user_name


LINES: dict[str, Line] = {
    # ── naming ──
    "opener": Line(
        "hey! i'm your new persona 👋\nwhat do you want to call me?",
        "Your very first message. Sent automatically; never choose it yourself.",
    ),
    "ask_agent_name": Line(
        "so, what do you want to call me?",
        "Steering back to naming you after answering something else.",
    ),
    "check_mash": Line(
        "haha is that a name, or did your cat walk across the keyboard?",
        "They sent something that looks like a keyboard mash, not a name.",
    ),
    "named": Line(
        "{agent_name}, i like it.\ncan i give you a quick call? it's faster than texting",
        "Right after they first name you.",
    ),
    "default_named": Line(
        "i'll go by {agent_name} for now. you can rename me anytime\ncan i give you a quick call? it's faster than texting",
        "They didn't pick a name, so you've gone with the default.",
    ),
    "renamed": Line(
        "{agent_name} it is",
        "They renamed you later on.",
    ),
    # ── the call ──
    "calling_now": Line(
        "calling you now 📞",
        "Right after you call call_user.",
    ),
    "keep_texting": Line(
        "all good, texting works too",
        "They'd rather not have a call.",
    ),
    "after_decline": Line(
        "no worries, we can keep going here",
        "They declined your call.",
        _after("declined"),
    ),
    "after_missed": Line(
        "tried calling, no stress. we can keep going here",
        "They didn't pick up.",
        _after("missed"),
    ),
    "call_failed": Line(
        "hm, the call didn't go through. we can keep going here",
        "The call couldn't connect.",
        _after("failed"),
    ),
    # ── getting to know them ──
    "ask_user_name": Line(
        "what's your name, by the way?",
        "You don't know their name yet.",
        _user_unknown,
    ),
    "nice_to_meet": Line(
        "nice to meet you, {user_name}",
        "They just told you their name.",
    ),
    "ask_help": Line(
        "what's one thing you'd love off your plate this week?",
        "You don't know yet what they need help with.",
        _need_unknown,
    ),
    # ── gmail ──
    "gmail_offer": Line(
        "want to connect your gmail? easier to show you than tell you",
        "You know what they need help with and Gmail isn't connected yet.",
        _gmail_offerable,
    ),
    "gmail_link_sent": Line(
        "tap that and i'll take a look",
        "Right after you call send_gmail_link.",
        _gmail_link_out,
    ),
    "gmail_declined": Line(
        "no problem, we can do that later",
        "They don't want to connect Gmail right now.",
    ),
}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _values(s: Session) -> dict[str, str | None]:
    return {"agent_name": s.agent_name, "user_name": s.user_name}


def available(s: Session) -> dict[str, str]:
    """Lines the model may choose right now, with placeholders filled in."""
    vals = _values(s)
    out = {}
    for key, line in LINES.items():
        if key == "opener":
            continue
        if line.show and not line.show(s):
            continue
        needed = _PLACEHOLDER.findall(line.text)
        if all(vals.get(n) for n in needed):
            out[key] = line.text.format(**{n: vals[n] for n in needed})
    return out


def render(key: str, s: Session) -> str | None:
    return available(s).get(key) if key != "opener" else LINES["opener"].text


def catalog(s: Session) -> str:
    """The lines section of the system prompt."""
    avail = available(s)
    rows = [f'- {key}: "{text}"  ({LINES[key].when})'.replace("\n", " / ") for key, text in avail.items()]
    return "\n".join(rows)
