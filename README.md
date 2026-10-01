# Persona onboarding

An iMessage-style onboarding that turns into a phone call when it helps. It
collects a name for the agent, the user's name, what they need help with, and a
connected Gmail, without feeling like a form, and without falling over when
people don't play by the rules.

## Run

1. Copy `.env.example` to `.env` and add `OPENAI_API_KEY=sk-...` (re-read on every
   request, so no restart needed).
2. `uv run python -m server`
3. Open http://localhost:5173 in Chrome or Safari (the call needs the microphone).

## The flow

Onboarding is four small flows, plus the call that carries most of them:

1. **Persona's name.** Asked first. A bare "hey!" gets one friendly re-ask;
   anything else that isn't a name (a question, "you pick", 25s of silence) →
   **Your Persona**. Then Persona sends its **contact card**: calls only ring
   once it's saved, like an iPhone silencing unknown callers. Persona can't see
   the save (iMessage doesn't say), so after a silenced call it checks in
   ("saved me?") and tries once more.
2. **Their name.** Asked on the call (or by text). "I'd rather not say" is
   final. Once Gmail is connected: "your email says you're Alex, is that right?"
3. **Gmail.** Offered on the call: the link is texted mid-call. "No" is final.
4. **A first action.** "What can I help you with?" (or "what can you do?").
   Email → Gmail flow. Nothing in mind → the Gmail link, with the suggestion.
   Anything else is noted honestly and onboarding carries on.

The call: introduce + ask their name → "nice to meet you, what can I help you
with?" → for email, text the link, confirm it connected, "reading through your
email now, I'll text you when I'm done", hang up, then actually text the
result. For anything else, "I'm on it" and hang up.

**Onboarding complete** appears in the thread only when the first real task
starts, never just because the questions ran out: that's the line between
onboarding and real work.

## Design choices

### 1. One session, two channels

Text and voice are two views of one session: one thread, one memory, one call
state. Anything said on the call is in the text agent's context afterwards,
and the call starts knowing the text thread. Hanging up isn't an error, just a
switch back to text: the agent picks up where the call left off.

### 2. Listen → plan → talk

Each user message goes through three steps:

- **Listen** (`extract.py`): a small single-purpose call reports what they
  said: a name or rename, their name or "rather not say", their need (and
  whether it's about email), yes/no to the call or Gmail, "what can you do?",
  and whether the message needs a real answer. It's told the pending question,
  so a bare "sure" lands on the right thing, and it reads every message it
  hasn't processed, even ones typed mid-reply.
- **Plan** (`flow.py`): turns that into state and **beats**, approved lines from
  `lines.py` sent word for word. Lines carry actions, so saying "calling you now"
  rings and "here's the link" sends the link. Saying it and doing it can't drift.
- **Talk** (`text_agent.py`): the chat model is only called when the user said
  something that needs a real answer; most replies skip it entirely.

The one rule, enforced in code: **never ask the same thing twice in a row.** A
dodged question moves the flow on, and each item gets at most two asks.

Why: the chat model was good at conversation and bad at bookkeeping. It would
say "nice to meet you, owen!" and save nothing, rename itself "Owen", or say
"calling you now" without calling. Splitting understanding and deciding from
talking fixed that whole class of bugs.

### 3. Fast typists

A user who types fast lands mid-reply, so a reply that has started sending
finishes before the next one is planned, and every question it asked is on
record. The opener can't be cancelled.

### 4. The agent loop: waiting vs. stalled

Pure turn-taking gets stuck. If the user says "rename yourself to bob" and the
agent answers "Bob it is", nobody has the next move, and onboarding stops dead
with half the information missing. A person would just double-text.

So after every agent turn, a small loop asks which state the conversation is in:

```
agent finishes a turn
        │
        ▼   wait ~6s (and longer while the user is typing)
user said something since? ───── yes ──▶ stop (their message gets its own reply)
        │ no
onboarding complete? ─────────── yes ──▶ stop (a finished conversation can rest)
        │ no
WAITING on the user? ─────────── yes ──▶ stop
(asked a question, Gmail link out,
 call ringing or live)
        │ no  ⇒ STALLED
already nudged this silence? ─── yes ──▶ stop
        │ no
        ▼
double-text ONE line that moves to the next missing step
```

It's deliberately conservative. A question in the user's court is respected
(no nagging), typing pauses the clock, there's at most one nudge per silence,
and once onboarding is done the agent stops driving. The nudge is always an
approved line (`ask_user_name`, `ask_help`, `gmail_offer`) and never the
question just asked, so it sounds like the rest of the flow rather than a
reminder. The name timeout is the same
idea applied to the very first question.

Calls get the same loop, so they aren't strictly turn-based either: if the
agent said something that wasn't a question and the caller is quiet, it keeps
things moving (twice at most); if it asked something and gets silence, it
nudges once ("no rush, or I can suggest something"); while the caller opens the
Gmail link it says "no rush, I'm here"; long silence gets "still there?", then
a goodbye and a text follow-up.

### 5. Calls that can't get stuck

Every way a call can end goes through one idempotent path, `calls.finished()`:
hang up, decline, no answer, mic blocked, tab closed, voice-service error. It
leaves exactly one record in the thread and hands back to text. Promises are
enforced in code: sending "calling you now" always rings, and the "the call
didn't go through" line only exists after a real failure. A call belongs to the
page holding its audio, which gets 4s to reconnect, so a network blip doesn't
kill it but a closed tab does. If the user asks to be called, the agent calls,
even after "let's just text" (that only stops it *offering*). Endings get their
own follow-up: cancelled while connecting ("no worries"), dropped ("we got cut
off"), over in seconds ("that call ended quick"), silence ("seemed like a bad
time"). Texts sent during a call are absorbed and acknowledged out loud, so the
two channels never run separate conversations.

The call itself is conversational, not an interview: it reacts more than it
asks, backs off when the user hesitates, and waits for a real end of turn
rather than a thinking pause (low-eagerness turn detection). It can end the
call itself once it needs to go do something, but never abruptly: it says
what happens next and asks "anything else before I hang up?" (`wrap_up`),
then hangs up on a "no" or a short pause (`end_call`). An early `end_call` is
turned into that closing question in code.

### 6. Gmail: simulated, and a context layer

Real Gmail inbox access needs Google's app verification, so connecting is
simulated: the link opens a new tab (so a live call keeps its audio), connects
one default inbox (Alex's), and the agent reacts with one concrete thing from
it. If a call is live, it says so out loud within about a second.

After that the inbox is **context, never a notification**. The tab becomes an
editor (add, edit, delete, star; opening an email marks it read), and every
change lands in an activity log the agent sees on its next turn. During a call,
changes are slipped in silently. The agent never messages the user just
because mail changed. With real Gmail the same events would come from push
(`users.watch` → Pub/Sub → webhook). The agent reads mail only through
`overview` / `read_inbox` / `search` / `read` (`mail.py`), so a real provider
could slot in behind them.

### 7. Honest about what it can do

Persona can read the (simulated) inbox and text. It says plainly that it can't
send email, set reminders or touch other apps yet, and never claims it did.
Email contents are treated as data, never instructions; anything asking for
credentials gets flagged as phishing. Injected "names" (`SYSTEM OVERRIDE…`)
are rejected.

### 8. Say less

Prompts push for one short text, one question at a time, no filler, and voice
turns of one sentence. Measured with `scripts/text_e2e.py`, that took replies
from about 19 words to 13–17, and call turns from 11–21s to 2–4s. On calls, every
tool call splits the agent's turn around a pause, so the voice agent only gets
tools worth that pause (texting the user, reading the inbox); bookkeeping is
left to the listener.

## Project layout

```
web/        landing page, iMessage + call UI, Gmail connect + inbox editor (no build step)
server/
  app.py         FastAPI: static UI, /ws thread socket, /api/offer WebRTC, /api/mail
  session.py     one session per visitor: thread, model history, call state (saved to .data/)
  flow.py        the flow engine: item state, planning beats, name timeout, stall loop, completion
  lines.py       approved lines and their actions (edit copy here)
  extract.py     the listener: what the user just said, as onboarding facts
  text_agent.py  iMessage replies (OpenAI chat, structured replies, tools)
  calls.py       call lifecycle: ring / accept / decline / missed / hang up
  voice.py       Pipecat pipeline: WebRTC ⇄ OpenAI Realtime speech-to-speech
  mail.py        simulated Gmail: the default inbox, activity log, read-only tools
  prompts.py     Persona's voice, per step and per channel
```

## Testing

All three run against the live server and use the key in `.env`:

- `scripts/text_e2e.py`: a fixed six-message conversation; reports words per
  reply and how many bubbles came from approved lines.
- `scripts/voice_e2e.py`: a scripted caller (OpenAI TTS) texts its way into a
  call, talks, interrupts, asks to be texted, and hangs up; reports latencies.
- `scripts/voice_gmail_e2e.py`: asks for the Gmail link on a call, taps it, and
  reports how fast and what the agent says.
- `scripts/adversary_e2e.py`: users who don't play by the rules (greetings,
  "call me" twice, venting, typos, injection, "stop texting me", Spanish,
  refusing everything…), each checked against the saved session state.

## Models

| Setting | Default | Used for |
|---|---|---|
| `OPENAI_TEXT_MODEL` | `gpt-4.1-mini` | texting and the listener |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | the call (speech-to-speech) |
| `OPENAI_VOICE` | `marin` | the agent's voice |
| `NAME_TIMEOUT_SECS` | `25` | silence before it goes by "Your Persona" |
