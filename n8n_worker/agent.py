"""LiveKit agent worker that makes n8n the brain of a call (turn mode)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, function_tool, mcp
from livekit.agents.llm import ToolResult
from livekit.plugins import openai, silero
from openai.types.beta.realtime.session import TurnDetection

from .client import N8nClient, TurnResult, render_instructions
from .config import Config, load
from .control import parse_control
from .llm import N8nLLM
from .voice_cache import VoiceCache
from .script import load_script, parse_result, pick, resolve, say_schema, spoken, tool_schema

class ScriptedAgent(Agent):
    """Scripted mode: text the model writes on its own is never spoken. Script lines carry their own audio."""

    async def tts_node(self, text: Any, model_settings: Any) -> Any:
        async for _ in text:
            pass
        return
        yield  # an empty audio stream


logger = logging.getLogger("n8n-worker")
config: Config = load()
# WORKER_HTTP_PORT=0 takes any free port, so several workers can run on one machine.
server = AgentServer(port=int(os.environ.get("WORKER_HTTP_PORT", "8081")))

UUI_ATTRIBUTES = ("sip.h.user-to-user", "sip.h.User-to-User", "sip.h.x-user-to-user")


def log(session_id: str, event: str) -> None:
    """Session id, event and UTC time only. Never text, audio or a phone number."""
    logger.info("%s %s %s", datetime.now(timezone.utc).isoformat(timespec="seconds"), session_id, event)


def start_payload(
    config: Config, *, room: str, correlation_id: str, identity: str, kind: str, attributes: dict[str, str]
) -> dict[str, Any]:
    """Body of the session start call. Carries the generic fields and the reference workflows' uui, sip and gateway."""
    raw = next((attributes[k] for k in UUI_ATTRIBUTES if attributes.get(k)), "")
    uui: dict[str, str] = {"version": "1", "raw": raw, "language": config.default_language}
    if not raw and config.test_vin:
        # Test stand-in for a call with no User-to-User header: a seeded identity, as the car would send it.
        uui["vin"] = config.test_vin
        uui["msisdn"] = config.test_msisdn
        uui["raw"] = ";".join(["1", correlation_id, config.test_vin, config.test_msisdn, config.default_language])
    elif config.send_caller_number and attributes.get("sip.phoneNumber"):
        uui["msisdn"] = attributes["sip.phoneNumber"]
    return {
        "sessionId": room,
        "correlationId": correlation_id,
        "room": room,
        "participant": {"identity": identity, "kind": kind, "attributes": attributes},
        "uui": uui,
        "sip": {"callId": attributes.get("sip.callID", "")},
        "gateway": {"vendor": config.gateway_vendor},
    }


async def wait_for_headers(room: rtc.Room, participant: rtc.RemoteParticipant, wait_ms: int) -> None:
    """SIP header attributes can arrive after the join. Wait briefly for them."""
    if any(key.startswith("sip.h.") for key in participant.attributes):
        return
    arrived = asyncio.Event()

    def on_change(changed: dict[str, str], who: rtc.Participant) -> None:
        if who.identity == participant.identity and any(k.startswith("sip.h.") for k in changed):
            arrived.set()

    room.on("participant_attributes_changed", on_change)
    try:
        await asyncio.wait_for(arrived.wait(), wait_ms / 1000)
    except asyncio.TimeoutError:
        pass
    finally:
        room.off("participant_attributes_changed", on_change)


def speech_kwargs(base_url: str, api_key: str) -> dict[str, str]:
    kwargs = {}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    return kwargs


@server.rtc_session(agent_name=config.agent_name)
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    started = time.monotonic()
    correlation_id = str(uuid.uuid4())
    log_id = ctx.job.id
    participant = await ctx.wait_for_participant()
    log(log_id, "participant_joined")
    is_sip = participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    if is_sip:
        await wait_for_headers(ctx.room, participant, config.attributes_wait_ms)

    client = N8nClient(config)
    payload = start_payload(
        config,
        room=ctx.room.name,
        correlation_id=correlation_id,
        identity=participant.identity,
        kind=rtc.ParticipantKind.Name(participant.kind).replace("PARTICIPANT_KIND_", ""),
        attributes=dict(participant.attributes),
    )
    try:
        start = await client.start(payload)
    except Exception:
        start = None
    session_id = start.session_id if start and start.session_id else ctx.room.name
    if start:
        log_id = session_id
    language = start.language if start else config.default_language
    greeting = start.speak if start and start.speak else config.fallback(language)
    log(log_id, "start_ok" if start else "start_failed")

    state = {"reason": "caller_hangup", "reported": False}
    pending: list[asyncio.Task[None]] = []
    n8n_llm: N8nLLM | None = None

    async def report_end() -> None:
        """One end record per session, whatever ended it."""
        if state["reported"]:
            return
        state["reported"] = True
        if config.report_end and start:
            try:
                await client.end(
                    session_id, str(state["reason"]), correlation_id=correlation_id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    turns=n8n_llm.turns if n8n_llm else turns["count"],
                )
                log(log_id, "end_reported")
            except Exception:
                log(log_id, "end_report_failed")
        await client.aclose()

    ctx.add_shutdown_callback(report_end)
    session: AgentSession

    async def act(action: str, settle_s: float = 0.3, handle: Any = None) -> None:
        # Let the sentence finish before acting on it.
        await asyncio.sleep(settle_s)
        handle = handle or session.current_speech
        if handle is not None:
            await handle.wait_for_playout()
        if action == "handoff":
            # n8n sends the REFER (LiveKit node). Stop listening and wait.
            state["reason"] = "handoff"
            session.input.set_audio_enabled(False)
            log(log_id, "handoff_waiting")
            if config.path_handoff:
                try:
                    done = await client.handoff(
                        session_id, room=ctx.room.name, identity=participant.identity,
                        summary=str(state.get("summary", "")), correlation_id=correlation_id,
                    )
                except Exception:
                    done = False
                log(log_id, "handoff_done" if done else "handoff_failed")
                if not done:
                    state["reason"] = "handoff_failed"
                    session.say(config.fallback(language))
        elif action == "end":
            state["reason"] = "completed"
            log(log_id, "end")
            if config.end_call == "worker":
                # Without the LiveKit node on the n8n side, the worker hangs up by deleting the room.
                try:
                    await ctx.delete_room()
                except Exception:
                    log(log_id, "delete_room_failed")
            ctx.shutdown("end")

    def on_result(result: TurnResult) -> None:
        log(log_id, "turn_failed" if result.failed else f"turn_{result.action}")
        if result.action != "none":
            pending.append(asyncio.create_task(act(result.action)))

    turns = {"count": 0}
    agent_instructions = ""
    local_tools: list[Any] = []

    def transcript(role: str, text: str, **extra: Any) -> None:
        """Opt-in test aid (N8N_TRANSCRIPT_DIR): one JSON line per utterance or tool call. Off by default."""
        if not config.transcript_dir or not text:
            return
        os.makedirs(config.transcript_dir, exist_ok=True)
        row = {"ms": int((time.monotonic() - started) * 1000), "role": role, "text": text, **extra}
        with open(os.path.join(config.transcript_dir, f"{session_id}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    mcp_headers = {"Authorization": f"Bearer {config.mcp_token}"} if config.mcp_token else {}
    if config.mode == "scripted":
        # The model only listens and picks a tool. Every spoken word comes from the script, through one TTS voice.
        script = load_script(config.script_file)
        if script.greeting:
            # The script's own opening replaces the line n8n returned, so the order of the opening is fixed here.
            greeting = script.greeting
        agent_instructions = script.instructions
        remote = mcp.MCPServerHTTP(
            url=(start.mcp_url if start and start.mcp_url else config.mcp_url), transport_type=config.mcp_transport,
            headers=mcp_headers, timeout=10, client_session_timeout_seconds=30,
        )
        await remote.initialize()
        remote_tools = {t.info.name: t for t in await remote.list_tools()}
        ctx.add_shutdown_callback(remote.aclose)
        memory: dict[str, Any] = {}

        voice = start.voice if start and start.voice else config.tts_voice
        engine = openai.TTS(
            model=config.tts_model, voice=voice, speed=config.tts_speed,
            **({"instructions": config.tts_instructions} if config.tts_instructions else {}),
            **speech_kwargs(config.tts_base_url, config.tts_api_key),
        )
        cache = VoiceCache(
            engine, config.tts_cache_dir or os.path.join(os.getcwd(), ".cache", "tts"),
            f"{config.tts_model}|{voice}|{config.tts_speed}|{config.tts_instructions}",
        )

        async def render(text: str) -> None:
            try:
                await cache.warm(text)
            except Exception:
                log(log_id, "voice_cache_failed")

        def play(text: str, interruptible: bool = True) -> Any:
            """Every line is spoken from its own audio: cached sentences replay, new ones are synthesized once."""
            log(log_id, "voice_cached" if cache.cached(text) else "voice_live")
            return session.say(text, audio=cache.stream(text), allow_interruptions=interruptible)

        # Lines without a variable part are rendered ahead of the first use.
        for fixed in [greeting] + [line for line in script.lines.values() if "{" not in line]:
            pending.append(asyncio.create_task(render(fixed)))

        last_call = {"key": "", "at": 0.0}

        def speak(text: str, action: str) -> None:
            handle = play(text, action == "none") if text else None
            if action != "none":
                pending.append(asyncio.create_task(act(action, settle_s=0.2, handle=handle)))

        def scripted_tool(tool: Any) -> Any:
            @function_tool(raw_schema=tool_schema(tool))
            async def run(raw_arguments: dict[str, Any]) -> ToolResult:
                # The same call twice in a row is one caller turn answered twice; speak once.
                key = tool.name + json.dumps(raw_arguments, sort_keys=True)
                if key == last_call["key"] and time.monotonic() - last_call["at"] < 5:
                    log(log_id, f"tool_{tool.name}_duplicate")
                    return ToolResult(json.dumps({"ok": True, "duplicate": True}), reply_required=False)
                last_call.update(key=key, at=time.monotonic())
                turns["count"] += 1
                scope: dict[str, Any] = {"args": raw_arguments, "state": memory, "result": {}}
                result: dict[str, Any] = {"ok": True}
                if tool.call:
                    args = {key: resolve(value, scope) for key, value in tool.args.items()}
                    args[script.session_arg] = session_id
                    t0 = time.monotonic()
                    try:
                        result = parse_result(await remote_tools[tool.call](raw_arguments=args))
                    except Exception:
                        result = {"ok": False, "error": "tool_failed"}
                    log(log_id, f"tool_{tool.call}_{int((time.monotonic() - t0) * 1000)}ms")
                scope["result"] = result
                for key, value in tool.capture.items():
                    memory[key] = resolve(value, scope)
                if tool.name == config.tool_handoff:
                    state["summary"] = str(raw_arguments.get("summary", ""))
                outcome = pick(tool, result)
                line = spoken(script, outcome, scope)
                transcript("tool", tool.name, arguments=raw_arguments, output=result, line=outcome.line if outcome else "")
                speak(line, outcome.action if outcome else "none")
                # The result stays in the model's context; it does not get to answer.
                return ToolResult(json.dumps({"ok": result.get("ok"), "said": line}), reply_required=False)

            return run

        @function_tool(raw_schema=say_schema(script))
        async def say_line(raw_arguments: dict[str, Any]) -> ToolResult:
            key = str(raw_arguments.get("line", ""))
            line = resolve(script.lines.get(key, script.error_line), {"args": raw_arguments, "state": memory, "result": {}})
            transcript("tool", script.say_name, arguments=raw_arguments, line=key)
            log(log_id, f"say_{key}")
            if line:
                speak(line, "none")
            return ToolResult(json.dumps({"said": line}), reply_required=False)

        local_tools.extend([scripted_tool(t) for t in script.tools] + [say_line])
        session = AgentSession(
            llm=openai.realtime.RealtimeModel(
                model=config.realtime_model, modalities=["text"],
                turn_detection=TurnDetection(
                    type="server_vad", threshold=config.vad_threshold, prefix_padding_ms=300,
                    silence_duration_ms=config.vad_silence_ms, create_response=True, interrupt_response=True,
                ),
                **speech_kwargs(config.realtime_base_url, config.realtime_api_key),
            ),
            tts=engine,
        )
    elif config.mode == "realtime":
        # Speech-to-speech model holds the dialogue; n8n is its toolset over MCP.
        template = (start.instructions if start else "") or config.realtime_instructions
        agent_instructions = render_instructions(template, start) if start else ""
        session = AgentSession(
            llm=openai.realtime.RealtimeModel(
                model=config.realtime_model,
                voice=(start.voice if start and start.voice else config.realtime_voice),
                turn_detection=TurnDetection(
                    type="server_vad", threshold=config.vad_threshold, prefix_padding_ms=300,
                    silence_duration_ms=config.vad_silence_ms, create_response=True, interrupt_response=True,
                ),
                **speech_kwargs(config.realtime_base_url, config.realtime_api_key),
            ),
            mcp_servers=[
                mcp.MCPServerHTTP(
                    url=(start.mcp_url if start and start.mcp_url else config.mcp_url),
                    transport_type=config.mcp_transport,
                    headers=mcp_headers,
                    allowed_tools=list(config.mcp_allowed_tools) or None,
                    timeout=10,
                    client_session_timeout_seconds=30,
                )
            ],
        )

        if config.path_handoff:
            # The model only announces the handoff; n8n transfers the caller once the sentence has played.
            @function_tool(
                name=config.tool_handoff,
                description="Transfers the call to a human agent. Call only when the caller asks for a person "
                "or the request cannot be served. Pass a one-line summary of the call so far.",
            )
            async def request_handoff(summary: str) -> str:
                state["summary"] = summary
                return "Transfer requested. Say the handover line and stop talking."

            local_tools.append(request_handoff)

        def on_tools(event: Any) -> None:
            # Tool names only. n8n has already done the work; the worker acts on end and handoff.
            for call, output in zip(event.function_calls, event.function_call_outputs):
                transcript("tool", call.name, arguments=call.arguments, output=getattr(output, "output", None))
            for call in event.function_calls:
                turns["count"] += 1
                log(log_id, f"tool_{call.name}")
                if call.name == config.tool_end:
                    pending.append(asyncio.create_task(act("end", settle_s=1.5)))
                elif call.name == config.tool_handoff:
                    pending.append(asyncio.create_task(act("handoff", settle_s=1.5)))

        session.on("function_tools_executed", on_tools)
    else:
        n8n_llm = N8nLLM(
            client=client, config=config, session_id=session_id, correlation_id=correlation_id,
            language=language, on_result=on_result,
        )
        session = AgentSession(
            stt=openai.STT(
                model=config.stt_model, language=language[:2],
                **speech_kwargs(config.stt_base_url, config.stt_api_key),
            ),
            llm=n8n_llm,
            tts=openai.TTS(
                model=config.tts_model, voice=(start.voice if start and start.voice else config.tts_voice),
                **speech_kwargs(config.tts_base_url, config.tts_api_key),
            ),
            vad=silero.VAD.load(),
        )

    def on_metrics(event: Any) -> None:
        # Numbers only: stage name and milliseconds.
        m = event.metrics
        for field in ("ttft", "ttfb", "end_of_utterance_delay", "transcription_delay", "duration"):
            value = getattr(m, field, None)
            if isinstance(value, (int, float)) and value > 0:
                log(log_id, f"metric_{type(m).__name__}_{field}_{int(value * 1000)}ms")

    session.on("metrics_collected", on_metrics)

    def on_item(event: Any) -> None:
        role = getattr(event.item, "role", "")
        if role in ("user", "assistant"):
            transcript(role, getattr(event.item, "text_content", "") or "")

    session.on("conversation_item_added", on_item)

    def on_heard(event: Any) -> None:
        if getattr(event, "is_final", False):
            transcript("heard", getattr(event, "transcript", "") or "")

    session.on("user_input_transcribed", on_heard)

    def on_data(packet: rtc.DataPacket) -> None:
        # Only server-sent control messages count.
        if packet.participant is not None:
            return
        control = parse_control(packet.data, packet.topic)
        if control is None:
            return
        log(log_id, f"control_{control.type}")
        if control.type == "say":
            session.say(control.text)
        elif control.type == "end":
            state["reason"] = "control_end"
            ctx.shutdown("control_end")

    ctx.room.on("data_received", on_data)

    # record=False: no audio or transcript leaves the process (N8N_TRANSCRIPT_DIR writes a local test file).
    agent = (ScriptedAgent if config.mode == "scripted" else Agent)(instructions=agent_instructions, tools=local_tools)
    await session.start(agent=agent, room=ctx.room, record=False)
    if config.mode == "scripted":
        # The tools exist only now; before this the API refuses a required tool choice.
        agent.realtime_llm_session.update_options(tool_choice="required")
    # The greeting is not interruptible: VAD cut it short on phone calls otherwise.
    if config.mode == "realtime":
        # A realtime model does not speak first; it is told to say the greeting.
        session.generate_reply(
            instructions="Say exactly this greeting, word for word, then stop and wait: " + greeting,
            allow_interruptions=False,
        )
    elif config.mode == "scripted":
        # The caller is not heard during the opening: line noise cannot cut it short.
        session.input.set_audio_enabled(False)
        prompt = script.lines[script.after_greeting] if script.after_greeting else ""
        lengths = [cache.duration(text) for text in (greeting, prompt) if text]
        opening = play(greeting)
        if prompt:
            opening = play(prompt)

        async def open_microphone() -> None:
            # Listening starts just before the opening ends, so an early answer is not lost.
            if all(length is not None for length in lengths):
                await asyncio.sleep(max(0.0, sum(lengths) - 0.8))
            else:
                await opening.wait_for_playout()
            session.input.set_audio_enabled(True)
            log(log_id, "listening")

        pending.append(asyncio.create_task(open_microphone()))
    else:
        session.say(greeting, allow_interruptions=False)
    log(log_id, "greeting")
    if start is None:
        on_result(TurnResult(speak="", action="handoff", reason="U;PE", failed=True))
    elif start.action != "none":
        on_result(TurnResult(speak="", action=start.action))
