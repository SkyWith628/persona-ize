"""첨삭 고정 워크플로 (project-design §5.3 / design.md §3).

의도·보존 항목 → 검색 → 프롬프트 → 생성 → 검증 → (실패 시 재생성 1회) → 추천 또는 원문 유지.
LangGraph `StateGraph`로 짠 상태 그래프다. 단계는 상태에서 필요한 값을 읽고 바뀐 키만 돌려준다.

    START → analyze → retrieve → compose → generate → validate → recommend → END
                                             │           └────────→ fallback  → END
                                             └────────────────────→ fallback

    generate 뒤: 응답 → validate / 거절 + 기회 남음 → generate / 장애 또는 기회 없음 → fallback
    validate 뒤: 통과 → recommend / 실패 + 기회 남음 → generate / 기회 없음 → fallback

갈림길은 `generate` 뒤와 `validate` 뒤 두 곳뿐이고 둘 다 상태만 보는 순수 함수다 — 모델이 다음
단계를 고르지 않는다. 기회는 `1 + MAX_REGENERATIONS`번. 모델 장애는 다시 부르지 않고 바로 원문을
유지한다(두 번째 시도에서 나면 앞선 출력이 남아 있어도 추천하지 않는다).

상태에는 초안·검색된 대화·모델 출력이 들어 있다. 그래서 체크포인터·저장소·캐시 없이 컴파일하고
(실행이 끝나면 상태는 사라진다) LangSmith 추적도 실행 구간에서 꺼 둔다 — 환경변수로 켜져 있어도
상태가 밖으로 나가지 않는다.

선택 기능 둘 (graph-redesign 5.5, 기본은 꺼짐 — 꺼져 있으면 위 설명 그대로다):
- `register_warnings`: 추천 결과에 초안에도 설정에도 없는 호칭이 있으면 경고 한 줄을 붙인다. 검증 결과와
  재생성에는 관여하지 않는다.
- `register_post_check`: 사실 검증을 통과한 출력의 반말/존댓말이 설정의 격식과 다르면 한 번 다시 쓴다.
  그래프 모양은 그대로다(validate → generate 길을 같이 쓴다). 통과한 1차는 상태에 보관해 두고, 2차를
  쓸 수 없으면(사실 검증 실패·거절·장애) fallback 단계가 원문 대신 1차를 경고와 함께 추천한다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import configure as configure_langsmith
from langsmith import tracing_context

from app.ai.intent import classify_intent
from app.ai.preserve import extract_preserved_items
from app.ai.prompt import PromptBundle, build_prompt, build_regenerate_suffix, fit_examples, today_kst
from app.ai.providers.base import LLMProvider
from app.ai.retrieval.base import Retriever
from app.ai.validation import address_leaks, validate_output
from app.core.errors import ModelRefusalError, ModelUnavailableError
from app.domain.contact import Contact
from app.domain.conversation import exception_kind
from app.domain.persona import PersonaSettings, apply_adjust
from app.domain.rewrite import (
    AttemptRecord,
    Condition,
    Intent,
    ModelOutput,
    PreservedItem,
    RetrievedExample,
    RewriteInput,
    RewriteResult,
    ValidationReport,
    Violation,
)
from app.domain.titles import VALIDATOR_VERSION
from app.domain.usage import estimate_tokens

log = logging.getLogger(__name__)

# 이 서비스는 LangSmith 추적을 쓰지 않는다 — 프롬프트·상태에 초안과 대화 원문이 들어 있다.
# 프로세스 전체에서 끄고(환경변수보다 우선한다) run()에서 실행 구간을 한 번 더 감싼다.
# 운영에서는 core/guard.py가 추적 변수 자체를 막는다.
configure_langsmith(enabled=False)

MAX_REGENERATIONS = 1

# 단계 이름. 그래프 구조 테스트(test_workflow.py)가 이 이름들을 고정한다.
ANALYZE = "analyze"
RETRIEVE = "retrieve"
COMPOSE = "compose"
GENERATE = "generate"
VALIDATE = "validate"
RECOMMEND = "recommend"
FALLBACK = "fallback"

# 말투 사후 점검의 재생성 꼬리말. 사실 검증 실패용 꼬리말(prompts/<version>/regenerate.md)은 "검증에
# 실패했다, 위반 항목을 고쳐라"라는 글이라 맞지 않는다 — 이 경우 사실은 맞고 말투만 다르다.
_REGISTER_KO = {True: "존댓말(해요체 또는 합쇼체)", False: "반말(해체)"}
_REGISTER_SUFFIX = (
    "이전 결과는 사실은 맞지만 말투가 설정과 다르다. 설정은 {wanted}인데 이전 결과는 {actual}이다.\n"
    "날짜·수량·사람·약속 같은 사실은 이전 결과 그대로 두고, 문장 끝의 말투만 {wanted}로 고쳐 다시 작성한다. "
    "새 사실이나 호칭을 더하지 않는다.\n\n"
    "[이전 결과]\n{previous_text}"
)
_WARN_REGISTER_HELD = (
    "말투를 맞춰 다시 쓴 결과를 쓸 수 없어 첫 결과를 추천합니다. "
    "말투(반말/존댓말)가 설정과 다르니 보내기 전에 확인하세요."
)
_WARN_REGISTER_OFF = "결과의 말투(반말/존댓말)가 설정한 격식과 다릅니다. 보내기 전에 확인하세요."
_WARN_ADDRESS_LEAK = "설정과 원문에 없는 호칭 {terms}이(가) 결과에 들어 있습니다. 보내기 전에 확인하세요."


class WorkflowState(TypedDict, total=False):
    """요청 하나의 상태. 실행마다 새로 만들어지고 어디에도 저장하지 않는다."""

    # 입력 (run이 넣는다)
    inp: RewriteInput
    contact: Contact
    persona: PersonaSettings | None  # 저장된 설정 그대로
    started: float  # perf_counter — latency_ms 계산용
    # analyze
    intent: Intent
    preserved: list[PreservedItem]
    persona_used: PersonaSettings | None  # 이번 요청에 실제로 쓴 설정 (A는 None, 조정 반영)
    # retrieve
    retriever: Retriever
    retrieved: list[RetrievedExample]
    # compose
    prompt: PromptBundle
    user_prompt: str  # 다음 생성에 보낼 사용자 프롬프트 (재생성 때 꼬리말이 붙는다)
    # generate / validate
    attempts: int
    outcome: Literal["output", "refusal", "unavailable"]  # 마지막 모델 호출의 결과
    output: ModelOutput | None
    model_version: str
    usage: dict[str, int]
    report: ValidationReport
    attempt_log: list[AttemptRecord]
    # 말투 사후 점검 (register_post_check가 켜져 있을 때만 채운다)
    register_retry: bool  # 방금 검증한 출력이 사실은 통과했고 말투만 달라 한 번 더 생성하러 간다
    held: ModelOutput | None  # 그때의 출력 (사실 검증 통과). 2차를 쓸 수 없으면 이것을 추천한다
    held_report: ValidationReport  # 보관한 출력의 검증 결과
    # recommend / fallback
    result: RewriteResult


def _attempts_left(state: WorkflowState) -> bool:
    return state["attempts"] < 1 + MAX_REGENERATIONS


def _after_generate(state: WorkflowState) -> str:
    """응답이 왔으면 검증, 거절이고 기회가 남았으면 재생성, 장애이거나 기회가 없으면 원문 유지."""
    if state["outcome"] == "output":
        return VALIDATE
    if state["outcome"] == "refusal" and _attempts_left(state):
        return GENERATE
    return FALLBACK


def _after_validate(state: WorkflowState) -> str:
    """통과면 추천, 실패이고 기회가 남았으면 재생성, 아니면 원문 유지.

    말투 사후 점검이 재생성을 정했으면(`register_retry`) 통과했어도 생성으로 돌아간다. 판단은 validate
    단계가 끝냈고 여기서는 상태만 본다. 점검이 꺼져 있으면 이 키는 상태에 없다.
    """
    if state.get("register_retry"):
        return GENERATE
    if state["report"].passed:
        return RECOMMEND
    if _attempts_left(state):
        return GENERATE
    return FALLBACK


def _register_mismatch(persona: PersonaSettings | None, text: str) -> bool | None:
    """출력의 반말/존댓말이 설정의 격식과 다르면 설정이 바라는 쪽(True=존댓말)을, 아니면 None.

    보는 것은 반말/존댓말 경계뿐이다. 합쇼체와 해요체는 가르지 않는다 — 자연스러운 공손한 한국어는
    둘을 섞어 쓴다. 설정이 없거나(A 조건 포함) 출력의 말투가 섞였거나 알 수 없으면 판단하지 않는다.
    기준은 이번 요청에 실제로 쓴 설정(조정 반영 뒤)의 격식이다.
    """
    if persona is None:
        return None
    # 점검이 꺼져 있으면 말투 분류기를 올리지 않는다 (규칙 계산이라 모델 호출은 없다).
    from app.ai.register import level_of_formality, message_label, polite

    wanted = polite(level_of_formality(persona.formality))
    actual = polite(message_label(text))
    if wanted is None or actual is None or wanted == actual:
        return None
    return wanted


@dataclass
class RewriteWorkflow:
    provider: LLMProvider
    retrievers: dict[Condition, Retriever]
    prompt_version: str
    # 둘 다 기본은 꺼짐이고, 꺼져 있으면 결과가 예전과 글자까지 같다 (모듈 머리말).
    register_warnings: bool = False
    register_post_check: bool = False
    # 컴파일된 그래프. 인스턴스마다 한 번 만들고 요청끼리 공유한다(요청 상태는 들어 있지 않다).
    # 단계는 실행 때 self.provider·self.retrievers를 읽으므로 만든 뒤에 바꿔 끼워도 반영된다.
    _graph: Any = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        g = StateGraph(WorkflowState)
        g.add_node(ANALYZE, self._analyze)
        g.add_node(RETRIEVE, self._retrieve)
        g.add_node(COMPOSE, self._compose)
        g.add_node(GENERATE, self._generate)
        g.add_node(VALIDATE, self._validate)
        g.add_node(RECOMMEND, self._recommend)
        g.add_node(FALLBACK, self._fallback)
        g.add_edge(START, ANALYZE)
        g.add_edge(ANALYZE, RETRIEVE)
        g.add_edge(RETRIEVE, COMPOSE)
        g.add_edge(COMPOSE, GENERATE)
        g.add_conditional_edges(GENERATE, _after_generate, [VALIDATE, GENERATE, FALLBACK])
        g.add_conditional_edges(VALIDATE, _after_validate, [RECOMMEND, GENERATE, FALLBACK])
        g.add_edge(RECOMMEND, END)
        g.add_edge(FALLBACK, END)
        # 체크포인터·저장소·캐시 없음: 상태(초안·대화 원문)를 어디에도 남기지 않는다.
        return g.compile(checkpointer=None, name="rewrite")

    async def run(
        self, inp: RewriteInput, contact: Contact, persona: PersonaSettings | None
    ) -> RewriteResult:
        started = time.perf_counter()
        if inp.condition not in self.retrievers:
            # auto는 RewriteService가 A~D로 바꿔서 넘긴다. 여기까지 오면 호출 쪽 실수다.
            raise ValueError(f"워크플로는 A~D 조건만 받습니다: {inp.condition}")
        state: WorkflowState = {"inp": inp, "contact": contact, "persona": persona, "started": started}
        with tracing_context(enabled=False):  # 환경변수로 추적이 켜져 있어도 상태를 내보내지 않는다
            final = await self._graph.ainvoke(state)
        return final["result"]

    # ── 단계 ──────────────────────────────────────────────────────────────

    async def _analyze(self, state: WorkflowState) -> WorkflowState:
        inp = state["inp"]
        return {
            "intent": classify_intent(inp.draft_text, inp.situation),
            "preserved": extract_preserved_items(inp.draft_text),
            # A 조건은 페르소나를 쓰지 않는다 (기준선). 조정(adjust)은 이번 요청의 사본에만 덮어쓴다.
            "persona_used": None if inp.condition == "A" else apply_adjust(state.get("persona"), inp.adjust),
        }

    async def _retrieve(self, state: WorkflowState) -> WorkflowState:
        inp = state["inp"]
        retriever = self.retrievers[inp.condition]
        # 검색 결과는 프롬프트 예산에 맞게 줄여서 쓴다 (예시 하나·전체 글자 상한).
        # 검증과 근거 참조도 줄인 목록 기준이다.
        retrieved = fit_examples(await self._retrieve_safely(retriever, inp))
        return {"retriever": retriever, "retrieved": retrieved}

    async def _compose(self, state: WorkflowState) -> WorkflowState:
        inp = state["inp"]
        prompt = build_prompt(
            version=self.prompt_version,
            contact=state["contact"],
            persona=state.get("persona_used"),
            intent=state["intent"],
            preserved=state["preserved"],
            draft_text=inp.draft_text,
            situation=inp.situation,
            retrieved=state["retrieved"],
            today=today_kst(inp.as_of),  # 검색 기준 시각과 같은 날을 '오늘'로 준다
            adjust=inp.adjust,
            contact_known=inp.condition != "A",  # 설정이 없어도(B~D) 누구에게 쓰는지는 밝힌다
            register_block=inp.register_block,  # v4부터 자리가 있다. 없으면(None) 본문이 그대로다
            voice_block=inp.voice_block,  # v5부터 자리가 있다
        )
        return {
            "prompt": prompt,
            "user_prompt": prompt.user,
            "attempts": 0,
            "usage": {},
            "model_version": "unknown",
            "output": None,
            "attempt_log": [],
        }

    async def _generate(self, state: WorkflowState) -> WorkflowState:
        prompt = state["prompt"]
        user_prompt = state["user_prompt"]
        attempts = state["attempts"] + 1
        usage = dict(state["usage"])  # 상태의 값을 고치지 않고 새 값을 돌려준다 (키 순서는 유지)
        try:
            gen = await self.provider.generate(system=prompt.system, user=user_prompt)
        except ModelRefusalError as exc:
            # 거절은 장애가 아니다. 위반처럼 기록하고 재생성 1회를 시도한다 (11-08 E003).
            log.info("model refusal attempt=%d", attempts)
            self._count_unmeasured(usage, prompt.system, user_prompt)
            report = ValidationReport(
                passed=False, violations=[Violation(check="refusal", detail=exc.message)]
            )
            record = AttemptRecord(attempt=attempts, passed=False, violations=report.violations)
            return {
                "attempts": attempts,
                "outcome": "refusal",
                "usage": usage,
                "report": report,
                "attempt_log": [*state["attempt_log"], record],
                "output": None,
                "user_prompt": (
                    prompt.user
                    + "\n\n"
                    + build_regenerate_suffix(self.prompt_version, "(출력 없음)", report.violations)
                ),
            }
        except ModelUnavailableError as exc:
            # 장애는 다시 부르지 않는다. 앞선 시도의 출력이 남아 있어도 추천하지 않는다.
            log.info("model unavailable attempt=%d: %s", attempts, exc.message)
            self._count_unmeasured(usage, prompt.system, user_prompt)
            report = ValidationReport(passed=False, violations=[Violation(check="empty", detail=exc.message)])
            record = AttemptRecord(attempt=attempts, passed=False, violations=report.violations)
            return {
                "attempts": attempts,
                "outcome": "unavailable",
                "usage": usage,
                "report": report,
                "attempt_log": [*state["attempt_log"], record],
            }
        for k, v in gen.usage.items():
            usage[k] = usage.get(k, 0) + v
        return {
            "attempts": attempts,
            "outcome": "output",
            "model_version": gen.model_version,
            "usage": usage,
            "output": gen.output,
        }

    async def _validate(self, state: WorkflowState) -> WorkflowState:
        output = state["output"]
        assert output is not None  # generate가 응답을 받았을 때만 온다
        report = validate_output(
            state["inp"].draft_text,
            output,
            state["preserved"],
            state.get("persona_used"),
            state["retrieved"],
        )
        record = AttemptRecord(
            attempt=state["attempts"],
            passed=report.passed,
            violations=report.violations,
            rewritten_text=output.rewritten_text,
        )
        update: WorkflowState = {"report": report, "attempt_log": [*state["attempt_log"], record]}
        if not report.passed:
            update["user_prompt"] = (
                state["prompt"].user
                + "\n\n"
                + build_regenerate_suffix(self.prompt_version, output.rewritten_text, report.violations)
            )
        if self.register_post_check:
            # 말투 때문에 다시 쓰는 것은 한 번뿐이다: 사실 검증을 통과했고, 아직 보관한 출력이 없고,
            # 기회가 남았을 때. 그 밖에는 표시를 내려 예전 갈림길 그대로 간다.
            wanted = None
            if report.passed and state.get("held") is None and _attempts_left(state):
                wanted = _register_mismatch(state.get("persona_used"), output.rewritten_text)
            update["register_retry"] = wanted is not None
            if wanted is not None:
                # 이 출력은 사실이 맞다. 2차가 더 나빠질 수 있으니 버리지 않고 들고 간다.
                update["held"] = output
                update["held_report"] = report
                update["user_prompt"] = (
                    state["prompt"].user
                    + "\n\n"
                    + _REGISTER_SUFFIX.format(
                        wanted=_REGISTER_KO[wanted],
                        actual=_REGISTER_KO[not wanted],
                        previous_text=output.rewritten_text,
                    )
                )
        return update

    async def _recommend(self, state: WorkflowState) -> WorkflowState:
        output = state["output"]
        assert output is not None  # validate를 통과했을 때만 온다
        return {"result": self._recommended(state, output, state["report"], held=False)}

    def _recommended(
        self, state: WorkflowState, output: ModelOutput, report: ValidationReport, *, held: bool
    ) -> RewriteResult:
        """추천 결과를 만든다. `held`는 보관해 둔 1차 출력을 추천하는 경우다 (말투 사후 점검)."""
        inp = state["inp"]
        latency_ms = int((time.perf_counter() - state["started"]) * 1000)
        cited = set(output.evidence_ids)
        return RewriteResult(
            status="recommended",
            result_text=output.rewritten_text.strip(),
            change_reasons=output.change_reasons,
            warnings=output.warnings + self._register_notes(state, output, held=held),
            evidence_ids=output.evidence_ids,
            intent=state["intent"],
            preserved_items=state["preserved"],
            validation=report,
            attempts=state["attempts"],
            attempt_log=state["attempt_log"],
            model_version=state["model_version"],
            prompt_version=self.prompt_version,
            retrieval_version=state["retriever"].version,
            token_usage=state["usage"],
            latency_ms=latency_ms,
            validator_version=VALIDATOR_VERSION,
            adjust=inp.adjust,
            # 근거의 출처 메시지 참조만 남긴다. 원문 사본은 두지 않는다 —
            # 근거 조회 때 남아 있는 메시지를 다시 읽는다.
            evidence_refs={e.id: list(e.source_message_ids) for e in state["retrieved"] if e.id in cited},
        )

    def _register_notes(self, state: WorkflowState, output: ModelOutput, *, held: bool) -> list[str]:
        """추천 결과에 붙일 말투·호칭 경고. 두 기능이 다 꺼져 있으면 빈 목록이다.

        경고일 뿐이다 — 검증 결과를 바꾸지 않고 재생성도 부르지 않는다.
        """
        notes: list[str] = []
        if self.register_post_check:
            if held:
                notes.append(_WARN_REGISTER_HELD)
            elif _register_mismatch(state.get("persona_used"), output.rewritten_text) is not None:
                # 다시 쓸 기회가 없었거나(1차가 사실 검증 실패) 다시 써도 말투가 그대로인 경우
                notes.append(_WARN_REGISTER_OFF)
        if self.register_warnings:
            leaks = address_leaks(
                state["inp"].draft_text,
                output.rewritten_text,
                state.get("persona_used"),
                state["contact"].alias,
            )
            if leaks:
                notes.append(_WARN_ADDRESS_LEAK.format(terms=", ".join(f"'{t}'" for t in leaks)))
        return notes

    async def _fallback(self, state: WorkflowState) -> WorkflowState:
        held = state.get("held")
        if held is not None:
            # 말투 사후 점검으로 다시 썼는데 2차를 쓸 수 없다(사실 검증 실패·거절·장애). 1차는 사실 검증을
            # 통과한 출력이므로 원문으로 돌아가지 않고 1차를 추천한다 — 점검을 켰다고 멀쩡한 결과를
            # 잃으면 안 된다. 시도 기록에는 2차의 실패가 그대로 남는다.
            return {"result": self._recommended(state, held, state["held_report"], held=True)}
        # 재생성까지 실패 또는 모델 장애: 원문 유지, 자동 전송 없음.
        inp = state["inp"]
        report = state["report"]
        latency_ms = int((time.perf_counter() - state["started"]) * 1000)
        result = RewriteResult(
            status="fallback_original",
            result_text=inp.draft_text,
            change_reasons=[],
            warnings=["추천을 만들지 못해 원문을 유지했습니다."] + [v.detail for v in report.violations],
            evidence_ids=[],
            intent=state["intent"],
            preserved_items=state["preserved"],
            validation=report,
            attempts=state["attempts"],
            attempt_log=state["attempt_log"],
            model_version=state["model_version"],
            prompt_version=self.prompt_version,
            retrieval_version=state["retriever"].version,
            token_usage=state["usage"],
            latency_ms=latency_ms,
            validator_version=VALIDATOR_VERSION,
            adjust=inp.adjust,
        )
        return {"result": result}

    # ── 도우미 ────────────────────────────────────────────────────────────

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
