"""The onboarding flow engine.

Onboarding is four small flows, each an item with a status:

  1. agent_name    Persona's nickname        asked first; anything but a name → "Your Persona"
  2. user_name     their name                asked on the call (or text); "rather not say" is final
  3. gmail         a connected inbox          offered on the call; "no" is final
  4. first_action  something to start with    asked, or reached via "what can you do?"; no idea → gmail
     (+ the call itself, offered once after naming; a request to be called always wins)

Each user message goes: listener (extract.py) → plan() here → reply (text_agent.py).
plan() turns what was heard into state, then into BEATS: approved lines from
lines.py, sent word for word, with their side effects (contact card, Gmail link,
ringing). The chat model is only asked to write something when the user said
something that needs a real answer.

The one rule: never ask the same thing twice in a row. If the user ignores a
question, it's skipped for now (each item gets at most MAX_ASKS asks, never back
to back) and the flow moves on.

After every agent turn a small loop asks: WAITING on the user (a question or a
link is out, a call is ringing), or STALLED (nothing pending, onboarding open)?
If stalled, Persona double-texts the next ask, once.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from loguru import logger

from . import config
from .lines import INBOX_OWNER, LINES
from .session import Session

DEFAULT_AGENT_NAME = "Your Persona"
MAX_ASKS = {"agent_name": 2, "call": 2, "user_name": 2, "first_action": 2, "gmail": 2}
ORDER = ["call", "user_name", "first_action", "gmail"]  # what to ask next, in order


def tidy_name(raw: str) -> str:
    """Trim quotes/punctuation; capitalize names typed all in lowercase ("nova" → "Nova")."""
    name = raw.strip().strip("\"'“”‘’").strip().rstrip(".!?,").strip()[:32]
    if name and name == name.lower() and any(c.isalpha() for c in name):
        name = " ".join(w[:1].upper() + w[1:] for w in name.split())
    return name


_NOT_A_NAME = re.compile(r"\b(system|admin|administrator|override|ignore|instruction|prompt|developer|assistant)\b",
                         re.IGNORECASE)


# Deterministic guards on top of the listener's judgment.
CALL_REQUEST = re.compile(r"\b(call me|give me a call|ring me|phone me)\b(\s+(now|again|instead|back|please|pls|asap))*"
                          r"\s*[?!.]*\s*$|\bcan you (just )?(call|ring|phone) me\b", re.IGNORECASE)
NOT_A_TASK = re.compile(r"^\s*(call|phone|ring)( me)?\b|\btext(ing)? me\b|\bset ?up\b|\bonboarding\b|\bthe app\b"
                        r"|\bstress(ed)?\b|\banxious\b", re.IGNORECASE)
ASKS_CAPABILITIES = re.compile(r"what (can|do|else can) you (do|help)|what are you (for|good at)|how can you help"
                               r"|what do you do\b", re.IGNORECASE)


def plausible_name(name: str | None) -> bool:
    """Short, a few words, and not an instruction smuggled in as a name."""
    return bool(name) and len(name) <= 24 and len(name.split()) <= 3 and not _NOT_A_NAME.search(name)


# ── item state ──────────────────────────────────────────────────
def gmail_status(s: Session) -> str:
    return (s.mail or {}).get("status", "none")  # none | connected | declined


def status(s: Session, item: str) -> str:
    ob = s.ob["status"]
    if item == "agent_name":
        return "done" if s.agent_name else "open"
    if item == "user_name":
        return "done" if s.user_name else ob["user_name"]          # open | declined
    if item == "first_action":
        return "done" if s.help_need else ob["first_action"]       # open | none (no idea)
    if item == "gmail":
        return {"connected": "done", "declined": "declined"}.get(gmail_status(s), "open")
    return ob["call"]                                              # open | accepted | declined


def askable(s: Session, item: str) -> bool:
    return status(s, item) == "open" and s.ob["asks"][item] < MAX_ASKS[item]


def onboarding_open(s: Session) -> bool:
    if s.ob.get("complete") or s.ob.get("ready"):  # ready: every question asked; waiting on a first task
        return False
    return any(askable(s, item) for item in ORDER) or not s.agent_name


async def complete(s: Session, kind: str, task: str | None = None) -> None:
    """Onboarding is over: Persona hands off to its real functions. Marked in the thread so it's
    clear (to the user, and to whoever's evaluating) where onboarding ends."""
    if s.ob.get("complete"):
        return
    s.ob.update(complete=True, task=task, pending=None)
    s.changed()
    detail = {"email": f" · working on: {task}", "other": f" · handed off: {task}",
              "task": f" · first task: {task}"}.get(kind, "") if task else ""
    logger.info(f"[{s.id[:8]}] onboarding complete ({kind}{', ' + task if task else ''})")
    await s.add_message("system", "event", f"Onboarding complete{detail}", emphasis="Onboarding complete")
    s.remember("system", "Onboarding is complete" + (f"; you're working on: {task}" if task else ""))


EMAIL_TASK_NOTE = ("Do what they asked about their email now, using the inbox tools (read_inbox for anything "
                   "that needs judgment), and text them the result: short and concrete. Only mention emails the "
                   "tools return.")


def item_of(line_key: str) -> str | None:
    asks = LINES[line_key].asks
    return "user_name" if asks == "confirm_name" else asks


def next_ask(s: Session) -> str | None:
    """The next question to ask, never the one asked last."""
    if not s.agent_name:
        return None
    last = item_of_ask(s.ob["last_ask"])
    for item in ORDER:
        if item == last or not askable(s, item):
            continue
        if item == "call":
            if s.call.status == "idle":
                return "offer_call"
            continue
        if item == "user_name":
            confirm = gmail_status(s) == "connected" and not s.ob.get("confirm_asked")
            return "confirm_email_name" if confirm else "ask_user_name"
        if item == "first_action":
            return "ask_help"
        if item == "gmail":
            return "gmail_for_ideas" if status(s, "first_action") == "none" else "gmail_offer"
    return None


def item_of_ask(ask: str | None) -> str | None:
    return "user_name" if ask == "confirm_name" else ask


def note_sent(s: Session, line_key: str) -> None:
    """Bookkeeping once a line has gone out."""
    asks = LINES[line_key].asks
    if asks:
        s.ob["asks"][item_of(line_key)] = s.ob["asks"].get(item_of(line_key), 0) + 1
        s.ob["pending"] = asks
        s.ob["last_ask"] = asks
        s.ob["last_ask_line"] = line_key
        if asks == "confirm_name":
            s.ob["confirm_asked"] = True
    s.changed()


# ── planning a reply ────────────────────────────────────────────
@dataclass
class Plan:
    beats: list[str] = field(default_factory=list)  # approved lines, in order
    answer: bool = False  # the chat model writes a reply first (before the beats)
    lead: list[str] = field(default_factory=list)  # lines sent before the model's reply
    note: str = ""
    complete: tuple[str, str | None] | None = None  # (kind, task): mark onboarding done after sending


async def set_default_name(s: Session, why: str) -> None:
    cancel_name_timer(s)
    await s.set_agent_name(DEFAULT_AGENT_NAME)
    s.remember("system", f"{why}, so you go by {DEFAULT_AGENT_NAME} for now (they can rename you anytime)")


async def plan(s: Session, heard) -> Plan:
    """What happened in the user's message → state changes → the beats to send."""
    from . import mail  # avoid an import cycle

    ob, p = s.ob, Plan()
    pending, ob["pending"] = ob["pending"], None
    ob["paused"] = False  # any message from them resumes things
    if heard.asks_capabilities and not ASKS_CAPABILITIES.search(heard.raw or ""):
        heard.asks_capabilities, heard.needs_answer = False, True  # "are you real?" deserves a real answer
    if heard.call == "none" and CALL_REQUEST.search(heard.raw or ""):
        heard.call = "yes"  # "can you just call me?" is a call request, whatever else it looks like
    if heard.help_need and (heard.venting or NOT_A_TASK.search(heard.help_need)):
        heard.help_need = None  # venting, "call me", "skip setup" aren't things to work on...
        heard.needs_answer = heard.call != "yes"  # ...but they deserve a real reply (unless it's a call request)
    if heard.language:
        ob["lang"] = heard.language

    # They want quiet: say so once, then wait until they text again.
    if heard.stop:
        ob.update(paused=True, retry_call=False)
        s.remember("system", "They asked you to stop messaging: don't message them until they text again")
        p.beats.append("leave_be")
        return _done(s, p)

    # They missed the question: ask it again (they asked for it, so it's not a repeat).
    if heard.repeat_request and pending and ob.get("last_ask_line"):
        ob["asks"][item_of(ob["last_ask_line"])] -= 1  # doesn't use up an ask
        p.beats.append(ob["last_ask_line"])
        return _done(s, p)

    # 1 · Persona's name: a plausible name any time is a name or a rename
    name = tidy_name(heard.assistant_name) if heard.assistant_name else None
    if name and plausible_name(name) and name != s.agent_name:
        first = s.agent_name is None
        cancel_name_timer(s)
        await s.set_agent_name(name)
        s.remember("system", f"The user {'named' if first else 'renamed'} you {name}")
        p.beats.append("named" if first else "renamed")
    elif not s.agent_name and pending == "agent_name":
        if heard.just_greeting and askable(s, "agent_name"):
            p.beats.append("greet_ask_name")  # "hey!" isn't an answer: greet back and ask once more
        else:  # a question, "you pick", junk: default (never forced)
            await set_default_name(s, "They didn't give you a name")
            p.beats.append("default_named")

    if pending == "contact" and heard.call != "yes":
        ob["retry_call"] = False  # they moved on; they can still ask to be called any time

    # the call: a request to be called always wins
    if heard.call == "yes" and s.call.status == "idle":
        ob["status"]["call"] = "accepted"
        p.beats.append("calling_now")
    elif heard.call == "no" and status(s, "call") == "open":
        ob["status"]["call"] = "declined"
        ob["retry_call"] = False
        s.remember("system", "They'd rather not have a call (call only if they ask)")
        p.beats.append("keep_texting")

    # 2 · their name (corrections welcome)
    user = tidy_name(heard.user_name) if heard.user_name else None
    if user and plausible_name(user) and user != s.user_name:
        first = not s.user_name
        s.set_user_name(user)
        ob["status"]["user_name"] = "open"
        p.beats.append("nice_to_meet" if first else "name_fixed")
    elif heard.user_name_declined and status(s, "user_name") == "open":
        ob["status"]["user_name"] = "declined"
        s.remember("system", "They'd rather not share their name: never use one")
        p.beats.append("no_name_ok")
    elif pending == "confirm_name" and heard.name_confirmed == "yes":
        s.set_user_name(INBOX_OWNER)
        p.beats.append("nice_to_meet")

    # 4 · first action (changing their mind is fine)
    new_task = None
    if heard.help_need and heard.help_need != s.help_need:
        s.set_help_need(heard.help_need)
        ob["help_is_email"] = heard.help_is_email
        new_task = heard.help_need
    elif heard.no_idea and pending == "first_action" and status(s, "first_action") == "open":
        ob["status"]["first_action"] = "none"
        s.remember("system", "They don't have anything in mind yet")
    if heard.asks_capabilities and not s.help_need:
        p.beats.append("capabilities")  # the scripted answer to "what can you do?"

    # 3 · gmail
    if heard.gmail == "yes" and gmail_status(s) != "connected":
        # the link may already be out (it comes with gmail_for_ideas): point at it, don't resend
        p.beats.append("tap_link" if ob.get("last_ask_line") == "gmail_for_ideas" else "gmail_link")
    elif heard.gmail == "no" and gmail_status(s) == "none":
        await mail.deny(s)
        p.beats.append("gmail_declined")

    questions_done = s.agent_name and not any(askable(s, i) for i in ORDER)
    if new_task and not ob.get("complete"):
        if ob["help_is_email"] and gmail_status(s) == "connected":
            p.answer, p.note = True, EMAIL_TASK_NOTE  # an email task, connected: do it. That's the first action.
            p.complete = ("email", new_task)
        elif ob["help_is_email"] and askable(s, "gmail") and "gmail_link" not in p.beats:
            p.beats.append("gmail_offer")  # email: connecting Gmail comes first
        elif questions_done:
            p.beats.append("on_it")  # set up already: this is the first real task
            p.complete = ("other", new_task)
        else:
            p.beats.append("noted_task")  # honest: noted for later, and onboarding carries on

    # then the next question, unless this reply already asks one, waits on an action, or it's all done
    waiting = any(LINES[b].action in ("ring", "gmail_card") or b == "tap_link" for b in p.beats)
    if not waiting and not any(LINES[b].asks for b in p.beats) and not ob.get("complete") and not p.complete:
        nxt = "ask_user_name" if pending == "confirm_name" and heard.name_confirmed == "no" else next_ask(s)
        if nxt:
            p.beats.append(nxt)
        elif questions_done and s.help_need and not new_task:
            # Every question's been asked and a task was noted earlier: now it's the first action.
            p.beats.append("on_it")
            p.complete = ("other", s.help_need)
        elif questions_done and not ob.get("ready"):
            # Nothing left to ask and no task yet: say so once. "Complete" waits for a real first task.
            p.beats.append("all_set")
            ob["ready"] = True

    capabilities_answered = heard.asks_capabilities and "capabilities" in p.beats
    calling = heard.call == "yes" and "calling_now" in p.beats  # "calling you now" is the answer
    no_idea = heard.no_idea and "gmail_for_ideas" in p.beats    # so is "no worries, connect gmail…"
    p.answer = p.answer or (heard.needs_answer and not capabilities_answered and not calling and not no_idea) \
        or not p.beats
    return _done(s, p)


def _done(s: Session, p: Plan) -> Plan:
    s.changed()
    logger.info(f"[{s.id[:8]}] plan: answer={p.answer} beats={p.beats}")
    return p


async def absorb(s: Session, heard) -> None:
    """Apply facts heard on a call (no beats: the call already responded to them)."""
    from . import mail  # avoid an import cycle

    name = tidy_name(heard.assistant_name) if heard.assistant_name else None
    if name and plausible_name(name) and name != s.agent_name:
        await s.set_agent_name(name)
    user = tidy_name(heard.user_name) if heard.user_name else None
    if user and plausible_name(user) and user != s.user_name:
        s.set_user_name(user)
    elif heard.name_confirmed == "yes" and not s.user_name:
        s.set_user_name(INBOX_OWNER)
    elif heard.user_name_declined and status(s, "user_name") == "open":
        s.ob["status"]["user_name"] = "declined"
    if heard.help_need and heard.help_need != s.help_need:
        s.set_help_need(heard.help_need)
        s.ob["help_is_email"] = heard.help_is_email
    elif heard.no_idea and status(s, "first_action") == "open":
        s.ob["status"]["first_action"] = "none"
    if heard.gmail == "no" and gmail_status(s) == "none":
        await mail.deny(s)
    s.changed()


def call_event_beats(s: Session, kind: str) -> list[str]:
    """What Persona texts after a call event: an acknowledgement, then the next ask."""
    lead = {"declined": "after_decline", "missed": "after_missed", "failed": "call_failed",
            "blocked": "call_blocked", "blocked_again": "call_blocked_again", "ended": "after_call", "cancelled": "after_cancel",
            "dropped": "after_drop", "short": "after_short", "silence": "after_silence"}[kind]
    beats = [lead]
    if kind != "blocked":  # blocked waits for the contact to be saved
        nxt = next_ask(s)
        if nxt:
            beats.append(nxt)
    return beats


# ── the name timeout ────────────────────────────────────────────
def arm_name_timer(session: Session) -> None:
    """(Re)start the countdown to the default name. Any activity pushes it back."""
    if session.agent_name:
        return
    cancel_name_timer(session)
    session.name_timer = asyncio.create_task(_name_timeout(session))


def cancel_name_timer(session: Session) -> None:
    t = session.name_timer
    if t and not t.done() and t is not asyncio.current_task():
        t.cancel()
    session.name_timer = None


def touch(session: Session) -> None:
    """The user is active (e.g. typing): keep the countdown from firing under them."""
    if session.name_timer and not session.name_timer.done():
        arm_name_timer(session)


async def _name_timeout(session: Session) -> None:
    from . import text_agent  # avoid an import cycle

    try:
        await asyncio.sleep(config.settings().name_timeout_secs)
    except asyncio.CancelledError:
        return
    if session.agent_name:
        return
    if session.typing or (session.reply_task and not session.reply_task.done()):
        session.name_timer = None
        arm_name_timer(session)  # the agent is mid-reply; check again after
        return
    session.name_timer = None
    session.ob["pending"] = None
    await set_default_name(session, "They didn't pick a name")
    beats = ["default_named"]
    nxt = next_ask(session)
    if nxt:
        beats.append(nxt)
    text_agent.send_beats(session, beats)


# ── the stall loop ──────────────────────────────────────────────
STALL_SECS = 6.0
TYPING_GRACE_SECS = 4.0
MAX_NUDGES = 1  # double texts per silence


def waiting_on_user(s: Session) -> bool:
    """True if the ball is in the user's court."""
    if s.ob.get("paused") or s.call.status != "idle" or s.ob["pending"] or s.ob["retry_call"]:
        return True  # a call is on, a question is out, or a blocked call waits on the contact
    last = next((m for m in reversed(s.messages) if m.kind != "event"), None)
    if last is None or last.sender == "user":
        return True
    return last.kind == "card" and gmail_status(s) != "connected"  # the Gmail link is out


def after_agent_turn(session: Session) -> None:
    """Called after the agent finishes a turn: check back shortly for a stall."""
    if session.stall_task and not session.stall_task.done() and session.stall_task is not asyncio.current_task():
        session.stall_task.cancel()
    session.stall_task = asyncio.create_task(_stall_check(session))


def user_spoke(session: Session) -> None:
    session.nudges_since_user = 0
    t = session.call_retry_task  # their reply decides what happens next (e.g. "done" → call now)
    if t and not t.done():
        t.cancel()
    if session.stall_task and not session.stall_task.done():
        session.stall_task.cancel()


async def _stall_check(session: Session) -> None:
    from . import text_agent  # avoid an import cycle

    try:
        await asyncio.sleep(STALL_SECS)
        while time.time() - session.user_typing_at < TYPING_GRACE_SECS:
            await asyncio.sleep(1.0)  # they're typing: let them finish
    except asyncio.CancelledError:
        return
    busy = session.typing or (session.reply_task and not session.reply_task.done())
    if busy or waiting_on_user(session) or not onboarding_open(session):
        return
    if session.nudges_since_user >= MAX_NUDGES:
        return
    nxt = next_ask(session)
    if not nxt:
        return
    session.nudges_since_user += 1
    logger.info(f"[{session.id[:8]}] conversation stalled; double-texting {nxt}")
    text_agent.send_beats(session, [nxt])
