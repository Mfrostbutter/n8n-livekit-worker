"""n8n in the LLM seat: each user turn is one POST to the n8n turn endpoint."""
from __future__ import annotations

import uuid
from typing import Any, Callable

from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr

from .client import N8nClient, TurnResult, resolve_turn
from .config import Config


def last_user_text(chat_ctx: llm.ChatContext) -> str:
    for item in reversed(chat_ctx.items):
        if getattr(item, "type", "") == "message" and item.role == "user":
            return (item.text_content or "").strip()
    return ""


class N8nLLM(llm.LLM):
    def __init__(
        self,
        *,
        client: N8nClient,
        config: Config,
        session_id: str,
        correlation_id: str,
        language: str,
        on_result: Callable[[TurnResult], None],
    ) -> None:
        super().__init__()
        self._client = client
        self._config = config
        self._session_id = session_id
        self._correlation_id = correlation_id
        self.turns = 0
        self._language = language
        self._on_result = on_result

    @property
    def model(self) -> str:
        return "n8n-turn"

    @property
    def provider(self) -> str:
        return "n8n"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> llm.LLMStream:
        return N8nStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class N8nStream(llm.LLMStream):
    def __init__(self, n8n: N8nLLM, **kwargs: Any) -> None:
        super().__init__(n8n, **kwargs)
        self._n8n = n8n
        # A turn is not replayed: n8n has already advanced its state.
        self._retry_on_chunk_sent = False

    def _send(self, text: str) -> None:
        self._event_ch.send_nowait(
            llm.ChatChunk(id=uuid.uuid4().hex, delta=llm.ChoiceDelta(role="assistant", content=text))
        )

    async def _run(self) -> None:
        n8n = self._n8n
        text = last_user_text(self._chat_ctx)
        if not text:
            return
        config = n8n._config
        n8n.turns += 1
        turn = n8n.turns
        result = await resolve_turn(
            lambda: n8n._client.turn(
                n8n._session_id, text, n8n._language, correlation_id=n8n._correlation_id, turn=turn
            ),
            lambda: self._send(config.filler(n8n._language) + " "),
            filler_after_s=config.filler_after_ms / 1000,
            timeout_s=config.turn_timeout_ms / 1000,
            fallback_line=config.fallback(n8n._language),
        )
        if result.speak:
            self._send(result.speak)
        n8n._on_result(result)
