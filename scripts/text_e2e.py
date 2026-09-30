"""Text-flow test: run a fixed conversation through the real server and measure verbosity.

Opens a fresh session over the thread socket, sends each scripted message once the
agent has finished replying, and prints the transcript plus words/bubbles per reply.
Declines the call if one rings, so the script stays on text.

    uv run python scripts/text_e2e.py [host:port]
"""
import asyncio, json, statistics, sys, time, uuid

import websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:5173"
SCRIPT = [
    "call you nova",
    "nah i'd rather text for now",
    "i'm owen",
    "i mostly need help staying on top of email, it's chaos",
    "what kind of stuff can you actually do?",
    "ok cool. what should we do first?",
]
QUIET_SECS = 3.0


async def main():
    sid = str(uuid.uuid4())
    replies: list[list[str]] = [[]]
    sources: list[str] = []
    tags: dict[str, str] = {}
    last_activity = time.monotonic()
    typing = False

    async with websockets.connect(f"ws://{BASE}/ws?sid={sid}&page=text-e2e") as ws:
        async def reader():
            nonlocal last_activity, typing
            async for raw in ws:
                ev = json.loads(raw)
                if ev["type"] == "typing":
                    typing = ev["on"]
                    last_activity = time.monotonic()
                elif ev["type"] == "message":
                    m = ev["message"]
                    last_activity = time.monotonic()
                    if m["sender"] == "agent" and m["kind"] == "text":
                        src = m["meta"].get("line", "custom")
                        replies[-1].append(m["text"])
                        sources.append(src)
                        tags[m["text"]] = src
                    elif m["kind"] == "event":
                        replies[-1].append(f"· {m['text']}")
                elif ev["type"] == "call" and ev["call"]["status"] == "ringing":
                    await ws.send(json.dumps({"type": "call_decline"}))
        rt = asyncio.create_task(reader())

        async def settle():
            await asyncio.sleep(1.5)
            while typing or time.monotonic() - last_activity < QUIET_SECS:
                await asyncio.sleep(0.2)

        await settle()
        for line in SCRIPT:
            replies.append([])
            print(f"ME   {line}")
            await ws.send(json.dumps({"type": "user_message", "text": line, "client_id": uuid.uuid4().hex}))
            await settle()
            for b in replies[-1]:
                print(f"     {b}" if b.startswith("·") else f"AI   {b}   [{tags.get(b, '?')}]")
        rt.cancel()

    # replies[0] is the opener
    print("\nopener:", " | ".join(replies[0]))
    stats = []
    for r in replies:
        texts = [b for b in r if not b.startswith("·")]
        if texts:
            stats.append((len(texts), sum(len(b.split()) for b in texts)))
    print(json.dumps({
        "replies": len(stats),
        "avg_bubbles_per_reply": round(statistics.mean(b for b, _ in stats), 2),
        "avg_words_per_reply": round(statistics.mean(w for _, w in stats), 1),
        "max_words_in_a_reply": max(w for _, w in stats),
        "bubbles_from_approved_lines": f"{sum(s != 'custom' for s in sources)}/{len(sources)}",
    }, indent=2))


asyncio.run(main())
