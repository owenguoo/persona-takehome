# Persona onboarding

My take on Persona's onboarding. You land on an iMessage thread with your new
assistant, it asks what you want to call it, and then it calls you to sort out
the rest: your name, what you need help with, and your Gmail.

## Running it

You'll need [uv](https://docs.astral.sh/uv/) and an OpenAI key.

```bash
cp .env.example .env    # then paste your OPENAI_API_KEY in
uv run python -m server
```

Open http://localhost:5173 in Chrome or Safari and allow the mic when the call
comes in. It works on desktop (a Mac Messages window) and on a phone-sized
screen (iOS Messages).

Add `?dev` to the URL for a small panel of shell controls (force a call, send
the Gmail card, reset the session, switch layouts). It's hidden by default.

## How it goes

1. **Name the assistant.** It's the first text. If you dodge it, ask it
   something else, or say nothing for 25 seconds, it goes by "Your Persona"
   and you can rename it whenever.
2. **Save the contact.** Persona sends its contact card, and calls only ring
   through once you've saved it, the same way an iPhone silences unknown
   numbers. Persona can't actually see whether you saved it (iMessage doesn't
   tell it), so if a call gets silenced it checks in and tries once more.
3. **The call.** It introduces itself, asks your name, then asks what it can
   help with.
   - Something email-related: it texts you a Gmail link while you're still on
     the call, notices when you've connected, and does one thing in your inbox
     right there.
   - Anything else ("summarize my Slack"): "I'm on it, I'll text you", and it
     wraps up.
   - No idea: it sends the Gmail link anyway and offers to find something.
4. **Back to text.** Everything from the call carries over. "Onboarding
   complete" only shows up once a real first task has started.

You don't have to take the call. Declining, hanging up, or just never picking
up all drop you back into the text thread, and it keeps going over text.
Saying no to your name or to Gmail is respected the first time. Persona won't
ask the same question twice in a row.

## How it's built

Python (FastAPI) on the backend, plain HTML/CSS/JS on the front with no build
step. Texting uses `gpt-4.1-mini`. The call is speech-to-speech over WebRTC
with OpenAI Realtime, wired up with Pipecat.

The main thing I learned early: if you let one model both chat and keep
track of onboarding, it does the chatting fine and loses track of everything
else. It would say "nice to meet you, Owen!" and never save the name, or say
"calling you now" and not call. So each message goes through three steps:

- **Listen** (`extract.py`): a small structured-output call that only pulls
  facts out of what you said, like a name, a yes/no to the call or Gmail, or
  what you need help with.
- **Plan** (`flow.py`): plain code decides what's still missing and what to say
  next. Most of what Persona says comes from a list of written lines
  (`lines.py`), and some lines carry an action, so "calling you now" always
  rings and "here's the link" always sends the link.
- **Talk** (`text_agent.py`): the model only writes its own reply when you
  said something that needs a real answer.

It also shouldn't feel turn-based. After every reply Persona checks whether
it's waiting on you (it asked something, a link is out, a call is ringing) or
whether the conversation has just stalled. If it stalled, it double-texts once
to move on to the next step. Calls work the same way: it keeps things moving
if you go quiet, says "no rush" while you open the link, and hangs up politely
after a long silence instead of sitting on a dead line.

Every way a call can end (hang up, decline, missed, mic blocked, tab closed,
an error) runs through one function in `calls.py`, which leaves a single
record in the thread and hands things back to text with a fitting follow-up.

## Gmail

Gmail is simulated. Real inbox access needs Google's app verification, which
wasn't realistic here. The link connects a sample inbox belonging to "Alex",
which is why Persona asks if that's you when it doesn't know your name. The
inbox tab turns into an editor where you can add, edit and delete mail.
Persona sees those changes as background context but never texts you about
them. All mail access goes through a few functions in `mail.py`, so a real
provider could slot in behind them.

## Things I'd do next

- Real Gmail OAuth, with push notifications instead of the editor.
- Actually doing the handed-off tasks. Right now "I'm on it" for Slack,
  calendar and so on is where it stops.
- A real database. Sessions are JSON files in `.data/` for now.
- Proper phone numbers (Twilio or similar) instead of a browser call.

## Layout

```
web/          landing page, iMessage + call UI, Gmail connect page, inbox editor
server/
  app.py         routes, the thread websocket, WebRTC offer, mail API
  session.py     per-visitor state, saved to .data/
  flow.py        what's missing, what to say next, the stall loop
  lines.py       the written lines (edit the wording here)
  extract.py     the listener
  text_agent.py  iMessage replies
  calls.py       call lifecycle
  voice.py       the voice pipeline
  mail.py        the simulated inbox
  prompts.py     prompts for text and voice
```

Optional settings for `.env`: `OPENAI_TEXT_MODEL`, `OPENAI_REALTIME_MODEL`,
`OPENAI_VOICE`, and `NAME_TIMEOUT_SECS` (default 25).
