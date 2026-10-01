"""End-to-end Gmail-on-a-call test: connect Gmail mid-call, hear the reaction.

Texts its way into a call, asks for help with the inbox on the call, asks for the
link, waits for the Connect Gmail card to be texted mid-call, then taps it to connect
and reports how fast the agent reacts out loud and what it said.

    uv run python scripts/voice_gmail_e2e.py [host:port]
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
    lines = {
        "u1": "Hey! I'm Owen.",
        "u2": "Honestly my inbox is a mess. Could you take a look at it?",
        "u3": "Yeah, send me the link.",
    }
    audio = {k: tts(v) for k, v in lines.items()}
    sid = str(uuid.uuid4()); page = "probe-" + uuid.uuid4().hex[:8]
    speaking = asyncio.Event(); quiet = asyncio.Event(); quiet.set(); ringing = asyncio.Event()
    starts, ends, captions, thread = [], [], [], []
    async with websockets.connect(f"ws://{BASE}/ws?sid={sid}&page={page}") as ws:
        async def reader():
            async for raw in ws:
                ev = json.loads(raw); t = ev["type"]
                if t == "message":
                    m = ev["message"]; line = m["text"] if m["kind"] in ("text", "event") else f"<{m['kind']} {m['meta'].get('title', m['meta'])}>"
                    thread.append((ts(), m["sender"], line)); log(f"thread  {m['sender']:6} {line}")
                elif t == "call" and ev["call"]["status"] == "ringing": ringing.set()
                elif t == "speaking":
                    (starts if ev["on"] else ends).append(ts())
                    (speaking.set(), quiet.clear()) if ev["on"] else (speaking.clear(), quiet.set())
                elif t == "caption": captions.append((ts(), ev["text"]))
        rt = asyncio.create_task(reader())
        async def text(msg, wait):
            await ws.send(json.dumps({"type": "user_message", "text": msg, "client_id": uuid.uuid4().hex})); await asyncio.sleep(wait)
        await asyncio.sleep(4); await text("nova", 8); await text("sure call me", 1)
        await asyncio.wait_for(ringing.wait(), 20); await asyncio.sleep(1.5)
        await ws.send(json.dumps({"type": "call_accept", "page": page}))
        mic = Mic(); pc = RTCPeerConnection(); pc.addTrack(mic); sink = MediaBlackhole()
        pc.on("track", lambda tr: sink.addTrack(tr)); dc = pc.createDataChannel("pipecat")
        async def pinger():
            while True:
                if dc.readyState == "open": dc.send(f"ping: {time.time()}")
                await asyncio.sleep(1)
        pt = asyncio.create_task(pinger())
        await pc.setLocalDescription(await pc.createOffer())
        def post(path, body):
            req = urllib.request.Request(f"http://{BASE}{path}", method="POST", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read())
        ans = await asyncio.to_thread(post, "/api/offer", {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "request_data": {"session_id": sid, "page": page}})
        await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"])); await sink.start()
        await asyncio.wait_for(speaking.wait(), 15); await asyncio.wait_for(quiet.wait(), 30); await asyncio.sleep(0.8)
        async def say(k):
            log(f">> caller: {lines[k]}")
            n = len(starts)
            await (mic.say(audio[k])).wait()
            try:  # wait for the agent to answer, then for it to really finish
                await asyncio.wait_for(_wait_for_len(starts, n + 1), 15)
            except asyncio.TimeoutError:
                log("   (agent didn't answer)")
            while True:
                await asyncio.wait_for(quiet.wait(), 40)
                mark = len(starts); await asyncio.sleep(3)
                if len(starts) == mark and quiet.is_set(): break
        await say("u1"); await say("u2")
        if not any("Connect Gmail" in l for _, _, l in thread):
            await say("u3")
        for _ in range(20):  # give the card a moment to land
            if any("Connect Gmail" in l for _, _, l in thread): break
            await asyncio.sleep(0.5)
        card = any("Connect Gmail" in l for _, _, l in thread)
        log(f">> link card in thread: {card}. Tapping it to connect")
        connect_at = ts()
        await asyncio.to_thread(post, "/api/mail/connect", {"sid": sid})
        n = len(starts)
        try:
            await asyncio.wait_for(_wait_for_len(starts, n + 1), 20)
            log(f">> agent started talking {starts[n] - connect_at:.1f}s after connecting")
        except asyncio.TimeoutError:
            log(">> agent said nothing within 20s of connecting")
        # done when nothing has been said for 4s (tool calls pause mid-turn)
        while True:
            await asyncio.wait_for(quiet.wait(), 40)
            mark = len(starts)
            await asyncio.sleep(4)
            if len(starts) == mark and quiet.is_set():
                break
        await ws.send(json.dumps({"type": "hangup"})); await asyncio.sleep(8)
        pt.cancel(); await pc.close(); rt.cancel()
    print("\n=== what the agent said after Gmail connected (final caption per turn) ===")
    after = [t for at, t in captions if at >= connect_at]
    turns = [t for i, t in enumerate(after) if i + 1 == len(after) or not after[i + 1].startswith(t[:15])]
    print("\n".join(turns) or "(nothing)")
    print("\n=== thread ===")
    for at, who, line in thread: print(f"{at:6.1f}s {who:6} {line}")


async def _wait_for_len(lst, n):
    while len(lst) < n:
        await asyncio.sleep(0.02)


asyncio.run(main())
