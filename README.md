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

1. Landing page → **Chat with Persona** starts a session; Persona texts first.
2. It asks what to call it. "You pick", or 25s of silence (typing holds the
   timer off), gives **Your Persona**.
3. It offers a quick call. On the call (or over text if they'd rather) it learns
   their name and what they could use a hand with.
4. It offers to connect Gmail, texting the link mid-call if they're on the
   phone, and reacts with something concrete from the inbox.

## Design choices

### 1. One session, two channels

Text and voice are two views of one session: one thread, one memory, one call
state. Anything said on the call is in the text agent's context afterwards,
and the call starts knowing the text thread. Hanging up isn't an error, just a
switch back to text: the agent picks up where the call left off.

### 2. A scripted spine, with the model writing the words

`flow.py` decides *where* the conversation is (naming → call offer → their
name → their need → Gmail). `lines.py` holds approved wording for the key beats
(`named`, `calling_now`, `ask_help`, `gmail_offer`, …). Each reply is a list
of parts, each either an approved line sent word for word or the model's own
words when the moment needs them, and every bubble records which it was. Lines
appear only when they make sense: `after_decline` only right after a decline,
`gmail_offer` only once the need is known. The model can't pick a beat whose
moment hasn't happened. The opener skips the model entirely, so it's instant.

### 3. Listen first, then talk

The chat model is good at conversation and bad at bookkeeping: it would happily
reply "nice to meet you, owen!" and never save the name. So a separate,
single-purpose **listener** (`extract.py`) reads the user's latest lines
*before* each reply and updates state: the agent's name (including renames),
the user's name, their need, and "no calls, please". The chat model then talks
from up-to-date state. That one split fixed a whole class of bugs, like the
agent renaming itself "Owen" because the user said "i'm owen".

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
and once onboarding is done the agent stops driving. The nudge usually lands
on an approved line (`ask_user_name`, `ask_help`, `gmail_offer`), so it sounds
like the rest of the flow rather than a reminder. The name timeout is the same
idea applied to the very first question.

### 5. Calls that can't get stuck

Every way a call can end goes through one idempotent path, `calls.finished()`:
hang up, decline, no answer, mic blocked, tab closed, voice-service error. It
leaves exactly one record in the thread and hands back to text. Promises are
enforced in code: sending "calling you now" always rings, and the "the call
didn't go through" line only exists after a real failure. A call belongs to the
page holding its audio, which gets 4s to reconnect, so a network blip doesn't
kill it but a closed tab does. If the user asks to be called, the agent calls,
even after "let's just text" (that only stops it *offering*).

### 6. Gmail: simulated, and a context layer

Real Gmail inbox access needs Google's app verification, so connecting is
simulated: the link opens a new tab (so a live call keeps its audio), connects
one default inbox, and the agent reacts with one concrete thing from it. If a
call is live, it says it out loud within about a second.

After that the inbox is **context, never a notification**. The tab becomes an
editor (add, edit, delete, star; opening an email marks it read), and every
change lands in an activity log the agent sees on its next turn. During a call,
changes are slipped in silently. The agent never messages the user just
because mail changed. With real Gmail the same events would come from push
(`users.watch` → Pub/Sub → webhook). The agent reads mail only through
`overview` / `read_inbox` / `search` / `read` (`mail.py`), so a real provider
could slot in behind them.

### 7. Say less

Prompts push for one short text, one question at a time, no filler, and voice
turns of one sentence. Measured with `scripts/text_e2e.py`, that took replies
from 19 to about 12 words, and call turns from 11–21s to 2–4s. On calls, every
tool call splits the agent's turn around a pause, so the voice agent only gets
tools worth that pause (texting the user, reading the inbox); bookkeeping is
left to the listener.

## Project layout

```
web/        landing page, iMessage + call UI, Gmail connect + inbox editor (no build step)
server/
  app.py         FastAPI: static UI, /ws thread socket, /api/offer WebRTC, /api/mail
  session.py     one session per visitor: thread, model history, call state (saved to .data/)
  flow.py        onboarding steps, the name timeout, and the waiting/stalled loop
  lines.py       approved lines: exact wording for the key beats (edit copy here)
  extract.py     the listener: names, renames, their need, call preference
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

## Models

| Setting | Default | Used for |
|---|---|---|
| `OPENAI_TEXT_MODEL` | `gpt-4.1-mini` | texting and the listener |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | the call (speech-to-speech) |
| `OPENAI_VOICE` | `marin` | the agent's voice |
| `NAME_TIMEOUT_SECS` | `25` | silence before it goes by "Your Persona" |
