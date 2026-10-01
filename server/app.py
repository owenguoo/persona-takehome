"""HTTP + WebSocket entry point.

  GET  /                 the iMessage / call UI (web/)
  WS   /ws?sid=…         the thread: snapshot, then live events both ways
  POST /api/offer        WebRTC offer → answer for the voice call
  GET  /api/health       which keys/models are configured
"""
from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest, SmallWebRTCRequestHandler

from . import calls, config, flow, mail, text_agent, voice
from .session import Session, store

MAX_TEXT = 4000

app = FastAPI(title="onboarding")
webrtc = SmallWebRTCRequestHandler()


@app.middleware("http")
async def no_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
async def health():
    cfg = config.settings()
    return {
        "ok": True,
        "openai_api_key": bool(cfg.openai_api_key),
        "text_model": cfg.text_model,
        "realtime_model": cfg.realtime_model,
        "voice": cfg.voice,
    }


@app.post("/api/offer")
async def offer(body: dict):
    req = SmallWebRTCRequest.from_dict(body)
    sid = (req.request_data or {}).get("session_id")
    session = store.get(sid) if sid else None
    if not session:
        raise HTTPException(404, "unknown session")
    if not config.settings().openai_api_key:
        await calls.fail(session, "no_key")
        raise HTTPException(503, "OPENAI_API_KEY is not set")

    if session.call.status in ("idle", "ringing"):
        await calls.accept(session, (req.request_data or {}).get("page"))
    if session.call.status != "connecting":
        raise HTTPException(409, f"call is {session.call.status}")
    gen = session.call.generation

    async def on_connection(connection):
        session.call.voice_task = asyncio.create_task(voice.run_call(session, connection, gen))

    return await webrtc.handle_web_request(req, on_connection)


@app.websocket("/ws")
async def thread(ws: WebSocket, sid: str, page: str = ""):
    if not store.valid(sid):
        await ws.close(code=4400, reason="bad session id")
        return
    await ws.accept()
    session, created = store.get_or_create(sid)
    session.sockets[ws] = page
    await ws.send_json({"type": "snapshot", "session": session.snapshot()})
    if created:
        text_agent.schedule_line(session, "opener", delay=1.0)  # instant and identical every time
    if not session.agent_name:
        flow.arm_name_timer(session)
    try:
        while True:
            await handle(session, await ws.receive_json())
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket error")
    finally:
        session.sockets.pop(ws, None)
        # The call's audio lives in the page that answered it.
        if session.call.owner == page and not session.has_page(page):
            asyncio.create_task(calls.owner_left(session, page))


async def handle(session: Session, data: dict) -> None:
    kind = data.get("type")
    if kind == "user_message":
        text = str(data.get("text", "")).strip()[:MAX_TEXT]
        if text:
            await session.add_message("user", "text", text, client_id=str(data.get("client_id", ""))[:64])
            session.remember("user", text)
            flow.user_spoke(session)
            flow.arm_name_timer(session)
            text_agent.schedule_reply(session)
    elif kind == "user_typing":
        session.user_typing_at = time.time()
        flow.touch(session)
    elif kind == "call_accept":
        await calls.accept(session, str(data.get("page", "")) or None)
    elif kind == "call_decline":
        await calls.decline(session, text_instead=bool(data.get("text_instead")))
    elif kind == "call_failed":
        await calls.fail(session, str(data.get("reason", "")))
    elif kind == "hangup":
        await calls.hangup(session, "user")
    # ── shell controls ──
    elif kind == "dev_ring":
        await calls.ring(session)
    elif kind == "dev_nudge":
        session.remember("system", "Send the user a short, friendly follow-up message")
        text_agent.schedule_reply(session, delay=0.2)
    elif kind == "dev_card":
        await mail.send_link(session)


# ── simulated Gmail ───────────────────────────────────────────────
def _session(sid: str | None) -> Session:
    s = store.get(sid or "")
    if not s:
        raise HTTPException(404, "unknown session")
    return s


def _inbox_json(s: Session) -> dict:
    st = mail.state(s)
    return {"status": st["status"], "account": mail.account(s),
            "agent_name": s.agent_name or "Persona", "emails": mail.sorted_emails(s)}


@app.get("/api/mail/inbox")
async def mail_inbox(sid: str):
    return _inbox_json(_session(sid))


@app.post("/api/mail/connect")
async def mail_connect(body: dict):
    s = _session(body.get("sid"))
    await mail.connect(s)
    return _inbox_json(s)


@app.post("/api/mail/reset")
async def mail_reset(body: dict):
    s = _session(body.get("sid"))
    mail.load_default(s)
    return _inbox_json(s)


@app.post("/api/mail/emails")
async def mail_add(body: dict):
    s = _session(body.get("sid"))
    # Like Gmail push (users.watch → Pub/Sub): the agent learns about it, as context only.
    return mail.add(s, {**(body.get("email") or {}), "unread": True})


@app.patch("/api/mail/emails/{email_id}")
async def mail_update(email_id: int, body: dict):
    s = _session(body.get("sid"))
    email = mail.update(s, email_id, body.get("email") or {})
    if not email:
        raise HTTPException(404, "no such email")
    return email


@app.delete("/api/mail/emails/{email_id}")
async def mail_delete(email_id: int, sid: str):
    if not mail.delete(_session(sid), email_id):
        raise HTTPException(404, "no such email")
    return {"ok": True}


app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
