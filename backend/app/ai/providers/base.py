"""프로바이더 공통 계약."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.domain.rewrite import ModelOutput


@dataclass(frozen=True)
class GenerationResult:
    output: ModelOutput
    model_version: str
    usage: dict[str, int] = field(default_factory=dict)


class LLMProvider(Protocol):
    """구조화 출력(ModelOutput)을 돌려준다. 실패는 ModelUnavailableError로 올린다."""

    async def generate(self, *, system: str, user: str) -> GenerationResult: ...
