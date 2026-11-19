"""Worker configuration, read from the environment only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_FILLER = {"en": "One moment please.", "de": "Einen Moment bitte."}
DEFAULT_FALLBACK = {
    "en": "I am sorry, I cannot continue right now. I will connect you to a colleague.",
    "de": "Es tut mir leid, ich kann gerade nicht weitermachen. Ich verbinde Sie mit einem Kollegen.",
}


class ConfigError(ValueError):
    pass


def _lines(env: dict[str, str], prefix: str, defaults: dict[str, str]) -> dict[str, str]:
    lines = dict(defaults)
    for key, value in env.items():
        if key.startswith(prefix) and value:
            lines[key[len(prefix):].lower()] = value
    return lines


@dataclass(frozen=True)
class Config:
    n8n_base_url: str
    n8n_bearer: str
    mode: str = "turn"
    agent_name: str = ""
    path_start: str = "/ar/v1/session/start"
    path_turn: str = "/ar/v1/session/turn"
    path_end: str = "/ar/v1/session/end"
    transfer: str = "n8n"
    report_end: bool = False
    path_handoff: str = ""
    script_file: str = ""
    tts_cache_dir: str = ""
    tts_instructions: str = ""
    tts_speed: float = 1.0
    transcript_dir: str = ""
    end_call: str = "n8n"
    filler_after_ms: int = 2500
    turn_timeout_ms: int = 12000
    attributes_wait_ms: int = 800
    default_language: str = "en"
    send_caller_number: bool = True
    gateway_vendor: str = "n8n-livekit-worker"
    mcp_url: str = ""
    mcp_token: str = ""
    mcp_transport: str = "streamable_http"
    mcp_allowed_tools: tuple[str, ...] = ()
    realtime_model: str = "gpt-realtime"
    realtime_voice: str = "marin"
    realtime_base_url: str = ""
    realtime_api_key: str = ""
    realtime_instructions: str = ""
    vad_threshold: float = 0.7
    vad_silence_ms: int = 700
    tool_end: str = "EndCall"
    tool_handoff: str = "RequestAgent"
    test_vin: str = ""
    test_msisdn: str = ""
    stt_model: str = "gpt-4o-mini-transcribe"
    stt_base_url: str = ""
    stt_api_key: str = ""
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"
    tts_base_url: str = ""
    tts_api_key: str = ""
    filler_lines: dict[str, str] = field(default_factory=dict)
    fallback_lines: dict[str, str] = field(default_factory=dict)

    def line(self, lines: dict[str, str], language: str) -> str:
        return lines.get((language or "").lower()[:2]) or lines[self.default_language]

    def filler(self, language: str) -> str:
        return self.line(self.filler_lines, language)

    def fallback(self, language: str) -> str:
        return self.line(self.fallback_lines, language)


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    def need(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name} is required")
        return value

    mode = env.get("N8N_MODE", "turn").strip().lower()
    if mode not in ("turn", "realtime", "scripted"):
        raise ConfigError("N8N_MODE must be turn or realtime")
    instructions = env.get("REALTIME_INSTRUCTIONS", "")
    if env.get("REALTIME_INSTRUCTIONS_FILE"):
        try:
            with open(env["REALTIME_INSTRUCTIONS_FILE"], encoding="utf-8") as handle:
                instructions = handle.read()
        except OSError as error:
            raise ConfigError(f"REALTIME_INSTRUCTIONS_FILE: {error.strerror}") from None
    mcp_transport = env.get("N8N_MCP_TRANSPORT", "streamable_http").strip()
    if mode == "scripted" and not env.get("N8N_SCRIPT_FILE", "").strip():
        raise ConfigError("N8N_SCRIPT_FILE is required in scripted mode")
    if mode in ("realtime", "scripted"):
        if not env.get("N8N_MCP_URL", "").strip():
            raise ConfigError("N8N_MCP_URL is required in realtime mode")
        if mcp_transport not in ("streamable_http", "sse"):
            raise ConfigError("N8N_MCP_TRANSPORT must be streamable_http or sse")
    transfer = env.get("N8N_TRANSFER", "n8n").strip().lower()
    if transfer != "n8n":
        raise ConfigError("N8N_TRANSFER: only 'n8n' is implemented")
    end_call = env.get("N8N_END_CALL", "n8n").strip().lower()
    if end_call not in ("n8n", "worker"):
        raise ConfigError("N8N_END_CALL must be n8n or worker")
    filler_after = int(env.get("FILLER_AFTER_MS", "2500"))
    turn_timeout = int(env.get("TURN_TIMEOUT_MS", "12000"))
    if not 0 < filler_after < turn_timeout:
        raise ConfigError("FILLER_AFTER_MS must be above 0 and below TURN_TIMEOUT_MS")
    default_language = env.get("DEFAULT_LANGUAGE", "en").strip().lower()[:2]
    filler = _lines(env, "FILLER_LINE_", DEFAULT_FILLER)
    fallback = _lines(env, "FALLBACK_LINE_", DEFAULT_FALLBACK)
    if default_language not in filler or default_language not in fallback:
        raise ConfigError(f"no filler or fallback line for DEFAULT_LANGUAGE={default_language}")
    return Config(
        n8n_base_url=need("N8N_BASE_URL").rstrip("/"),
        n8n_bearer=need("N8N_BEARER"),
        mode=mode,
        agent_name=env.get("N8N_AGENT_NAME", "").strip(),
        path_start=env.get("N8N_PATH_START", "/ar/v1/session/start"),
        path_turn=env.get("N8N_PATH_TURN", "/ar/v1/session/turn"),
        path_end=env.get("N8N_PATH_END", "/ar/v1/session/end"),
        transfer=transfer,
        report_end=env.get("N8N_REPORT_END", "false").strip().lower() == "true",
        path_handoff=env.get("N8N_PATH_HANDOFF", "").strip(),
        script_file=env.get("N8N_SCRIPT_FILE", "").strip(),
        tts_cache_dir=env.get("TTS_CACHE_DIR", "").strip(),
        tts_instructions=env.get("TTS_INSTRUCTIONS", "").strip(),
        tts_speed=float(env.get("TTS_SPEED", "1.0")),
        transcript_dir=env.get("N8N_TRANSCRIPT_DIR", "").strip(),
        end_call=end_call,
        filler_after_ms=filler_after,
        turn_timeout_ms=turn_timeout,
        attributes_wait_ms=int(env.get("ATTRIBUTES_WAIT_MS", "800")),
        default_language=default_language,
        send_caller_number=env.get("N8N_SEND_CALLER_NUMBER", "true").strip().lower() != "false",
        gateway_vendor=env.get("N8N_GATEWAY_VENDOR", "n8n-livekit-worker"),
        mcp_url=env.get("N8N_MCP_URL", "").strip(),
        mcp_token=env.get("N8N_MCP_TOKEN", "").strip(),
        mcp_transport=mcp_transport,
        mcp_allowed_tools=tuple(t.strip() for t in env.get("N8N_MCP_ALLOWED_TOOLS", "").split(",") if t.strip()),
        realtime_model=env.get("REALTIME_MODEL", "gpt-realtime"),
        realtime_voice=env.get("REALTIME_VOICE", "marin"),
        realtime_base_url=env.get("REALTIME_BASE_URL", ""),
        realtime_api_key=env.get("REALTIME_API_KEY", ""),
        realtime_instructions=instructions.strip(),
        vad_threshold=float(env.get("REALTIME_VAD_THRESHOLD", "0.7")),
        vad_silence_ms=int(env.get("REALTIME_VAD_SILENCE_MS", "700")),
        tool_end=env.get("N8N_TOOL_END", "EndCall").strip(),
        tool_handoff=env.get("N8N_TOOL_HANDOFF", "RequestAgent").strip(),
        test_vin=env.get("TEST_CALLER_VIN", "").strip(),
        test_msisdn=env.get("TEST_CALLER_MSISDN", "").strip(),
        stt_model=env.get("STT_MODEL", "gpt-4o-mini-transcribe"),
        stt_base_url=env.get("STT_BASE_URL", ""),
        stt_api_key=env.get("STT_API_KEY", ""),
        tts_model=env.get("TTS_MODEL", "gpt-4o-mini-tts"),
        tts_voice=env.get("TTS_VOICE", "alloy"),
        tts_base_url=env.get("TTS_BASE_URL", ""),
        tts_api_key=env.get("TTS_API_KEY", ""),
        filler_lines=filler,
        fallback_lines=fallback,
    )
