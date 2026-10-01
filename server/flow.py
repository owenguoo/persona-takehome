"""The scripted spine of onboarding; the models handle the wording.

  1. Persona texts first and asks what to call it.
  2. No name within a while (or the user won't pick) → "Your Persona".
  3. Offer a quick call; learn their name and what they need; offer Gmail.

After every agent turn a small loop asks: are we WAITING on the user (the agent
asked something, a link is out, a call is ringing), or has it STALLED (the agent
answered without handing the turn back and onboarding isn't done)? If stalled,
the agent double-texts the next step, once.
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from . import config
from .session import Session

DEFAULT_AGENT_NAME = "Your Persona"


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
    try:
        await asyncio.sleep(config.settings().name_timeout_secs)
    except asyncio.CancelledError:
        return
    if session.agent_name:
        return
    busy = session.typing or (session.reply_task and not session.reply_task.done())
    if busy:  # the agent is mid-reply; check again after it's done
        session.name_timer = None
        arm_name_timer(session)
        return
    session.name_timer = None
    await use_default_name(session, "The user didn't pick a name")


async def use_default_name(session: Session, why: str) -> None:
    from . import text_agent  # avoid an import cycle

    cancel_name_timer(session)
    if session.agent_name:
        return
    logger.info(f"[{session.id[:8]}] defaulting agent name ({why})")
    await session.set_agent_name(DEFAULT_AGENT_NAME)
    session.remember(
        "system",
        f"{why}, so you're going by {DEFAULT_AGENT_NAME} for now. Say so in one short, light line "
        "(they can rename you anytime), then move on",
    )
    text_agent.schedule_reply(session, delay=0.6)


async def apply_heard(session: Session, heard) -> None:
    """Update onboarding state from the listener (before the chat model replies)."""
    from .text_agent import tidy_name  # avoid an import cycle

    name = tidy_name(heard.assistant_name) if heard.assistant_name else None
    if name and name != session.agent_name:  # a first name or a rename
        renamed = session.agent_name is not None
        cancel_name_timer(session)
        await session.set_agent_name(name)
        session.remember("system", f"The user {'renamed' if renamed else 'named'} you {name}")
    elif not session.agent_name:
        if heard.assistant_name_declined:
            cancel_name_timer(session)
            await session.set_agent_name(DEFAULT_AGENT_NAME)
            session.remember("system", f"They didn't want to pick, so you go by {DEFAULT_AGENT_NAME} for now")
    if heard.user_name and not session.user_name:
        session.set_user_name(tidy_name(heard.user_name))
    if heard.help_need and not session.help_need:
        session.set_help_need(heard.help_need)
    if heard.declines_call and not session.call_offer_done:
        session.call_offer_done = True
        session.changed()
        session.remember("system", "They'd rather not have a call right now: don't offer one (but call if they ask)")


# ── the stall loop ──────────────────────────────────────────────
STALL_SECS = 6.0
TYPING_GRACE_SECS = 4.0
MAX_NUDGES = 1  # double texts per silence


def onboarding_open(session: Session) -> bool:
    gmail = (session.mail or {}).get("status", "none")
    return not (session.agent_name and session.user_name and session.help_need
                and session.call_offer_done and gmail != "none")


def waiting_on_user(session: Session) -> bool:
    """True if the ball is in the user's court."""
    if session.call.status != "idle" or not session.agent_name:
        return True  # a call is ringing/live, or the opener's name question is open
    last = next((m for m in reversed(session.messages) if m.kind != "event"), None)
    if last is None or last.sender == "user":
        return True  # nothing said yet, or a reply is on its way
    if last.kind == "card":
        return (session.mail or {}).get("status") != "connected"  # link out, not tapped yet
    return last.kind == "text" and last.text.rstrip().endswith("?")


def after_agent_turn(session: Session) -> None:
    """Called after the agent finishes a turn: check back shortly for a stall."""
    if session.stall_task and not session.stall_task.done() and session.stall_task is not asyncio.current_task():
        session.stall_task.cancel()
    session.stall_task = asyncio.create_task(_stall_check(session))


def user_spoke(session: Session) -> None:
    session.nudges_since_user = 0
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
    if busy or not onboarding_open(session) or waiting_on_user(session):
        return
    if session.nudges_since_user >= MAX_NUDGES:
        return
    session.nudges_since_user += 1
    logger.info(f"[{session.id[:8]}] conversation stalled; double-texting")
    session.remember(
        "system",
        "The conversation stalled: your last message didn't hand the turn back and onboarding isn't done. "
        "Double-text ONE short line that moves to the next missing step (prefer the matching line). "
        "Don't apologise or recap.",
    )
    text_agent.schedule_reply(session, delay=0)
