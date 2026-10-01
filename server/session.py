"""One onboarding session, shared by every channel (text thread + voice call).

`messages` is what the iMessage thread shows. `history` is what the models see:
it also carries voice-call turns and system notes, which never become bubbles.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
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
    owner: str | None = None  # page id holding the call's audio
    note: Any = None  # async callable: add silent context to the live call (no reply)
    speaking: bool = False  # the agent is talking right now
    inject: Any = None  # async callable: tell the live call something (set by the voice pipeline)


class Session:
    def __init__(self, sid: str):
        self.id = sid
        self.created_at = time.time()
        self.agent_name: str | None = None
        self.user_name: str | None = None
        self.help_need: str | None = None  # what they want a hand with, in their words
        self.mail: dict[str, Any] = {}  # sandbox Gmail (see mail.py)
        self.call_offer_done = False  # a call happened, or the user said they'd rather text
        self.call_outcome: str | None = None  # "declined" | "missed" | "failed", until the agent has replied
        self.name_timer: asyncio.Task | None = None
        self.messages: list[Message] = []
        self.history: list[Turn] = []
        self.call = Call()
        self.typing = False
        self.sockets: dict[WebSocket, str] = {}  # socket → page id
        self.reply_task: asyncio.Task | None = None
        self.line_task: asyncio.Task | None = None  # a scripted line being sent (e.g. the opener)
        self.stall_task: asyncio.Task | None = None  # checks, after each agent turn, whether things stalled
        self.user_typing_at = 0.0
        self.nudges_since_user = 0  # double texts sent since the user last spoke
        self._ids = itertools.count(1)
        self._save_handle: asyncio.TimerHandle | None = None
        self.on_change = None  # set by the store: persists the session

    def has_page(self, page: str) -> bool:
        return page in self.sockets.values()

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
            self.sockets.pop(ws, None)

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
    def changed(self) -> None:
        if self.on_change:
            self.on_change(self)

    async def add_message(self, sender: Sender, kind: Kind = "text", text: str = "", **meta: Any) -> Message:
        msg = Message(id=next(self._ids), sender=sender, kind=kind, text=text, meta=meta)
        self.messages.append(msg)
        self.changed()
        await self.emit("message", message=asdict(msg))
        return msg

    async def add_notice(self, text: str) -> None:
        """A system note in the thread, skipped if it would repeat the last item verbatim."""
        last = self.messages[-1] if self.messages else None
        if last and last.kind == "event" and last.text == text:
            return
        await self.add_message("system", "event", text)

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
            self.changed()
            await self.emit("read", ids=[m.id for m in changed], at=now)

    async def set_agent_name(self, name: str) -> None:
        self.agent_name = name
        self.changed()
        await self.emit("contact", agent_name=name)
        await self.add_message("system", "event", f"Contact saved as {name}", emphasis=name)

    def set_user_name(self, name: str) -> bool:
        if not name or name == self.user_name:
            return False
        self.user_name = name
        self.changed()
        self.remember("system", f"The user's name is {name}")
        return True

    def set_help_need(self, need: str) -> bool:
        if not need or need == self.help_need:
            return False
        self.help_need = need
        self.changed()
        self.remember("system", f"They want help with: {need}")
        return True

    async def emit_call(self) -> None:
        await self.emit("call", call=self.call_json())

    # ── model memory ────────────────────────────────────────────
    def remember(self, role: Literal["user", "assistant", "system"], content: str, channel: Literal["text", "voice"] = "text") -> None:
        content = content.strip()
        if content:
            self.history.append(Turn(role, content, channel))
            self.changed()
            logger.debug(f"[{self.id[:8]}] {channel}/{role}: {content}")


    # ── persistence ─────────────────────────────────────────────
    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "agent_name": self.agent_name,
            "user_name": self.user_name,
            "help_need": self.help_need,
            "mail": self.mail,
            "call_offer_done": self.call_offer_done,
            "messages": [asdict(m) for m in self.messages],
            "history": [asdict(t) for t in self.history],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Session:
        s = cls(data["id"])
        s.created_at = data["created_at"]
        s.agent_name = data.get("agent_name")
        s.user_name = data.get("user_name")
        s.help_need = data.get("help_need")
        s.mail = data.get("mail") or {}
        s.call_offer_done = bool(data.get("call_offer_done", False))
        s.messages = [Message(**m) for m in data.get("messages", [])]
        s.history = [Turn(**t) for t in data.get("history", [])]
        s._ids = itertools.count(max((m.id for m in s.messages), default=0) + 1)
        return s


SID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class SessionStore:
    """In-memory sessions, mirrored to one JSON file each so restarts keep conversations.

    Call state is deliberately not saved: a call can't survive a server restart.
    """

    def __init__(self, directory: Path):
        self._dir = directory
        self._sessions: dict[str, Session] = {}

    @staticmethod
    def valid(sid: str) -> bool:
        return bool(SID_RE.match(sid or ""))

    def _path(self, sid: str) -> Path:
        return self._dir / f"{sid}.json"

    def get(self, sid: str) -> Session | None:
        if not self.valid(sid):
            return None
        s = self._sessions.get(sid)
        if s is None and self._path(sid).exists():
            s = self._load(sid)
        return s

    def get_or_create(self, sid: str) -> tuple[Session, bool]:
        if not self.valid(sid):
            raise ValueError("bad session id")
        s = self.get(sid)
        if s:
            return s, False
        s = self._track(Session(sid))
        self._write(s)
        return s, True

    def _load(self, sid: str) -> Session | None:
        try:
            s = Session.from_json(json.loads(self._path(sid).read_text()))
        except Exception:
            logger.exception(f"couldn't load session {sid}")
            return None
        return self._track(s)

    def _track(self, s: Session) -> Session:
        s.on_change = self._schedule_save
        self._sessions[s.id] = s
        return s

    def _schedule_save(self, s: Session) -> None:
        # Coalesce bursts of changes into one write.
        if s._save_handle is None:
            s._save_handle = asyncio.get_running_loop().call_later(0.25, self._flush, s)

    def _flush(self, s: Session) -> None:
        s._save_handle = None
        self._write(s)

    def _write(self, s: Session) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path(s.id).with_suffix(".tmp")
            tmp.write_text(json.dumps(s.to_json()))
            os.replace(tmp, self._path(s.id))
        except Exception:
            logger.exception(f"couldn't save session {s.id}")


store = SessionStore(Path(__file__).resolve().parent.parent / ".data" / "sessions")
