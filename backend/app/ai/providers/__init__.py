"""LLM 프로바이더. 설정의 LLM_PROVIDER로 고른다."""

from __future__ import annotations

from app.ai.providers.base import GenerationResult, LLMProvider
from app.ai.providers.fake import FakeProvider
from app.core.config import Settings


def build_provider(settings: Settings) -> LLMProvider:
    """설정에 맞는 프로바이더를 만든다. anthropic SDK는 실제 호출 시점에만 import한다."""
    if settings.llm_provider == "anthropic":
        from app.ai.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    return FakeProvider()


__all__ = ["GenerationResult", "LLMProvider", "FakeProvider", "build_provider"]
