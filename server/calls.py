"""Call lifecycle: idle → ringing → connecting → active → idle.

Calls only reach the user once Persona's contact card is saved (like an iPhone
silencing unknown callers); otherwise the attempt is recorded and Persona asks
them to save it, then retries.

Every exit path (hang up, decline, missed, mic blocked, tab closed, pipeline
crash) funnels into one place and is idempotent, so the thread always gets
exactly one call record and Persona follows up over text.
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from . import extract, flow, text_agent
from .session import Session

RING_TIMEOUT_SECS = 25
CONNECT_TIMEOUT_SECS = 30  # covers the browser's microphone prompt


def _fmt(secs: float) -> str:
    s = max(0, int(secs))
    return f"{s // 60}:{s % 60:02d}"


def _offer_done(session: Session) -> None:
    if session.ob["status"]["call"] == "open":
        session.ob["status"]["call"] = "accepted"
        session.changed()


def _follow_up(session: Session, kind: str) -> None:
    text_agent.send_beats(session, flow.call_event_beats(session, kind))


def _cancel(task: asyncio.Task | None) -> None:
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def ring(session: Session) -> bool:
    """Persona calls the user."""
    c = session.call
    if c.status != "idle":
        return False
    if not session.ob["contact_saved"]:
        await _blocked(session)
        return False
    session.ob["retry_call"] = False
    c.generation += 1
    c.status, c.direction, c.started_at, c.caption = "ringing", "incoming", None, ""
    _offer_done(session)
    session.remember("system", "You started ringing the user's phone")
    await session.emit_call()
    c.ring_task = asyncio.create_task(_ring_timeout(session, c.generation))
    return True


async def _blocked(session: Session) -> None:
    """Their phone silenced the call: Persona isn't a saved contact yet."""
    _offer_done(session)
    session.ob["retry_call"] = True
    session.changed()
    name = session.agent_name or "Persona"
    logger.info(f"[{session.id[:8]}] call blocked: contact not saved")
    await session.add_message("system", "event", f"Call from {name} didn't go through: not in your contacts",
                              emphasis="didn't go through")
    session.remember("system", "Your call didn't go through: they haven't saved your contact card yet")
    _follow_up(session, "blocked")


async def contact_saved(session: Session) -> None:
    """They tapped Add on Persona's contact card. Retry a call that was blocked."""
    if session.ob["contact_saved"]:
        return
    session.ob["contact_saved"] = True
    session.changed()
    name = session.agent_name or "Persona"
    await session.emit("contact_saved")
    await session.add_message("system", "event", f"Contact saved as {name}", emphasis=name)
    session.remember("system", "They saved your contact card")
    if session.ob["retry_call"] and session.call.status == "idle":
        text_agent.send_beats(session, ["calling_now"])  # "i'll try again"


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
        _offer_done(session)
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
    _follow_up(session, status)


async def fail(session: Session, reason: str) -> None:
    """The call never connected (mic blocked, WebRTC failure, timeout)."""
    c = session.call
    if c.status not in ("ringing", "connecting"):
        return
    _cancel(c.ring_task)
    _cancel(c.connect_task)
    c.status = "idle"
    await session.emit_call()
    # (what the user sees, what the agent is told)
    shown, why = {
        "mic": ("Call couldn't connect: microphone access is blocked", "their microphone is blocked"),
        "timeout": ("Call couldn't connect", "it never connected"),
        "no_key": ("Call couldn't connect: voice isn't set up yet", "voice isn't configured yet"),
    }.get(reason, ("Call couldn't connect", "of a connection problem"))
    await session.add_message("system", "event", shown)
    session.remember("system", f"The call couldn't connect because {why}")
    _follow_up(session, "failed")


async def _endgame(session: Session, handoff: dict) -> None:
    """Persona hung up to go do something: onboarding is over, real work starts."""
    from . import mail  # avoid an import cycle

    kind, task = handoff["kind"], handoff.get("task") or session.help_need
    if kind == "email" and mail.connected(session):
        text_agent.send_beats(session, [], answer=True, note=f"On the call they asked: {task}. {flow.EMAIL_TASK_NOTE}",
                              complete=("email", task))
    elif kind == "email":  # it never connected: back to text for Gmail
        _follow_up(session, "ended")
    else:
        await flow.complete(session, kind, task if kind == "other" else None)


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
    c_started = c.started_at
    c.status, c.hangup, c.caption, c.owner = "idle", None, "", None
    c.inject, c.note, c.speaking = None, None, False
    await session.emit_call()
    if was_active:
        await session.add_message("agent", "call", status="ended", duration=duration)
    if error:
        await session.add_message("system", "event", f"Call dropped: {error}")
        session.remember("system", f"The call dropped after {_fmt(duration)} because {error}")
    elif not was_active:
        await session.add_message("system", "event", "Call couldn't connect")
        session.remember("system", "The call dropped before it connected")
    if not was_active:
        _follow_up(session, "failed")
        return

    # What did we learn on the call? Read the whole transcript, both sides.
    agenda_open = [i for i in ("user_name", "first_action", "gmail") if flow.askable(session, i)]
    transcript = [f"{'USER' if t.role == 'user' else 'ASSISTANT'}: {t.content}" for t in session.history
                  if t.channel == "voice" and t.role in ("user", "assistant") and t.at >= (c_started or 0)]
    if transcript:
        await flow.absorb(session, await extract.listen(session, transcript, transcript=True))
    for item in agenda_open:  # it came up (or could have) on the call: one text follow-up left, at most
        session.ob["asks"][item] = max(session.ob["asks"][item], 1)
    session.ob["pending"] = None
    session.changed()
    session.remember("system", f"Phone call ended after {_fmt(duration)}")
    handoff = session.ob.pop("handoff", None)
    if error:
        _follow_up(session, "failed")
    elif handoff and not session.ob.get("complete"):
        await _endgame(session, handoff)
    elif flow.onboarding_open(session):
        _follow_up(session, "ended")
    else:  # nothing left to ask: a short, specific follow-up on the call
        text_agent.send_beats(session, [], answer=True,
                              note="The call just ended. Follow up in one short text on what you talked about. "
                                   "Don't recap it or repeat anything you texted during it.")
