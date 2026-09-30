"""The scripted spine of onboarding; the models handle the wording.

  1. Persona texts first and asks what to call it.
  2. No name within a while (or the user won't pick) → "Your Persona".
  3. Offer a quick call. Everything after that is open conversation.
"""
from __future__ import annotations

import asyncio

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
