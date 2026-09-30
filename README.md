# Onboarding

An iMessage-style onboarding that can turn into a phone call. Text and voice
share one session, so whatever is said on the call is in the text agent's
memory afterwards, and the call knows the text thread.

```
web/        iMessage + call UI (Mac and iPhone layouts, no build step)
server/
  app.py         FastAPI: static UI, /ws thread socket, /api/offer WebRTC
  session.py     one session per visitor: thread, model history, call state
  text_agent.py  iMessage replies (OpenAI chat + tools)
  calls.py       call lifecycle: ring / accept / decline / missed / hang up
  voice.py       Pipecat pipeline: WebRTC ⇄ OpenAI Realtime speech-to-speech
  prompts.py     persona + channel style (placeholder onboarding logic)
```

## Run

1. Copy `.env.example` to `.env` and put your key in it:
   ```
   OPENAI_API_KEY=sk-...
   ```
   It's re-read on every request, so no restart needed.
2. Start the server:
   ```
   uv run python -m server
   ```
3. Open http://localhost:5173 in Chrome or Safari. The call needs microphone access.

`GET /api/health` shows whether the key is picked up and which models are in use.

## Models

| Setting | Default | Used for |
|---|---|---|
| `OPENAI_TEXT_MODEL` | `gpt-4.1-mini` | iMessage replies |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | the voice call (speech-to-speech) |
| `OPENAI_VOICE` | `marin` | the agent's voice |

## How a call ends

Every exit funnels through `calls.finished()` and is idempotent: user hangs up,
declines, doesn't answer (25s), blocks the mic, closes the tab (the page's
WebSocket drops → server hangs up), or the voice service errors (bad key, no
quota, dropped socket). Each leaves one call record or note in the thread, and the
text agent follows up over text.
