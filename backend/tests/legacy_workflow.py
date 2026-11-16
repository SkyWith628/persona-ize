"""2026-11-14 LangGraph로 옮기기 전의 구현을 대조용으로 얼려 둔 것 — 테스트에서만 쓴다. 고치지 않는다.

커밋 7b54c7e의 `backend/app/ai/workflow.py` 그대로다. 클래스 이름만 `LegacyRewriteWorkflow`로 바꿨다.
`tests/test_workflow_parity.py`가 같은 입력을 이 구현과 `app.ai.workflow.RewriteWorkflow`에 넣어 견준다.
예외 하나: 결과에 `validator_version`을 찍는 두 줄(2026-11-16)은 지금 구현과 같이 넣었다.
새 필드라 옛 구현에는 없었고, 빼 두면 대조가 모든 시나리오에서 이 필드 하나로 어긋난다.
아래는 원래 머리말.

첨삭 고정 워크플로 (project-design §5.3 / design.md §3).

검증 → 의도·보존 항목 → 검색 → 프롬프트 → 생성 → 검증 → (실패 시 재생성 1회) → 추천 또는 원문 유지.
LangGraph 없이 함수 조합으로 시작한다. 상태 추적이 필요해지면 그때 바꾼다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.ai.intent import classify_intent
from app.ai.preserve import extract_preserved_items
from app.ai.prompt import build_prompt, build_regenerate_suffix, fit_examples, today_kst
from app.ai.providers.base import LLMProvider
from app.ai.retrieval.base import Retriever
from app.ai.validation import validate_output
from app.core.errors import ModelRefusalError, ModelUnavailableError
from app.domain.contact import Contact
from app.domain.conversation import exception_kind
from app.domain.persona import PersonaSettings, apply_adjust
from app.domain.rewrite import (
    AttemptRecord,
    Condition,
    ModelOutput,
    RetrievedExample,
    RewriteInput,
    RewriteResult,
    ValidationReport,
    Violation,
)
from app.domain.titles import VALIDATOR_VERSION
from app.domain.usage import estimate_tokens

log = logging.getLogger(__name__)

MAX_REGENERATIONS = 1


@dataclass
class LegacyRewriteWorkflow:
    provider: LLMProvider
    retrievers: dict[Condition, Retriever]
    prompt_version: str

    async def run(
        self, inp: RewriteInput, contact: Contact, persona: PersonaSettings | None
    ) -> RewriteResult:
        t0 = time.perf_counter()
        if inp.condition not in self.retrievers:
            # auto는 RewriteService가 A~D로 바꿔서 넘긴다. 여기까지 오면 호출 쪽 실수다.
            raise ValueError(f"워크플로는 A~D 조건만 받습니다: {inp.condition}")
        intent = classify_intent(inp.draft_text, inp.situation)
        preserved = extract_preserved_items(inp.draft_text)

        # A 조건은 페르소나를 쓰지 않는다 (기준선). 조정(adjust)은 이번 요청의 사본에만 덮어쓴다.
        persona_used = None if inp.condition == "A" else apply_adjust(persona, inp.adjust)

        retriever = self.retrievers[inp.condition]
        # 검색 결과는 프롬프트 예산에 맞게 줄여서 쓴다 (예시 하나·전체 글자 상한).
        # 검증과 근거 참조도 줄인 목록 기준이다.
        retrieved = fit_examples(await self._retrieve_safely(retriever, inp))

        prompt = build_prompt(
            version=self.prompt_version,
            contact=contact,
            persona=persona_used,
            intent=intent,
            preserved=preserved,
            draft_text=inp.draft_text,
            situation=inp.situation,
            retrieved=retrieved,
            today=today_kst(inp.as_of),  # 검색 기준 시각과 같은 날을 '오늘'로 준다
            adjust=inp.adjust,
            contact_known=inp.condition != "A",  # 설정이 없어도(B~D) 누구에게 쓰는지는 밝힌다
        )

        attempts = 0
        usage: dict[str, int] = {}
        model_version = "unknown"
        last_report: ValidationReport | None = None
        user_prompt = prompt.user
        output: ModelOutput | None = None
        attempt_log: list[AttemptRecord] = []

        for attempt in range(1 + MAX_REGENERATIONS):
            attempts = attempt + 1
            try:
                gen = await self.provider.generate(system=prompt.system, user=user_prompt)
            except ModelRefusalError as exc:
                # 거절은 장애가 아니다. 위반처럼 기록하고 재생성 1회를 시도한다 (11-08 E003).
                log.info("model refusal attempt=%d", attempts)
                self._count_unmeasured(usage, prompt.system, user_prompt)
                last_report = ValidationReport(
                    passed=False, violations=[Violation(check="refusal", detail=exc.message)]
                )
                attempt_log.append(
                    AttemptRecord(attempt=attempts, passed=False, violations=last_report.violations)
                )
                output = None
                user_prompt = (
                    prompt.user
                    + "\n\n"
                    + build_regenerate_suffix(self.prompt_version, "(출력 없음)", last_report.violations)
                )
                continue
            except ModelUnavailableError as exc:
                log.info("model unavailable attempt=%d: %s", attempts, exc.message)
                self._count_unmeasured(usage, prompt.system, user_prompt)
                last_report = ValidationReport(
                    passed=False, violations=[Violation(check="empty", detail=exc.message)]
                )
                attempt_log.append(
                    AttemptRecord(attempt=attempts, passed=False, violations=last_report.violations)
                )
                break
            model_version = gen.model_version
            for k, v in gen.usage.items():
                usage[k] = usage.get(k, 0) + v
            output = gen.output
            last_report = validate_output(inp.draft_text, output, preserved, persona_used, retrieved)
            attempt_log.append(
                AttemptRecord(
                    attempt=attempts,
                    passed=last_report.passed,
                    violations=last_report.violations,
                    rewritten_text=output.rewritten_text,
                )
            )
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
            cited = set(output.evidence_ids)
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
                attempt_log=attempt_log,
                model_version=model_version,
                prompt_version=self.prompt_version,
                retrieval_version=retriever.version,
                token_usage=usage,
                latency_ms=latency_ms,
                validator_version=VALIDATOR_VERSION,
                adjust=inp.adjust,
                # 근거의 출처 메시지 참조만 남긴다. 원문 사본은 두지 않는다 —
                # 근거 조회 때 남아 있는 메시지를 다시 읽는다.
                evidence_refs={e.id: list(e.source_message_ids) for e in retrieved if e.id in cited},
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
            attempt_log=attempt_log,
            model_version=model_version,
            prompt_version=self.prompt_version,
            retrieval_version=retriever.version,
            token_usage=usage,
            latency_ms=latency_ms,
            validator_version=VALIDATOR_VERSION,
            adjust=inp.adjust,
        )

    @staticmethod
    def _count_unmeasured(usage: dict[str, int], system: str, user: str) -> None:
        """응답 없이 끝난 호출(거절·장애·시간 초과)의 입력을 글자 수로 어림해 남긴다.

        이런 호출도 프롬프트는 보냈다(과금될 수 있다). 0으로 두면 실패하는 호출을 되풀이해도
        토큰 한도에 닿지 않는다. 측정값(`input_tokens`)과 섞지 않으려고
        `estimated_input_tokens`에 따로 쌓는다.
        """
        usage["estimated_input_tokens"] = usage.get("estimated_input_tokens", 0) + estimate_tokens(
            len(system) + len(user)
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
            log.warning("retrieval failed, continuing without history: %s", exception_kind(exc))
            return []
