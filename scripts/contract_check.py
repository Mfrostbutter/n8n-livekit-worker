"""Scripted start, turn and end against the configured n8n. No LiveKit, no audio."""
import asyncio
import sys
import time
import uuid

from n8n_worker.agent import start_payload
from n8n_worker.client import N8nClient
from n8n_worker.config import load


async def main() -> int:
    config = load()
    client = N8nClient(config)
    corr = str(uuid.uuid4())
    t = time.monotonic()
    payload = start_payload(
        config, room=f"check-{corr[:8]}", correlation_id=corr, identity="check", kind="SIP",
        attributes={"sip.callID": "contract-check"},
    )
    try:
        start = await client.start(payload)
        print(f"start {int((time.monotonic()-t)*1000)} ms session={start.session_id} lang={start.language} action={start.action}")
        print(f"  speak: {start.speak}")
        for i, line in enumerate(sys.argv[1:] or ["Hello, I would like to book a service appointment."], 1):
            t = time.monotonic()
            turn = await client.turn(start.session_id, line, start.language, correlation_id=corr, turn=i)
            print(f"turn {i} {int((time.monotonic()-t)*1000)} ms action={turn.action}")
            print(f"  speak: {turn.speak}")
            if turn.action != "none":
                break
        t = time.monotonic()
        await client.end(start.session_id, "completed", correlation_id=corr, duration_ms=1000, turns=1)
        print(f"end {int((time.monotonic()-t)*1000)} ms ok")
        return 0
    except Exception as error:
        print("FAILED:", error)
        return 1
    finally:
        await client.aclose()


sys.exit(asyncio.run(main()))
