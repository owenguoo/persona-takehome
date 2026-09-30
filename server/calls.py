"""Call lifecycle: idle → ringing → connecting → active → idle.

Every exit path (hang up, decline, missed, mic blocked, tab closed, pipeline
crash) funnels into one place and is idempotent, so the thread always gets
exactly one call record and the agent follows up over text.
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from . import text_agent
from .session import Session

RING_TIMEOUT_SECS = 25
CONNECT_TIMEOUT_SECS = 30  # covers the browser's microphone prompt


def _fmt(secs: float) -> str:
    s = max(0, int(secs))
    return f"{s // 60}:{s % 60:02d}"


def _cancel(task: asyncio.Task | None) -> None:
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def ring(session: Session) -> bool:
    """The agent calls the user."""
    c = session.call
    if c.status != "idle":
        return False
    c.generation += 1
    c.status, c.direction, c.started_at, c.caption = "ringing", "incoming", None, ""
    session.remember("system", "You started ringing the user's phone")
    await session.emit_call()
    c.ring_task = asyncio.create_task(_ring_timeout(session, c.generation))
    return True


async def _ring_timeout(session: Session, gen: int) -> None:
    await asyncio.sleep(RING_TIMEOUT_SECS)
    if session.call.generation == gen and session.call.status == "ringing":
        await _not_answered(session, "missed", "The user didn't pick up the call")


async def accept(session: Session, page: str | None = None) -> None:
    """The user answered, or tapped call themselves. Audio arrives via /api/offer."""
    c = session.call
    if c.status == "ringing":
        _cancel(c.ring_task)
    elif c.status == "idle":
        c.generation += 1
        c.direction, c.caption = "outgoing", ""
        session.remember("system", "The user started calling you")
    else:
        return
    c.status, c.owner = "connecting", page
    await session.emit_call()
    _cancel(c.connect_task)
    c.connect_task = asyncio.create_task(_connect_timeout(session, c.generation))


async def _connect_timeout(session: Session, gen: int) -> None:
    await asyncio.sleep(CONNECT_TIMEOUT_SECS)
    if session.call.generation == gen and session.call.status == "connecting":
        await fail(session, "timeout")


async def decline(session: Session, text_instead: bool = False) -> None:
    if session.call.status != "ringing":
        return
    note = "The user declined the call" + (" and said they'd rather text" if text_instead else "")
    await _not_answered(session, "declined", note)


async def _not_answered(session: Session, status: str, note: str) -> None:
    c = session.call
    _cancel(c.ring_task)
    c.status = "idle"
    await session.emit_call()
    await session.add_message("agent", "call", status=status)
    session.remember("system", note)
    text_agent.schedule_reply(session, delay=1.2)


async def fail(session: Session, reason: str) -> None:
    """The call never connected (mic blocked, WebRTC failure, timeout)."""
    c = session.call
    if c.status not in ("ringing", "connecting"):
        return
    _cancel(c.ring_task)
    _cancel(c.connect_task)
    c.status = "idle"
    await session.emit_call()
    why = {
        "mic": "their microphone is blocked",
        "timeout": "it never connected",
        "no_key": "voice isn't configured yet",
    }.get(reason, "of a connection problem")
    await session.add_message("system", "event", f"Call couldn't connect: {why}")
    session.remember("system", f"The call couldn't connect because {why}")
    text_agent.schedule_reply(session, delay=1.0)


async def connected(session: Session) -> None:
    """Called by the voice pipeline once audio is flowing."""
    c = session.call
    if c.status != "connecting":
        return
    _cancel(c.connect_task)
    c.status, c.started_at = "active", time.time()
    session.remember("system", "Phone call connected")
    await session.emit_call()


PAGE_GRACE_SECS = 4


async def owner_left(session: Session, page: str) -> None:
    """The page holding the call's audio disconnected. Give it a moment to come back
    (a network blip) before treating it as gone (tab closed or reloaded)."""
    c = session.call
    if c.owner != page or c.status not in ("connecting", "active"):
        return
    gen = c.generation
    await asyncio.sleep(PAGE_GRACE_SECS)
    if c.generation == gen and c.owner == page and not session.has_page(page):
        await hangup(session, "page closed")


async def hangup(session: Session, reason: str = "user") -> None:
    """End a live call from our side (user tapped end, or their page went away)."""
    c = session.call
    if c.status not in ("connecting", "active"):
        return
    logger.info(f"[{session.id[:8]}] hangup ({reason})")
    if c.hangup:
        try:
            await c.hangup()
        except Exception:
            logger.exception("voice hangup failed")
    await finished(session, c.generation)


async def finished(session: Session, gen: int, error: str | None = None) -> None:
    """Final step for any call that got past ringing. Safe to call repeatedly."""
    c = session.call
    if c.generation != gen or c.status not in ("connecting", "active"):
        return
    was_active = c.status == "active"
    _cancel(c.connect_task)
    duration = time.time() - (c.started_at or time.time())
    c.status, c.hangup, c.caption, c.owner = "idle", None, "", None
    await session.emit_call()
    if was_active:
        await session.add_message("agent", "call", status="ended", duration=duration)
    if error:
        await session.add_message("system", "event", f"Call dropped: {error}")
        session.remember("system", f"The call dropped after {_fmt(duration)} because {error}. Continue over text")
    elif was_active:
        session.remember("system", f"Phone call ended after {_fmt(duration)}. Continue over text")
    else:
        await session.add_message("system", "event", "Call couldn't connect")
        session.remember("system", "The call dropped before it connected")
    text_agent.schedule_reply(session, delay=1.4 if was_active else 1.0)
