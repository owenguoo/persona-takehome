"""One onboarding session, shared by every channel (text thread + voice call).

`messages` is what the iMessage thread shows. `history` is what the models see:
it also carries voice-call turns and system notes, which never become bubbles.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from fastapi import WebSocket
from loguru import logger

Sender = Literal["agent", "user", "system"]
Kind = Literal["text", "call", "card", "event"]
CallStatus = Literal["idle", "ringing", "connecting", "active"]


@dataclass
class Message:
    id: int
    sender: Sender
    kind: Kind
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    read_at: float | None = None


@dataclass
class Turn:
    role: Literal["user", "assistant", "system"]
    content: str
    channel: Literal["text", "voice"]
    at: float = field(default_factory=time.time)


@dataclass
class Call:
    status: CallStatus = "idle"
    direction: Literal["incoming", "outgoing"] = "incoming"  # from the user's point of view
    started_at: float | None = None
    generation: int = 0  # bumps per call so late events from an old call are ignored
    ring_task: asyncio.Task | None = None
    connect_task: asyncio.Task | None = None
    voice_task: asyncio.Task | None = None
    hangup: Any = None  # async callable set by the voice pipeline
    caption: str = ""


class Session:
    def __init__(self, sid: str):
        self.id = sid
        self.created_at = time.time()
        self.agent_name: str | None = None
        self.messages: list[Message] = []
        self.history: list[Turn] = []
        self.call = Call()
        self.typing = False
        self.sockets: set[WebSocket] = set()
        self.reply_task: asyncio.Task | None = None
        self._ids = itertools.count(1)

    # ── outbound ────────────────────────────────────────────────
    async def emit(self, type: str, **data: Any) -> None:
        payload = {"type": type, **data}
        dead = []
        for ws in list(self.sockets):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    def call_json(self) -> dict[str, Any]:
        c = self.call
        return {"status": c.status, "direction": c.direction, "started_at": c.started_at}

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "agent_name": self.agent_name,
            "messages": [asdict(m) for m in self.messages],
            "typing": self.typing,
            "call": self.call_json(),
        }

    # ── thread ──────────────────────────────────────────────────
    async def add_message(self, sender: Sender, kind: Kind = "text", text: str = "", **meta: Any) -> Message:
        msg = Message(id=next(self._ids), sender=sender, kind=kind, text=text, meta=meta)
        self.messages.append(msg)
        await self.emit("message", message=asdict(msg))
        return msg

    async def set_typing(self, on: bool) -> None:
        if self.typing != on:
            self.typing = on
            await self.emit("typing", on=on)

    async def mark_read(self) -> None:
        now = time.time()
        changed = [m for m in self.messages if m.sender == "user" and m.read_at is None]
        for m in changed:
            m.read_at = now
        if changed:
            await self.emit("read", ids=[m.id for m in changed], at=now)

    async def set_agent_name(self, name: str) -> None:
        self.agent_name = name
        await self.emit("contact", agent_name=name)
        await self.add_message("system", "event", f"Contact saved as {name}", emphasis=name)

    async def emit_call(self) -> None:
        await self.emit("call", call=self.call_json())

    # ── model memory ────────────────────────────────────────────
    def remember(self, role: Literal["user", "assistant", "system"], content: str, channel: Literal["text", "voice"] = "text") -> None:
        content = content.strip()
        if content:
            self.history.append(Turn(role, content, channel))
            logger.debug(f"[{self.id[:8]}] {channel}/{role}: {content}")


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def get(self, sid: str) -> Session | None:
        return self._sessions.get(sid)

    def get_or_create(self, sid: str) -> tuple[Session, bool]:
        s = self._sessions.get(sid)
        if s:
            return s, False
        s = self._sessions[sid] = Session(sid)
        return s, True


store = SessionStore()
