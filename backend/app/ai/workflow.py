"""첨삭 고정 워크플로 (project-design §5.3 / design.md §3).

검증 → 의도·보존 항목 → 검색 → 프롬프트 → 생성 → 검증 → (실패 시 재생성 1회) → 추천 또는 원문 유지.
LangGraph 없이 함수 조합으로 시작한다. 상태 추적이 필요해지면 그때 바꾼다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.ai.intent import classify_intent
from app.ai.preserve import extract_preserved_items
from app.ai.prompt import build_prompt, build_regenerate_suffix
from app.ai.providers.base import LLMProvider
from app.ai.retrieval.base import Retriever
from app.ai.validation import validate_output
from app.core.errors import ModelUnavailableError
from app.domain.contact import Contact
from app.domain.persona import PersonaSettings
from app.domain.rewrite import (
    Condition,
    ModelOutput,
    RetrievedExample,
    RewriteInput,
    RewriteResult,
    ValidationReport,
    Violation,
)

log = logging.getLogger(__name__)

MAX_REGENERATIONS = 1


@dataclass
class RewriteWorkflow:
    provider: LLMProvider
    retrievers: dict[Condition, Retriever]
    prompt_version: str

    async def run(
        self, inp: RewriteInput, contact: Contact, persona: PersonaSettings | None
    ) -> RewriteResult:
        t0 = time.perf_counter()
        intent = classify_intent(inp.draft_text, inp.situation)
        preserved = extract_preserved_items(inp.draft_text)

        # A 조건은 페르소나를 쓰지 않는다 (기준선).
        persona_used = None if inp.condition == "A" else persona

        retriever = self.retrievers[inp.condition]
        retrieved = await self._retrieve_safely(retriever, inp)

        prompt = build_prompt(
            version=self.prompt_version,
            contact=contact,
            persona=persona_used,
            intent=intent,
            preserved=preserved,
            draft_text=inp.draft_text,
            situation=inp.situation,
            retrieved=retrieved,
        )

        attempts = 0
        usage: dict[str, int] = {}
        model_version = "unknown"
        last_report: ValidationReport | None = None
        user_prompt = prompt.user
        output: ModelOutput | None = None

        for attempt in range(1 + MAX_REGENERATIONS):
            attempts = attempt + 1
            try:
                gen = await self.provider.generate(system=prompt.system, user=user_prompt)
            except ModelUnavailableError as exc:
                log.info("model unavailable attempt=%d: %s", attempts, exc.message)
                last_report = ValidationReport(
                    passed=False, violations=[Violation(check="empty", detail=exc.message)]
                )
                break
            model_version = gen.model_version
            for k, v in gen.usage.items():
                usage[k] = usage.get(k, 0) + v
            output = gen.output
            last_report = validate_output(inp.draft_text, output, preserved, persona_used, retrieved)
            if last_report.passed:
                break
            user_prompt = (
                prompt.user
                + "\n\n"
                + build_regenerate_suffix(self.prompt_version, output.rewritten_text, last_report.violations)
            )

        latency_ms = int((time.perf_counter() - t0) * 1000)
        assert last_report is not None

        if output is not None and last_report.passed:
            return RewriteResult(
                status="recommended",
                result_text=output.rewritten_text.strip(),
                change_reasons=output.change_reasons,
                warnings=output.warnings,
                evidence_ids=output.evidence_ids,
                intent=intent,
                preserved_items=preserved,
                validation=last_report,
                attempts=attempts,
                model_version=model_version,
                prompt_version=self.prompt_version,
                retrieval_version=retriever.version,
                token_usage=usage,
                latency_ms=latency_ms,
            )

        # 재생성까지 실패 또는 모델 장애: 원문 유지, 자동 전송 없음.
        return RewriteResult(
            status="fallback_original",
            result_text=inp.draft_text,
            change_reasons=[],
            warnings=["추천을 만들지 못해 원문을 유지했습니다."] + [v.detail for v in last_report.violations],
            evidence_ids=[],
            intent=intent,
            preserved_items=preserved,
            validation=last_report,
            attempts=attempts,
            model_version=model_version,
            prompt_version=self.prompt_version,
            retrieval_version=retriever.version,
            token_usage=usage,
            latency_ms=latency_ms,
        )

    @staticmethod
    async def _retrieve_safely(retriever: Retriever, inp: RewriteInput) -> list[RetrievedExample]:
        """검색 장애는 이력 미사용으로 축소 동작한다 (project-design §9).

        소유권 검증 실패는 서비스 계층에서 먼저 막히므로 여기까지 오지 않는다.
        """
        try:
            return await retriever.retrieve(
                user_id=inp.user_id,
                contact_id=inp.contact_id,
                draft_text=inp.draft_text,
                situation=inp.situation,
                as_of=inp.as_of,
            )
        except Exception as exc:  # noqa: BLE001 - 검색 계층의 어떤 장애든 축소 동작
            log.warning("retrieval failed, continuing without history: %s", exc)
            return []
