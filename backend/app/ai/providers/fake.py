"""네트워크 없이 동작하는 가짜 프로바이더.

- 기본 동작: 프롬프트의 [원문] 블록을 그대로 돌려준다 (사실 보존 100%, 문체 변화 0). UI·파이프라인 점검용.
  문체 예시가 프롬프트에 있으면 첫 번째 예시의 id를 '참고했다'고 보고한다 — 근거 표시(evidence)까지 화면에서
  확인할 수 있게 하기 위해서다. 예시가 없으면(A·B 조건) 근거도 없다.
- 테스트: `ScriptedProvider`에 출력 목록을 넣어 검증 실패 → 재생성 → 성공/실패 경로를 재현한다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.ai.providers.base import GenerationResult
from app.core.errors import ModelUnavailableError
from app.domain.rewrite import ModelOutput

_DRAFT_BLOCK = re.compile(r"\[원문\]\n(.*?)\n\n\[문체 예시", re.S)
_STYLE_BLOCK = re.compile(r"\[문체 예시[^\n]*\n(.*?)(?:\n\n\[|\Z)", re.S)
_EXAMPLE_ID = re.compile(r"^- id=(\S+) ", re.M)


def extract_draft(user_prompt: str) -> str:
    m = _DRAFT_BLOCK.search(user_prompt)
    return m.group(1).strip() if m else user_prompt.strip()


def first_style_example_id(user_prompt: str) -> str | None:
    """프롬프트의 [문체 예시] 블록에서 첫 번째 예시 id. 없으면 None."""
    block = _STYLE_BLOCK.search(user_prompt)
    found = _EXAMPLE_ID.search(block.group(1)) if block else None
    return found.group(1) if found else None


class FakeProvider:
    """원문을 그대로 돌려주는 기준선. 호출 횟수를 기록한다."""

    model_version = "fake-echo"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *, system: str, user: str) -> GenerationResult:
        self.calls += 1
        draft = extract_draft(user)
        cited = first_style_example_id(user)
        return GenerationResult(
            output=ModelOutput(
                rewritten_text=draft,
                change_reasons=["fake 프로바이더: 원문을 그대로 반환했습니다."],
                evidence_ids=[cited] if cited else [],
                warnings=[],
            ),
            model_version=self.model_version,
            # 사용량 제어가 토큰을 세므로 글자 수 기반 추정값을 보고한다 (한글 ~2자/토큰)
            usage={
                "input_tokens": max(len(system) + len(user), 1) // 2,
                "output_tokens": max(len(draft), 1) // 2,
            },
        )


class ScriptedProvider:
    """미리 정한 출력을 순서대로 돌려준다. 출력 대신 예외를 넣으면 그 호출에서 예외를 낸다."""

    model_version = "fake-scripted"

    def __init__(self, outputs: Sequence[ModelOutput | Exception]) -> None:
        self._outputs = list(outputs)
        self.calls = 0
        self.prompts: list[str] = []

    async def generate(self, *, system: str, user: str) -> GenerationResult:
        self.calls += 1
        self.prompts.append(user)
        if not self._outputs:
            raise ModelUnavailableError("스크립트된 출력이 더 없습니다.")
        nxt = self._outputs.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return GenerationResult(
            output=nxt, model_version=self.model_version, usage={"input_tokens": 1, "output_tokens": 1}
        )
