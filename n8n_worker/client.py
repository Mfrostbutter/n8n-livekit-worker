"""n8n contract client: session start, turn and end."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

from .config import Config

ACTIONS = ("none", "end", "handoff")
HANDOFF_REASON_TIMEOUT = "U;PE"


@dataclass(frozen=True)
class StartResult:
    session_id: str
    speak: str
    language: str
    voice: str = ""
    action: str = "none"
    instructions: str = ""
    mcp_url: str = ""
    dealer: str = ""


@dataclass(frozen=True)
class TurnResult:
    speak: str
    action: str = "none"
    interruptible: bool = True
    reason: str = ""
    failed: bool = False


def normalize_action(value: Any) -> str:
    """none, end or handoff. The reference workflows say continue for none; anything unknown is none."""
    action = str(value or "none").lower()
    return action if action in ACTIONS else "none"


def parse_turn(body: Any) -> TurnResult:
    """Turn response to a TurnResult. An unknown action is treated as none."""
    if not isinstance(body, dict):
        raise ValueError("turn response is not an object")
    action = normalize_action(body.get("action"))
    return TurnResult(
        speak=str(body.get("speak") or ""),
        action=action,
        interruptible=body.get("interruptible", True) is not False,
        reason=str(body.get("reason") or ""),
    )


def parse_start(body: Any, payload: dict[str, Any], default_language: str) -> StartResult:
    """Start response to a StartResult. n8n may issue its own session id; the room name is the default."""
    body = body if isinstance(body, dict) else {}
    return StartResult(
        session_id=str(body.get("sessionId") or payload.get("sessionId") or ""),
        speak=str(body.get("speak") or ""),
        language=str(body.get("language") or default_language),
        voice=str(body.get("voice") or ""),
        action=normalize_action(body.get("action")),
        instructions=str(body.get("instructions") or ""),
        mcp_url=str(body.get("mcpUrl") or ""),
        dealer=str((body.get("customer") or {}).get("dealer") or "") if isinstance(body.get("customer"), dict) else "",
    )


LANGUAGE_NAMES = {"en": "English", "de": "German", "pl": "Polish", "es": "Spanish", "fr": "French", "it": "Italian"}


def render_instructions(template: str, start: StartResult) -> str:
    """Fill {{sessionId}}, {{language}}, {{languageName}} and {{dealer}} in the realtime instructions."""
    code = (start.language or "en").lower()[:2]
    values = {
        "sessionId": start.session_id,
        "language": code,
        "languageName": LANGUAGE_NAMES.get(code, "English"),
        "dealer": start.dealer or "none",
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


class N8nClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession | None = None) -> None:
        self._config = config
        self._session = session

    async def _post(self, path: str, body: dict[str, Any], timeout_s: float) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        async with self._session.post(
            self._config.n8n_base_url + path,
            json=body,
            headers={"Authorization": f"Bearer {self._config.n8n_bearer}"},
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as response:
            if response.status >= 400:
                raise RuntimeError(f"n8n answered {response.status}")
            text = await response.text()
            return await response.json(content_type=None) if text.strip() else {}

    async def start(self, payload: dict[str, Any]) -> StartResult:
        body = await self._post(self._config.path_start, payload, self._config.turn_timeout_ms / 1000)
        return parse_start(body, payload, self._config.default_language)

    async def turn(
        self, session_id: str, text: str, language: str, *, correlation_id: str = "", turn: int = 0
    ) -> TurnResult:
        # transcript is the reference workflows' name for the caller's words; text is the generic one.
        body = await self._post(
            self._config.path_turn,
            {
                "sessionId": session_id, "correlationId": correlation_id,
                "transcript": text, "text": text, "language": language, "turn": turn,
            },
            self._config.turn_timeout_ms / 1000,
        )
        return parse_turn(body)

    async def end(
        self, session_id: str, reason: str, *, correlation_id: str = "", duration_ms: int = 0, turns: int = 0
    ) -> None:
        await self._post(
            self._config.path_end,
            {
                "sessionId": session_id, "correlationId": correlation_id,
                "endReason": reason, "reason": reason, "durationMs": duration_ms, "turns": turns,
            },
            10,
        )

    async def handoff(
        self, session_id: str, *, room: str, identity: str, summary: str = "", correlation_id: str = ""
    ) -> bool:
        """Asks n8n to transfer the caller. True when n8n reports the transfer as done."""
        body = await self._post(
            self._config.path_handoff,
            {
                "sessionId": session_id, "correlationId": correlation_id,
                "room": room, "identity": identity, "summary": summary,
            },
            45,
        )
        return bool(isinstance(body, dict) and body.get("ok"))

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()


async def resolve_turn(
    request: Callable[[], Awaitable[TurnResult]],
    on_filler: Callable[[], None],
    *,
    filler_after_s: float,
    timeout_s: float,
    fallback_line: str,
) -> TurnResult:
    """Run one turn. Filler after filler_after_s; a failure or timeout_s becomes the fallback handoff."""
    task = asyncio.ensure_future(request())
    failure = TurnResult(
        speak=fallback_line, action="handoff", interruptible=False,
        reason=HANDOFF_REASON_TIMEOUT, failed=True,
    )
    try:
        try:
            return await asyncio.wait_for(asyncio.shield(task), filler_after_s)
        except asyncio.TimeoutError:
            on_filler()
        return await asyncio.wait_for(task, timeout_s - filler_after_s)
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception:
        task.cancel()
        return failure
