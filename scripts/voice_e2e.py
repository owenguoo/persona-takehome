"""End-to-end voice test: text into a call, then talk to the agent with synthesized speech.

Follows the real product path against a running server: open a session over the thread
socket, name the agent, say yes to the call, answer the ring, then speak over WebRTC
(including interrupting the agent). Logs every thread/call/caption/speaking event and
reports latencies. Uses the OpenAI key in .env for the caller's voice (TTS).

    uv run python scripts/voice_e2e.py [host:port]
"""
import asyncio, fractions, json, os, sys, time, uuid, urllib.request, urllib.error

import av
import numpy as np
import websockets
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaBlackhole
from dotenv import load_dotenv
from openai import OpenAI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:5173"
load_dotenv(os.path.join(ROOT, ".env"))
oa = OpenAI()
T0 = time.monotonic()
ts = lambda: time.monotonic() - T0
log = lambda *a: print(f"{ts():6.2f}s", *a, flush=True)

RATE = 48000
FRAME = 960  # 20 ms


def tts(text: str) -> np.ndarray:
    r = oa.audio.speech.create(model="gpt-4o-mini-tts", voice="alloy", input=text, response_format="pcm")
    pcm24 = np.frombuffer(r.content, dtype=np.int16)
    return np.repeat(pcm24, 2)  # 24 kHz → 48 kHz


class Mic(MediaStreamTrack):
    """A microphone that plays queued utterances and is silent otherwise."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.buf = np.zeros(0, dtype=np.int16)
        self.pts = 0
        self.start = None
        self.done_event = None

    def say(self, pcm: np.ndarray) -> asyncio.Event:
        self.buf = np.concatenate([self.buf, pcm])
        self.done_event = asyncio.Event()
        return self.done_event

    async def recv(self):
        if self.start is None:
            self.start = time.monotonic()
        target = self.start + self.pts / RATE
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if len(self.buf) >= FRAME:
            chunk, self.buf = self.buf[:FRAME], self.buf[FRAME:]
            if len(self.buf) == 0 and self.done_event:
                self.done_event.set()
        else:
            chunk = np.zeros(FRAME, dtype=np.int16)
            if len(self.buf):
                chunk[: len(self.buf)] = self.buf
                self.buf = np.zeros(0, dtype=np.int16)
                if self.done_event:
                    self.done_event.set()
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
        frame.planes[0].update(chunk.tobytes())
        frame.sample_rate = RATE
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, RATE)
        self.pts += FRAME
        return frame


async def main():
    log("synthesizing caller speech…")
    lines = {
        "u1": "Hey! Yeah, I'm Owen. Nice to meet you.",
        "u2": "Honestly, I could really use help keeping my inbox under control. It's kind of a disaster right now, with work email and newsletters and everything mixed together.",
        "u3": "Sorry, one sec. Can you just text me a couple of ideas for that instead?",
    }
    audio = {k: tts(v) for k, v in lines.items()}
    log("ready:", {k: f"{len(v) / RATE:.1f}s" for k, v in audio.items()})

    sid = str(uuid.uuid4())
    page = "probe-" + uuid.uuid4().hex[:8]
    speaking = asyncio.Event()
    quiet = asyncio.Event()
    quiet.set()
    ringing = asyncio.Event()
    agent_speech_starts, agent_speech_ends = [], []
    captions = []
    thread = []
    call_state = {"status": "idle"}

    async with websockets.connect(f"ws://{BASE}/ws?sid={sid}&page={page}") as ws:
        async def reader():
            async for raw in ws:
                ev = json.loads(raw)
                t = ev["type"]
                if t == "message":
                    m = ev["message"]
                    line = m["text"] if m["kind"] in ("text", "event") else f"<{m['kind']} {m['meta']}>"
                    thread.append((ts(), m["sender"], line))
                    log(f"thread  {m['sender']:6} {line}")
                elif t == "call":
                    call_state.update(ev["call"])
                    log(f"call    {ev['call']['status']}")
                    if ev["call"]["status"] == "ringing":
                        ringing.set()
                elif t == "speaking":
                    if ev["on"]:
                        agent_speech_starts.append(ts()); speaking.set(); quiet.clear()
                    else:
                        agent_speech_ends.append(ts()); speaking.clear(); quiet.set()
                    log(f"speaking {'ON ' if ev['on'] else 'off'}")
                elif t == "caption":
                    captions.append((ts(), ev["role"], ev["text"]))
        rt = asyncio.create_task(reader())

        async def text(msg, wait=9):
            log(f"thread  user   {msg}")
            await ws.send(json.dumps({"type": "user_message", "text": msg, "client_id": uuid.uuid4().hex}))
            await asyncio.sleep(wait)

        await asyncio.sleep(7)                       # intro texts
        await text("call you nova", 9)               # name → call offer
        await ws.send(json.dumps({"type": "save_contact"}))  # calls only ring for saved contacts
        await asyncio.sleep(1)
        await text("yeah sure, call me", 1)
        await asyncio.wait_for(ringing.wait(), 20)
        await asyncio.sleep(2.5)                     # let it ring a moment, like a person
        log(">> answering")
        await ws.send(json.dumps({"type": "call_accept", "page": page}))

        mic = Mic()
        pc = RTCPeerConnection()
        pc.addTrack(mic)
        sink = MediaBlackhole()
        pc.on("track", lambda track: sink.addTrack(track))
        dc = pc.createDataChannel("pipecat")

        async def pinger():
            while True:
                if dc.readyState == "open":
                    dc.send(f"ping: {time.time()}")
                await asyncio.sleep(1)
        pt = asyncio.create_task(pinger())
        await pc.setLocalDescription(await pc.createOffer())

        def post():
            req = urllib.request.Request(f"http://{BASE}/api/offer", method="POST",
                data=json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type,
                                 "request_data": {"session_id": sid, "page": page}}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        ans = await asyncio.to_thread(post)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
        await sink.start()
        answered_at = ts()
        call_window = [answered_at, 1e9]

        results = {}
        # Greeting: agent should speak first.
        await asyncio.wait_for(speaking.wait(), 15)
        results["greeting_after_answer_s"] = round(agent_speech_starts[0] - answered_at, 2)
        await asyncio.wait_for(quiet.wait(), 30)
        await asyncio.sleep(0.8)

        async def turn(key):
            log(f">> caller says: {lines[key]}")
            n = len(agent_speech_starts)
            done = mic.say(audio[key])
            await done.wait()
            end = ts()
            await asyncio.wait_for(_wait_for_len(agent_speech_starts, n + 1), 20)
            lat = agent_speech_starts[n] - end
            await asyncio.wait_for(quiet.wait(), 40)
            return round(lat, 2)

        results["reply_latency_u1_s"] = await turn("u1")
        await asyncio.sleep(0.8)

        # u2, then barge in with u3 about 1.2 s after the agent starts answering.
        log(f">> caller says: {lines['u2']}")
        n = len(agent_speech_starts)
        await (mic.say(audio["u2"])).wait()
        u2_end = ts()
        await asyncio.wait_for(_wait_for_len(agent_speech_starts, n + 1), 20)
        results["reply_latency_u2_s"] = round(agent_speech_starts[n] - u2_end, 2)
        await asyncio.sleep(1.2)
        barge_at = ts()
        log(f">> caller interrupts: {lines['u3']}")
        m = len(agent_speech_ends)
        mic.say(audio["u3"])
        await asyncio.wait_for(_wait_for_len(agent_speech_ends, m + 1), 15)
        results["stopped_after_interrupt_s"] = round(agent_speech_ends[m] - barge_at, 2)
        k = len(agent_speech_starts)
        u3_end = barge_at + len(audio["u3"]) / RATE
        try:
            await asyncio.wait_for(_wait_for_len(agent_speech_starts, k + 1), 15 + len(audio["u3"]) / RATE)
            results["reply_after_interrupt_s"] = round(agent_speech_starts[k] - u3_end, 2)
        except asyncio.TimeoutError:
            results["reply_after_interrupt_s"] = "no reply within 15s"
        await asyncio.sleep(1)
        await asyncio.wait_for(quiet.wait(), 40)
        await asyncio.sleep(1.5)

        call_window[1] = ts()
        log(">> hanging up")
        await ws.send(json.dumps({"type": "hangup"}))
        await asyncio.sleep(12)  # text follow-up
        pt.cancel()
        await pc.close()
        rt.cancel()

    print("\n=== captions (final text per turn) ===")
    last = {}
    ordered = []
    for t, role, text in captions:
        if ordered and ordered[-1][1] == role and text.startswith(ordered[-1][2][:12]):
            ordered[-1] = (ordered[-1][0], role, text)
        else:
            ordered.append((t, role, text))
    for t, role, text in ordered:
        print(f"{t:6.2f}s {role:5} {text}")
    print("\n=== thread after the call ===")
    for t, who, line in thread:
        print(f"{t:6.2f}s {who:6} {line}")
    durs = []
    for st in agent_speech_starts:
        end = next((e for e in agent_speech_ends if e > st), None)
        if end: durs.append(round(end - st, 1))
    results["agent_turn_lengths_s"] = durs
    during = [line for t, who, line in thread if who == "agent" and call_window[0] <= t <= call_window[1] and not line.startswith("<")]
    results["texts_sent_during_call"] = during
    print("\n=== results ===")
    print(json.dumps(results, indent=2))


async def _wait_for_len(lst, n):
    while len(lst) < n:
        await asyncio.sleep(0.02)


asyncio.run(main())
