"""Simulated Gmail: one default inbox per session that the user can edit.

The agent only touches mail through overview / search / read, so a real Gmail
provider could replace this module's storage without changing the agent.
Real Gmail inbox access needs Google's app verification, which is why the
onboarding uses a sandbox: anyone can connect, and testers control the context.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from .lines import INBOX_OWNER
from .session import Session

# ── the default inbox ───────────────────────────────────────────
# (from_name, from_email, subject, body, minutes_ago, unread, starred)
DEFAULT_EMAILS: list[tuple[str, str, str, str, int, bool, bool]] = [
    ("Marcus Lee", "marcus@brightline-logistics.com", "Dashboard down again — third time this month",
     "Hi Alex,\n\nOur ops team can't load the routing dashboard again. This is the third outage this month and we have a board review Friday. I need a timeline today or we'll have to revisit the contract.\n\nMarcus", 38, True, False),
    ("Priya Shah", "priya@northbeam.vc", "Q3 numbers before Thursday's partner meeting?",
     "Hey Alex! Could you send updated Q3 revenue and burn before Thursday? Partners want to discuss the follow-on. A short summary is fine.\n\nPriya", 125, True, True),
    ("Jamie Chen", "jamie@yourco.io", "can you review the pricing page copy tonight?",
     "pushed a draft to the doc. mostly worried about the enterprise tier wording. need your eyes before we ship tomorrow morning", 190, True, False),
    ("Sofia Alvarez", "sofia.alvarez@fastmail.com", "Re: Senior engineer offer — a few questions",
     "Thanks so much for the offer! Before I sign: is the equity on a 4-year vest with a 1-year cliff, and is remote-first still the plan? I have another offer expiring Monday.\n\nSofia", 300, True, False),
    ("Dana Brooks", "dana@harborpm.com", "Office lease renewal — signature needed by the 15th",
     "Hi Alex, attached is the renewal for suite 4B. Rent goes up 6%. Please sign by the 15th or let me know if you'd like to discuss terms.\n\nDana, Harbor Property Management", 720, False, False),
    ("Hollis & Grant LLP", "docs@hollisgrant.law", "Draft SAFE for review",
     "Please find the draft SAFE for the bridge round. Key terms: $12M cap, no discount, MFN. Let us know if you have comments by Wednesday.", 1500, False, False),
    ("Calendar", "calendar@yourco.io", "Invitation: Board prep @ Fri 10:00",
     "Board prep with Jamie and Priya. Friday 10:00–11:00. Agenda: Q3 metrics, hiring plan, bridge round.", 1700, False, False),
    ("The Operator's Digest", "hello@operatorsdigest.com", "5 ways founders waste their mornings",
     "This week: the inbox trap, meeting creep, and why your calendar is lying to you.", 2900, False, False),
    ("SaaS Weekly", "news@saasweekly.io", "Pricing pages that convert",
     "Teardowns of 12 pricing pages, plus: usage-based pricing is back.", 4300, False, False),
    ("Launch Daily", "digest@launchdaily.co", "Today's top launches",
     "An AI note-taker, a calendar app for dogs, and 14 more.", 5800, False, False),
]


def account(session: Session) -> str:
    return f"{INBOX_OWNER.lower()}@sandbox.mail"  # the inbox is Alex's (the agent can infer the name)


# ── storage on the session ──────────────────────────────────────
def state(session: Session) -> dict[str, Any]:
    if not session.mail:
        session.mail = {"status": "none", "emails": [], "next_id": 1}
    return session.mail


def connected(session: Session) -> bool:
    return state(session)["status"] == "connected"


def load_default(session: Session) -> None:
    st = state(session)
    now = time.time()
    st["emails"] = []
    st["next_id"] = 1
    st["activity"] = []
    for name, addr, subject, body, ago, unread, starred in DEFAULT_EMAILS:
        _insert(st, {"from_name": name, "from_email": addr, "subject": subject, "body": body,
                     "at": now - ago * 60, "unread": unread, "starred": starred})
    session.changed()


def _insert(st: dict[str, Any], email: dict[str, Any]) -> dict[str, Any]:
    email = {"id": st["next_id"], "from_name": "", "from_email": "", "subject": "", "body": "",
             "at": time.time(), "unread": True, "starred": False, **email}
    email["id"] = st["next_id"]
    st["next_id"] += 1
    st["emails"].append(email)
    return email


EDITABLE = {"from_name", "from_email", "subject", "body", "unread", "starred", "at"}
ACTIVITY_KEPT = 30


# ── activity: the inbox as a context layer ──────────────────────
# Changes never make the agent message the user. They're logged here and shown to
# the agent as context on its next turn (and slipped silently into a live call).
def record(session: Session, kind: str, email: dict[str, Any]) -> None:
    st = state(session)
    log = st.setdefault("activity", [])
    entry = {"at": time.time(), "kind": kind, "id": email["id"],
             "from": email["from_name"] or email["from_email"], "subject": email["subject"]}
    last = log[-1] if log else None
    if last and last["kind"] == kind == "edited" and last["id"] == email["id"] and entry["at"] - last["at"] < 120:
        log[-1] = entry  # autosave fires per keystroke: one "edited" per burst
    else:
        log.append(entry)
    del log[:-ACTIVITY_KEPT]
    session.changed()
    if session.call.status == "active" and session.call.note:
        asyncio.create_task(session.call.note(f"Inbox update (context only, don't bring it up unless relevant): {describe(entry)}"))


def describe(a: dict[str, Any]) -> str:
    what = {"new": "new email arrived", "read": "read", "unread": "marked unread", "starred": "starred",
            "unstarred": "unstarred", "edited": "edited", "deleted": "deleted"}.get(a["kind"], a["kind"])
    return f"{what}: {a['from']}, \"{a['subject']}\" (id {a['id']})"


def recent_activity(session: Session, n: int = 8) -> list[str]:
    st = state(session)
    since = st.get("connected_at", 0)
    return [f"{_age(a['at'])}: {describe(a)}" for a in st.get("activity", [])[-n:] if a["at"] >= since]


def add(session: Session, fields: dict[str, Any]) -> dict[str, Any]:
    email = _insert(state(session), {k: v for k, v in fields.items() if k in EDITABLE})
    record(session, "new", email)
    return email


def update(session: Session, email_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    for e in state(session)["emails"]:
        if e["id"] == email_id:
            before = dict(e)
            e.update({k: v for k, v in fields.items() if k in EDITABLE})
            if before["unread"] != e["unread"]:
                record(session, "unread" if e["unread"] else "read", e)
            if before["starred"] != e["starred"]:
                record(session, "starred" if e["starred"] else "unstarred", e)
            if any(before[k] != e[k] for k in ("from_name", "from_email", "subject", "body")):
                record(session, "edited", e)
            session.changed()
            return e
    return None


def delete(session: Session, email_id: int) -> bool:
    st = state(session)
    gone = [e for e in st["emails"] if e["id"] == email_id]
    st["emails"] = [e for e in st["emails"] if e["id"] != email_id]
    for e in gone:
        record(session, "deleted", e)
    session.changed()
    return bool(gone)


def sorted_emails(session: Session) -> list[dict[str, Any]]:
    return sorted(state(session)["emails"], key=lambda e: e["at"], reverse=True)


# ── what the agent can do (the provider interface) ──────────────
def _age(at: float) -> str:
    mins = max(0, int((time.time() - at) / 60))
    if mins < 60:
        return f"{mins}m ago"
    if mins < 60 * 24:
        return f"{mins // 60}h ago"
    return f"{mins // (60 * 24)}d ago"


def _brief(e: dict[str, Any]) -> dict[str, Any]:
    return {"id": e["id"], "from": e["from_name"] or e["from_email"], "subject": e["subject"],
            "snippet": e["body"][:120], "unread": e["unread"], "starred": e["starred"], "received": _age(e["at"])}


NOT_CONNECTED = {"error": "Gmail isn't connected yet. Offer to connect it instead."}


def overview(session: Session) -> dict[str, Any]:
    if not connected(session):
        return NOT_CONNECTED
    emails = sorted_emails(session)
    senders: dict[str, int] = {}
    for e in emails:
        senders[e["from_name"] or e["from_email"]] = senders.get(e["from_name"] or e["from_email"], 0) + 1
    top = sorted(senders.items(), key=lambda kv: -kv[1])[:5]
    return {
        "account": account(session),
        "total": len(emails),
        "unread": sum(e["unread"] for e in emails),
        "top_senders": [{"name": n, "emails": c} for n, c in top],
        "recent": [_brief(e) for e in emails[:10]],
    }


def search(session: Session, query: str, limit: int = 8) -> dict[str, Any]:
    """Emails containing ANY of the words, best matches first (an all-words match found nothing
    for "due deadline action required" and the agent concluded there were no to-dos)."""
    if not connected(session):
        return NOT_CONNECTED
    words = [w for w in query.lower().split() if len(w) > 1]
    scored = []
    for e in sorted_emails(session):
        text = f"{e['from_name']} {e['from_email']} {e['subject']} {e['body']}".lower()
        score = sum(w in text for w in words)
        if score:
            scored.append((score, e))
    scored.sort(key=lambda se: -se[0])
    return {"query": query, "matches": [_brief(e) for _, e in scored[:limit]], "total_matches": len(scored)}


def read_all(session: Session, limit: int = 30) -> dict[str, Any]:
    """Every email in full (newest first). The inbox is small, so judgment questions
    ("what are my to-dos?") are answered from the actual content, not keyword hits."""
    if not connected(session):
        return NOT_CONNECTED
    return {"emails": [{**_brief(e), "body": e["body"][:800]} for e in sorted_emails(session)[:limit]]}


def read(session: Session, email_id: int) -> dict[str, Any]:
    if not connected(session):
        return NOT_CONNECTED
    for e in state(session)["emails"]:
        if e["id"] == email_id:
            return {**_brief(e), "from_email": e["from_email"], "body": e["body"]}
    return {"error": f"no email with id {email_id}"}


# ── the onboarding moments ──────────────────────────────────────
def link_url(session: Session) -> str:
    page = "inbox.html" if connected(session) else "connect.html"
    return f"/{page}?sid={session.id}"


async def send_link(session: Session) -> None:
    state(session)["link_sent"] = True
    session.changed()
    await session.add_message("agent", "card", title="Connect Gmail", sub="Tap to connect your inbox",
                              url=f"/connect.html?sid={session.id}")


async def connect(session: Session) -> None:
    st = state(session)
    first = st["status"] != "connected"
    if first:
        load_default(session)
        st["connected_at"] = time.time()
    st["status"] = "connected"
    session.changed()
    logger.info(f"[{session.id[:8]}] gmail connected")
    if first:
        # Hand over the overview up front: one less tool round trip (and pause) on a live call.
        ov = overview(session)
        listing = "; ".join(
            f"[{e['id']}] {e['from']}: \"{e['subject']}\" ({'unread, ' if e['unread'] else ''}"
            f"{'starred, ' if e['starred'] else ''}{e['received']})" for e in ov["recent"])
        overview_note = (f"Gmail is connected: {ov['total']} emails, {ov['unread']} unread. Most recent: {listing}.")
        email_task = session.help_need if session.ob.get("help_is_email") else None
        session.ob["pending"] = None
        session.remember("system", "They connected their Gmail")
        from . import flow, text_agent  # avoid an import cycle

        if session.call.status == "active" and session.call.inject:
            # What they asked for on this call isn't saved until it ends, so the call decides.
            note = (f"{overview_note} It worked: confirm that in a few words, then ask if there's anything in their "
                    "email they'd like you to do (if they already said, just do it). Do that one thing right there on "
                    "the call with the inbox tools, then close out as in Ending (kind \"email\"). If they say no, close out "
                    "with kind \"none\". Only mention emails listed here or returned by the inbox tools.")
            asyncio.create_task(session.call.inject(note))
        elif email_task and not session.ob.get("complete"):
            text_agent.send_beats(session, [], answer=True, note=f"{overview_note} {flow.EMAIL_TASK_NOTE}",
                                  complete=("email", email_task), lead=["gmail_connected"])
        else:
            nxt = flow.next_ask(session)
            text_agent.send_beats(
                session, [nxt] if nxt else [], answer=True, lead=["gmail_connected"],
                note=f"{overview_note} React with ONE concrete, useful observation tied to what they need "
                     "(read_email for details). Only mention emails listed here or returned by the inbox tools.")


async def deny(session: Session) -> None:
    """They said no to connecting (over text); recorded mid-reply by the agent."""
    st = state(session)
    if st["status"] == "connected":
        return
    st["status"] = "declined"
    session.changed()
    session.remember("system", "The user chose not to connect Gmail for now. Don't push; carry on.")
