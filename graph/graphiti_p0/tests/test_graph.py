"""Graphiti 클라이언트 구성 테스트 (네트워크 없음)."""

from __future__ import annotations

import asyncio
from typing import Any

from graphiti_core.llm_client.config import LLMConfig

from p0.graph import GraphAnthropicClient, PassthroughReranker, _MessagesWithoutTemperature


class _StubMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.extra = "visible"

    async def create(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "ok"


def test_wrapper_strips_temperature_only() -> None:
    stub = _StubMessages()
    wrapped = _MessagesWithoutTemperature(stub)
    result = asyncio.run(wrapped.create(model="m", max_tokens=5, temperature=0.0, messages=[]))
    assert result == "ok"
    assert stub.calls == [{"model": "m", "max_tokens": 5, "messages": []}]
    assert wrapped.extra == "visible"  # 다른 속성은 그대로 통과


def test_client_installs_wrapper() -> None:
    client = GraphAnthropicClient(config=LLMConfig(api_key="test-key", model="claude-haiku-4-5"))
    assert isinstance(client.client.messages, _MessagesWithoutTemperature)


def test_passthrough_reranker_keeps_order() -> None:
    ranked = asyncio.run(PassthroughReranker().rank("q", ["a", "b", "c"]))
    assert [p for p, _ in ranked] == ["a", "b", "c"]
    assert [s for _, s in ranked] == [3.0, 2.0, 1.0]
