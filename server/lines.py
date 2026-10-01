"""The approved lines: exact wording for every beat of onboarding.

Edit the wording here. The flow engine (flow.py) decides which lines to send;
the model only writes free-form answers around them. A line can ASK about one
onboarding item (so the engine knows what's pending and never asks it twice in
a row) and can carry an ACTION that happens right after it's sent, so saying
"calling you now" and actually ringing can't drift apart.

"\\n" splits a line into bubbles. Placeholders: {agent_name}, {user_name}, {owner}.
"""
from __future__ import annotations

from dataclasses import dataclass

from .session import Session

INBOX_OWNER = "Alex"  # the simulated inbox belongs to "Alex" (see mail.py)


@dataclass(frozen=True)
class Line:
    text: str
    asks: str | None = None    # the onboarding item this line asks about
    action: str | None = None  # "contact_card" | "gmail_card" | "ring"


LINES: dict[str, Line] = {
    # ── 1. Persona's name ──
    "opener": Line("hey! i'm your new persona 👋\nwhat do you want to call me?", asks="agent_name"),
    "named": Line("{agent_name}, i like it\nhere's my contact, save it so my calls come through",
                  action="contact_card"),
    "default_named": Line("i'll go by {agent_name} for now, you can rename me anytime\n"
                          "here's my contact, save it so my calls come through", action="contact_card"),
    "renamed": Line("{agent_name} it is"),
    # ── the call ──
    "offer_call": Line("can i give you a quick call? it's faster than texting", asks="call"),
    "calling_now": Line("calling you now 📞", action="ring"),
    "keep_texting": Line("all good, texting works too"),
    "call_blocked": Line("my call didn't go through, i'm not in your contacts yet\n"
                         "tap Add on my card and i'll try again"),
    "after_decline": Line("no worries, we can keep going here"),
    "after_missed": Line("tried calling, no stress. we can keep going here"),
    "call_failed": Line("hm, the call didn't go through. we can keep going here"),
    "after_call": Line("good chatting!"),
    # ── 2. their name ──
    "ask_user_name": Line("what's your name, by the way?", asks="user_name"),
    "confirm_email_name": Line("your email says you're {owner}, is that right?", asks="confirm_name"),
    "nice_to_meet": Line("nice to meet you, {user_name}"),
    "no_name_ok": Line("all good, no names needed"),
    # ── 4. first action ──
    "ask_help": Line("what's one thing you'd love off your plate this week?", asks="first_action"),
    "capabilities": Line("i can help with email, your calendar, reminders and quick research\n"
                         "want to start with any of these?", asks="first_action"),
    # ── 3. gmail ──
    "gmail_offer": Line("want to connect your gmail? easier to show you than tell you", asks="gmail"),
    "gmail_for_ideas": Line("no worries. connect your gmail and i'll find something to take off your plate",
                            asks="gmail"),
    "gmail_link": Line("here's the link, tap it and i'll take a look", action="gmail_card"),
    "gmail_declined": Line("no problem, we'll leave email out of it"),
    # ── the endgame ──
    "on_it": Line("on it, i'll text you when it's done"),
    "all_set": Line("you're all set! if anything comes to mind, just text me"),
}


def render(key: str, s: Session) -> str:
    return LINES[key].text.format(agent_name=s.agent_name or "Persona", user_name=s.user_name or "",
                                  owner=INBOX_OWNER)
