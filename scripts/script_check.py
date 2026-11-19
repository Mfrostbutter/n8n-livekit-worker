"""Dry run of scripted mode without a call: start a session, run the script's tools in order, print each line.
Usage (env as for the worker): python scripts/script_check.py VerifyIdentity:pin=1234 FindSlots:fromDate= ChooseSlot:option=1 BookAppointment EndCall"""
import asyncio
import sys
import time
import uuid

from livekit.agents import mcp

from n8n_worker.agent import start_payload
from n8n_worker.client import N8nClient
from n8n_worker.config import load
from n8n_worker.script import load_script, parse_result, pick, resolve, spoken


async def main() -> None:
    config = load()
    script = load_script(config.script_file)
    client = N8nClient(config)
    correlation = uuid.uuid4().hex[:8].upper()
    start = await client.start(start_payload(
        config, room="script-check", correlation_id=correlation, identity="check", kind="SIP", attributes={}))
    print("greeting |", start.speak)
    if script.after_greeting:
        print("after    |", script.lines[script.after_greeting])
    server = mcp.MCPServerHTTP(
        url=config.mcp_url, transport_type=config.mcp_transport,
        headers={"Authorization": f"Bearer {config.mcp_token}"}, timeout=10, client_session_timeout_seconds=30)
    await server.initialize()
    remote = {t.info.name: t for t in await server.list_tools()}
    memory: dict = {}
    for step in sys.argv[1:]:
        name, _, rest = step.partition(":")
        raw = dict(pair.split("=", 1) for pair in rest.split(",") if "=" in pair)
        tool = next(t for t in script.tools if t.name == name)
        scope = {"args": raw, "state": memory, "result": {}}
        result = {"ok": True}
        ms = 0
        if tool.call:
            args = {k: resolve(v, scope) for k, v in tool.args.items()}
            args[script.session_arg] = start.session_id
            t0 = time.monotonic()
            result = parse_result(await remote[tool.call](raw_arguments=args))
            ms = int((time.monotonic() - t0) * 1000)
        scope["result"] = result
        for k, v in tool.capture.items():
            memory[k] = resolve(v, scope)
        outcome = pick(tool, result)
        print(f"{name} {ms}ms ok={result.get('ok')} err={result.get('error', '')} | {spoken(script, outcome, scope)}"
              f" [{outcome.action if outcome else 'no-outcome'}]")
    await server.aclose()
    await client.aclose()


asyncio.run(main())
