"""첨삭 워크플로 대조: 같은 입력을 예전 구현과 지금 구현에 넣고 바깥 동작이 같은지 본다.

- 예전 구현: `tests/legacy_workflow.py`의 `LegacyRewriteWorkflow` (2026-11-14 LangGraph로 옮기기 전의 사본)
- 지금 구현: `app.ai.workflow.RewriteWorkflow`

견주는 것: `RewriteResult` 전체(`latency_ms`만 뺀다 — 잰 시간이라 매번 다르다), 올라온 예외의 종류와 문구,
모델에 보낸 `system`·`user` 프롬프트와 횟수, 검색기를 부른 인자와 횟수, 입력을 고쳤는지,
워크플로가 남긴 로그 줄(수준·문구).
모델은 부르지 않는다. 정해 둔 응답을 순서대로 내는 가짜 제공자를 두 구현에 따로 준다.
문장은 전부 지어낸 것이다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.ai.providers.base import GenerationResult
from app.ai.workflow import RewriteWorkflow
from app.core.errors import DomainError, ModelRefusalError, ModelUnavailableError
from app.domain.contact import Contact
from app.domain.persona import PersonaSettings
from app.domain.rewrite import (
    ModelOutput,
    RetrievedExample,
    RewriteInput,
    RewriteResult,
    ValidationReport,
)
from tests import legacy_workflow
from tests.legacy_workflow import LegacyRewriteWorkflow

# ── 고정 입력 ────────────────────────────────────────────────────────────────

AS_OF = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)  # KST 11/10 12:00 — v3 프롬프트의 [오늘]이 고정된다
CREATED = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)

SUPERIOR = Contact(
    id=UUID("00000000-0000-4000-8000-000000000001"),
    user_id="u-parity",
    alias="박 팀장",
    relationship_type="superior",
    created_at=CREATED,
)
FRIEND = Contact(
    id=UUID("00000000-0000-4000-8000-000000000002"),
    user_id="u-parity",
    alias="준호",
    relationship_type="friend",
    created_at=CREATED,
)
PARTNER_FIRM = Contact(
    id=UUID("00000000-0000-4000-8000-000000000003"),
    user_id="u-parity",
    alias="거래처 담당",
    relationship_type="acquaintance",
    created_at=CREATED,
)

# 격식을 4로 둔다 — more_formal(5)·more_casual(3) 조정이 둘 다 프롬프트에 드러나게.
PERSONA = PersonaSettings(
    formality=4,
    friendliness=2,
    directness=3,
    address_term="팀장님",
    preferred_phrases=["확인 부탁드립니다"],
    forbidden_phrases=["ㅋㅋ", "ㅠ"],
    notes="결론부터 쓴다",
)
PERSONA_FRIEND = PersonaSettings(formality=1, friendliness=5, directness=5, emoji="rare")
PERSONA_FRIEND_HYUNG = PERSONA_FRIEND.model_copy(update={"address_term": "형"})

DRAFT = "회의 자료 3건은 오늘 정리해서 내일 오전 10시까지 보내드리겠습니다."
GOOD_TEXT = "팀장님, 회의 자료 3건은 오늘 정리해서 내일 오전 10시까지 보내드리겠습니다."
DRAFT_FRIEND = "주말에는 못 갈 것 같아. 다음 주 토요일 저녁 7시는 어때?"
DRAFT_PERSON = "김민수 대리님께 견적서 2부를 금요일까지 전달하겠습니다."


def out(text: str, *, evidence: list[str] | None = None, **extra: Any) -> ModelOutput:
    return ModelOutput(rewritten_text=text, evidence_ids=evidence or [], **extra)


GOOD = out(GOOD_TEXT, change_reasons=["호칭 추가", "격식 올림"])
# 아래는 일부러 검증에 걸리게 만든 출력. 어떤 검사에 걸리는지는 시나리오 표의 `checks`가 단언한다.
MISSING_DATE = out("팀장님, 회의 자료 3건은 오늘 정리해서 보내드리겠습니다.")  # '내일 오전 10시' 빠짐
MISSING_NUMBER = out("팀장님, 회의 자료는 오늘 정리해서 내일 오전 10시까지 보내드리겠습니다.")  # '3건' 빠짐
CHANGED_NUMBER = out("팀장님, 회의 자료 5건은 오늘 정리해서 내일 오전 10시까지 보내드리겠습니다.")
ADDED_DATE = out(GOOD_TEXT + " 금요일에 다시 말씀드리겠습니다.")  # 원문에 없는 날짜
FORBIDDEN = out(GOOD_TEXT + " ㅋㅋ")  # 금지 표현
NEGATION_ADDED = out(
    "팀장님, 회의 자료 3건은 오늘 정리해서 내일 오전 10시까지 보내드리겠습니다만 조금 어렵습니다."
)
PROMISE_DROPPED = out("팀장님, 회의 자료 3건은 오늘 정리해서 내일 오전 10시까지 보냅니다.")
EMPTY = out("  \n ")
UNKNOWN_EVIDENCE = out(GOOD_TEXT, evidence=["ep-없는-근거"])
MANY_VIOLATIONS = out("팀장님, 회의 자료 5건은 오늘 정리해서 보냅니다 ㅋㅋ", evidence=["ep-없는-근거"])
PADDED = out("\n  " + GOOD_TEXT + "  \n")
WITH_WARNINGS = out(
    GOOD_TEXT,
    change_reasons=["호칭 추가", "문장 끝을 격식체로"],
    warnings=["호칭은 설정값을 썼습니다.", "시간 표현은 그대로 두었습니다."],
)

GOOD_FRIEND = out("주말에는 못 갈 것 같아 ㅠㅠ 다음 주 토요일 저녁 7시는 어때?")
KINSHIP = out("형, 주말에는 못 갈 것 같아. 다음 주 토요일 저녁 7시는 어때?")  # 설정에 없는 친족 호칭
NEGATION_DROPPED = out("주말 말고 다음 주 토요일 저녁 7시는 어때?")  # '못 갈'이 사라짐

GOOD_PERSON = out("김민수 대리님께 견적서 2부를 금요일까지 전달하겠습니다. 확인 부탁드립니다.")
PERSON_CHANGED = out("박지현 대리님께 견적서 2부를 금요일까지 전달하겠습니다.")

REFUSAL = ModelRefusalError("모델이 생성을 거절했습니다 (합성)")
UNAVAILABLE = ModelUnavailableError("모델 응답 없음 429 (합성)")
CRASH = RuntimeError("제공자 내부 오류 (합성)")


def example(
    id_: str, text: str, *, purpose: str = "style", refs: tuple[str, ...] = (), day: int | None = 3
) -> RetrievedExample:
    return RetrievedExample(
        id=id_,
        purpose=purpose,  # type: ignore[arg-type]
        text=text,
        speaker_role="me" if purpose == "style" else "graph",
        sent_at=datetime(2026, 11, day, 23, 30, tzinfo=UTC) if day else None,  # KST로는 다음 날
        source_message_ids=list(refs),
        score=0.5,
    )


STYLE = (
    example("ep-1", "팀장님, 주간 보고 정리해서 올려두었습니다.", refs=("c1:m3", "c1:m4"), day=3),
    example("ep-2", "넵 확인했습니다. 수정본은 오후에 드리겠습니다.", refs=("c1:m9",), day=5),
    example("ep-3", "팀장님 회의실은 예약해 두었습니다.", refs=(), day=None),
)
CONTEXT = (example("g-1", "박 팀장은 화요일마다 주간 회의를 연다.", purpose="context", refs=("c1:m12",)),)


def long_text(tag: str, size: int) -> str:
    return (f"[{tag}] " + "이번 주 보고는 금요일에 정리해서 드리겠습니다. " * 40)[:size]


# 예산(예시 하나 600자, 문체 합계 2,400자, 맥락 합계 1,200자)을 넘는 검색 결과.
# 남는 것: big-1~3(잘려서)·short-6, ctx-1~2. 빠지는 것: big-4·big-5·ctx-3.
OVER_BUDGET = (
    *(example(f"big-{i}", long_text(f"긴 묶음 {i}", 700), refs=(f"c9:m{i}",)) for i in range(1, 6)),
    example("short-6", "네, 바로 확인하겠습니다.", refs=("c9:m60", "c9:m61")),
    *(
        example(f"ctx-{i}", long_text(f"맥락 {i}", 500), purpose="context", refs=(f"c9:g{i}",))
        for i in range(1, 4)
    ),
)
# 딱 예산만큼: 600자 넷(합계 2,400자)은 그대로 들어가고 한 글자짜리 다섯째가 빠진다.
AT_BUDGET = (
    *(example(f"fit-{i}", long_text(f"꽉 찬 묶음 {i}", 600), refs=(f"c8:m{i}",)) for i in range(1, 5)),
    example("fit-5", "넵", refs=("c8:m5",)),
)


def checks(*names: str) -> frozenset[str]:
    return frozenset(names)


OK = checks()


@dataclass(frozen=True)
class Scenario:
    id: str
    about: str
    script: tuple[ModelOutput | Exception, ...]
    # 기대하는 길. 예외가 올라오는 시나리오는 `raises`만 채운다.
    status: str | None = None
    attempts: int | None = None
    checks: tuple[frozenset[str], ...] = ()  # 시도마다 걸려야 하는 검사 이름 (정확히 이 집합)
    raises: type[Exception] | None = None
    condition: str = "B"
    draft: str = DRAFT
    situation: str = "업무 보고"
    contact: Contact = field(default_factory=lambda: SUPERIOR)
    persona: PersonaSettings | None = field(default_factory=lambda: PERSONA)
    adjust: str | None = None
    examples: tuple[RetrievedExample, ...] = ()  # 이 조건의 검색기가 돌려주는 것
    retriever_error: Exception | None = None
    retriever_conditions: tuple[str, ...] = ("A", "B", "C", "D")
    prompt_version: str = "rewrite-v3"
    as_of: datetime | None = AS_OF


REC, FALLBACK = "recommended", "fallback_original"

SCENARIOS: tuple[Scenario, ...] = (
    # ── 기본 길 ──
    Scenario("pass-first", "첫 시도 통과", (GOOD,), REC, 1, (OK,)),
    Scenario(
        "fail-then-pass",
        "첫 시도 검증 실패 → 재생성 통과",
        (MISSING_DATE, GOOD),
        REC,
        2,
        (checks("preserve_missing"), OK),
    ),
    Scenario(
        "fail-twice",
        "두 번 다 검증 실패 → 원문 유지",
        (MISSING_DATE, MISSING_NUMBER),
        FALLBACK,
        2,
        (checks("preserve_missing"), checks("preserve_missing")),
    ),
    # ── 모델 거절 ──
    Scenario("refusal-then-pass", "거절 → 재생성 통과", (REFUSAL, GOOD), REC, 2, (checks("refusal"), OK)),
    Scenario(
        "refusal-twice",
        "거절 두 번 → 원문 유지",
        (REFUSAL, REFUSAL),
        FALLBACK,
        2,
        (checks("refusal"), checks("refusal")),
    ),
    Scenario(
        "refusal-then-invalid",
        "거절 뒤 검증 실패 → 원문 유지",
        (REFUSAL, ADDED_DATE),
        FALLBACK,
        2,
        (checks("refusal"), checks("fact_added")),
    ),
    Scenario(
        "invalid-then-refusal",
        "검증 실패 뒤 거절 → 원문 유지",
        (MISSING_DATE, REFUSAL),
        FALLBACK,
        2,
        (checks("preserve_missing"), checks("refusal")),
    ),
    # ── 모델 장애 ──
    Scenario(
        "unavailable-first",
        "첫 시도에 장애 → 재시도 없이 원문 유지",
        (UNAVAILABLE,),
        FALLBACK,
        1,
        (checks("empty"),),
    ),
    Scenario(
        "invalid-then-unavailable",
        "검증 실패 뒤 두 번째 시도에 장애 — 앞선 출력이 있어도 원문 유지",
        (MISSING_DATE, UNAVAILABLE),
        FALLBACK,
        2,
        (checks("preserve_missing"), checks("empty")),
    ),
    Scenario(
        "refusal-then-unavailable",
        "거절 뒤 장애 → 원문 유지",
        (REFUSAL, UNAVAILABLE),
        FALLBACK,
        2,
        (checks("refusal"), checks("empty")),
    ),
    Scenario(
        "provider-crash-first", "제공자가 예상 밖 예외 → 그대로 올라온다", (CRASH,), raises=RuntimeError
    ),
    Scenario(
        "provider-crash-second",
        "검증 실패 뒤 예상 밖 예외 → 그대로 올라온다",
        (MISSING_DATE, CRASH),
        raises=RuntimeError,
    ),
    # ── 조건 A~D ──
    Scenario(
        "cond-A-persona-unused",
        "A는 페르소나를 쓰지 않는다 — 금지 표현이 있어도 통과",
        (FORBIDDEN,),
        REC,
        1,
        (OK,),
        condition="A",
    ),
    Scenario(
        "cond-B-forbidden",
        "B는 페르소나의 금지 표현에 걸린다 → 재생성 통과",
        (FORBIDDEN, GOOD),
        REC,
        2,
        (checks("forbidden_phrase"), OK),
    ),
    Scenario(
        "cond-C-evidence",
        "C: 문체 예시를 인용 → evidence_refs",
        (out(GOOD_TEXT, evidence=["ep-2", "ep-1"]),),
        REC,
        1,
        (OK,),
        condition="C",
        examples=STYLE,
    ),
    Scenario(
        "cond-C-no-citation",
        "C: 예시는 있지만 인용 없음",
        (GOOD,),
        REC,
        1,
        (OK,),
        condition="C",
        examples=STYLE,
    ),
    Scenario(
        "cond-D-evidence",
        "D: 문체 + 그래프 맥락, 같은 id를 두 번 인용, 출처가 빈 예시",
        (out(GOOD_TEXT, evidence=["g-1", "ep-3", "ep-3"]),),
        REC,
        1,
        (OK,),
        condition="D",
        examples=STYLE + CONTEXT,
    ),
    Scenario(
        "cond-D-fallback-with-evidence",
        "D: 근거를 인용했지만 두 번 다 실패 → 원문 유지(근거 비움)",
        (
            out(MISSING_DATE.rewritten_text, evidence=["ep-1"]),
            out(ADDED_DATE.rewritten_text, evidence=["g-1"]),
        ),
        FALLBACK,
        2,
        (checks("preserve_missing"), checks("fact_added")),
        condition="D",
        examples=STYLE + CONTEXT,
    ),
    Scenario(
        "cond-B-retriever-returns",
        "실제 구성 밖: B의 검색기가 예시를 돌려주면 그대로 쓴다",
        (out(GOOD_TEXT, evidence=["ep-1"]),),
        REC,
        1,
        (OK,),
        examples=STYLE,
    ),
    # ── 이번 요청만의 조정 ──
    Scenario("adjust-shorter", "조정 shorter", (GOOD,), REC, 1, (OK,), adjust="shorter"),
    Scenario("adjust-longer", "조정 longer", (GOOD,), REC, 1, (OK,), adjust="longer"),
    Scenario("adjust-more-formal", "조정 more_formal", (GOOD,), REC, 1, (OK,), adjust="more_formal"),
    Scenario("adjust-more-casual", "조정 more_casual", (GOOD,), REC, 1, (OK,), adjust="more_casual"),
    Scenario(
        "adjust-cond-A",
        "A + 조정(페르소나 없이 한 줄만)",
        (GOOD,),
        REC,
        1,
        (OK,),
        condition="A",
        adjust="shorter",
    ),
    Scenario(
        "adjust-no-persona",
        "페르소나 None + 조정",
        (GOOD,),
        REC,
        1,
        (OK,),
        condition="C",
        persona=None,
        adjust="more_formal",
        examples=STYLE,
    ),
    Scenario(
        "adjust-fallback",
        "조정이 있는 요청이 원문 유지로 끝남",
        (EMPTY, EMPTY),
        FALLBACK,
        2,
        (checks("empty"), checks("empty")),
        adjust="more_casual",
    ),
    # ── 페르소나 None ──
    Scenario("persona-none-A", "페르소나 None, A", (GOOD,), REC, 1, (OK,), condition="A", persona=None),
    Scenario("persona-none-B", "페르소나 None, B", (GOOD,), REC, 1, (OK,), persona=None),
    Scenario(
        "persona-none-C-person",
        "페르소나 None, C: 사람 이름이 바뀐 출력 → 재생성 통과",
        (PERSON_CHANGED, GOOD_PERSON),
        REC,
        2,
        (checks("preserve_missing", "fact_added"), OK),
        condition="C",
        draft=DRAFT_PERSON,
        situation="견적 전달",
        contact=PARTNER_FIRM,
        persona=None,
        examples=STYLE,
    ),
    # ── 검색 ──
    Scenario(
        "retriever-raises",
        "검색기 예외 → 이력 없이 계속",
        (GOOD,),
        REC,
        1,
        (OK,),
        condition="C",
        examples=STYLE,
        retriever_error=RuntimeError("검색 장애 (합성)"),
    ),
    Scenario(
        "retriever-raises-domain-error",
        "검색기가 모델 장애와 같은 종류의 예외를 내도 축소 동작",
        (GOOD,),
        REC,
        1,
        (OK,),
        condition="D",
        examples=STYLE,
        retriever_error=ModelUnavailableError("검색 쪽 장애 (합성)"),
    ),
    Scenario(
        "retriever-raises-cited",
        "검색기 예외인데 출력이 근거를 인용 → 두 번 실패, 원문 유지",
        (out(GOOD_TEXT, evidence=["ep-1"]), out(GOOD_TEXT, evidence=["ep-1"])),
        FALLBACK,
        2,
        (checks("evidence_unknown"), checks("evidence_unknown")),
        condition="D",
        examples=STYLE,
        retriever_error=RuntimeError("검색 장애 (합성)"),
    ),
    Scenario(
        "over-budget",
        "검색 결과가 예산보다 많다: 빠진 예시를 인용하면 실패 → 남은 예시를 인용해 통과",
        (
            out(GOOD_TEXT, evidence=["big-4", "ctx-3"]),
            out(GOOD_TEXT, evidence=["short-6", "big-1", "ctx-2"]),
        ),
        REC,
        2,
        (checks("evidence_unknown"), OK),
        condition="D",
        examples=OVER_BUDGET,
    ),
    Scenario(
        "at-budget",
        "딱 예산만큼인 검색 결과",
        (out(GOOD_TEXT, evidence=["fit-4", "fit-1"]),),
        REC,
        1,
        (OK,),
        condition="C",
        examples=AT_BUDGET,
    ),
    # ── 검증 위반 종류 ──
    Scenario(
        "number-missing",
        "수량이 빠진 출력 → 재생성 통과",
        (MISSING_NUMBER, GOOD),
        REC,
        2,
        (checks("preserve_missing"), OK),
    ),
    Scenario(
        "number-changed",
        "수량이 바뀐 출력 두 번 → 원문 유지",
        (CHANGED_NUMBER, CHANGED_NUMBER),
        FALLBACK,
        2,
        (checks("preserve_missing", "fact_added"),) * 2,
    ),
    Scenario("date-added", "없던 날짜가 생긴 출력", (ADDED_DATE, GOOD), REC, 2, (checks("fact_added"), OK)),
    Scenario(
        "negation-added",
        "없던 '어렵다'가 생긴 출력",
        (NEGATION_ADDED, GOOD),
        REC,
        2,
        (checks("negation_changed"), OK),
    ),
    Scenario(
        "promise-dropped",
        "약속 표현이 사라진 출력",
        (PROMISE_DROPPED, GOOD),
        REC,
        2,
        (checks("preserve_missing"), OK),
    ),
    Scenario("empty-output", "빈 출력 → 재생성 통과", (EMPTY, GOOD), REC, 2, (checks("empty"), OK)),
    Scenario(
        "unknown-evidence",
        "검색되지 않은 근거 id",
        (UNKNOWN_EVIDENCE, GOOD),
        REC,
        2,
        (checks("evidence_unknown"), OK),
    ),
    Scenario(
        "many-violations",
        "위반 여러 종류가 한 출력에 — 위반 순서까지 같은지",
        (MANY_VIOLATIONS, MANY_VIOLATIONS),
        FALLBACK,
        2,
        (checks("preserve_missing", "fact_added", "forbidden_phrase", "evidence_unknown"),) * 2,
    ),
    Scenario(
        "kinship-added",
        "설정에 없는 친족 호칭 → 재생성 통과",
        (KINSHIP, GOOD_FRIEND),
        REC,
        2,
        (checks("fact_added"), OK),
        draft=DRAFT_FRIEND,
        situation="약속 미루기",
        contact=FRIEND,
        persona=PERSONA_FRIEND,
    ),
    Scenario(
        "kinship-allowed",
        "같은 출력이 호칭 설정이 '형'이면 통과",
        (KINSHIP,),
        REC,
        1,
        (OK,),
        draft=DRAFT_FRIEND,
        situation="약속 미루기",
        contact=FRIEND,
        persona=PERSONA_FRIEND_HYUNG,
    ),
    Scenario(
        "negation-dropped",
        "'못 간다'가 사라진 출력 두 번 → 원문 유지",
        (NEGATION_DROPPED, NEGATION_DROPPED),
        FALLBACK,
        2,
        (checks("negation_changed"),) * 2,
        draft=DRAFT_FRIEND,
        situation="약속 미루기",
        contact=FRIEND,
        persona=PERSONA_FRIEND,
    ),
    # ── 그 밖의 출력·입력 모양 ──
    Scenario(
        "padded-output", "앞뒤 공백이 있는 출력 — 결과는 다듬고 기록은 그대로", (PADDED,), REC, 1, (OK,)
    ),
    Scenario("model-warnings", "모델이 준 주의점·바꾼 이유 순서", (WITH_WARNINGS,), REC, 1, (OK,)),
    Scenario("empty-situation", "상황 설명 없음", (GOOD,), REC, 1, (OK,), situation=""),
    Scenario(
        "prompt-v1-no-as-of",
        "프롬프트 v1, as_of 없음(v1에는 [오늘]이 없어 결과가 날짜에 매이지 않는다)",
        (MISSING_DATE, GOOD),
        REC,
        2,
        (checks("preserve_missing"), OK),
        prompt_version="rewrite-v1",
        as_of=None,
    ),
    Scenario(
        "prompt-v2",
        "프롬프트 v2의 재생성 문구",
        (REFUSAL, GOOD),
        REC,
        2,
        (checks("refusal"), OK),
        condition="C",
        examples=STYLE,
        prompt_version="rewrite-v2",
    ),
    Scenario(
        "as-of-kst-midnight",
        "as_of가 KST 자정을 막 넘긴 시각",
        (GOOD,),
        REC,
        1,
        (OK,),
        as_of=datetime(2026, 10, 20, 15, 0, tzinfo=UTC),
    ),
    # ── 예외 ──
    Scenario(
        "condition-missing",
        "조건이 retrievers에 없다",
        (GOOD,),
        raises=ValueError,
        condition="C",
        retriever_conditions=("A", "B"),
    ),
    Scenario("condition-auto", "auto가 워크플로까지 왔다", (GOOD,), raises=ValueError, condition="auto"),
    Scenario(
        "prompt-version-missing",
        "없는 프롬프트 버전",
        (GOOD,),
        raises=FileNotFoundError,
        condition="C",
        examples=STYLE,
        prompt_version="rewrite-없는-버전",
    ),
)

BY_ID = {s.id: s for s in SCENARIOS}
PARAMS = [pytest.param(s, id=s.id) for s in SCENARIOS]


# ── 가짜 제공자·검색기 ───────────────────────────────────────────────────────


class ScriptExhaustedError(Exception):
    """정해 둔 응답보다 더 많이 불렀다 — 구현이 호출을 더 한 것이다."""


def fresh(item: ModelOutput | Exception) -> ModelOutput | Exception:
    """두 구현이 같은 객체를 나눠 쓰지 않게 사본을 만든다."""
    if isinstance(item, ModelOutput):
        return item.model_copy(deep=True)
    if isinstance(item, DomainError):
        return type(item)(item.message)
    return type(item)(*item.args)


def reply(item: ModelOutput | Exception, call_no: int, system: str, user: str) -> GenerationResult:
    """호출마다 다른 모델 버전·토큰 수를 낸다 — 어느 호출의 값이 결과에 남는지, 합산이 같은지가 드러난다."""
    if isinstance(item, Exception):
        raise item
    usage = {
        "input_tokens": (len(system) + len(user)) // 2,
        "output_tokens": len(item.rewritten_text) + call_no,
    }
    if call_no > 1:
        usage["cache_read_input_tokens"] = 7 * call_no
    return GenerationResult(output=item, model_version=f"scripted-{call_no}", usage=usage)


class RecordingProvider:
    """정해 둔 응답을 순서대로 내고, 받은 프롬프트를 적어 둔다."""

    def __init__(self, script: tuple[ModelOutput | Exception, ...]) -> None:
        self._script = [fresh(item) for item in script]
        self.calls: list[tuple[str, str]] = []

    async def generate(self, *, system: str, user: str) -> GenerationResult:
        self.calls.append((system, user))
        call_no = len(self.calls)
        if call_no > len(self._script):
            raise ScriptExhaustedError(f"{call_no}번째 호출 — 정해 둔 응답은 {len(self._script)}개")
        return reply(self._script[call_no - 1], call_no, system, user)


class StubRetriever:
    def __init__(
        self, version: str, examples: tuple[RetrievedExample, ...] = (), error: Exception | None = None
    ) -> None:
        self.version = version
        self._examples = examples
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def retrieve(
        self,
        *,
        user_id: str,
        contact_id: UUID,
        draft_text: str,
        situation: str,
        top_k: int = 4,
        as_of: datetime | None = None,
    ) -> list[RetrievedExample]:
        self.calls.append(
            {
                "user_id": user_id,
                "contact_id": str(contact_id),
                "draft_text": draft_text,
                "situation": situation,
                "top_k": top_k,
                "as_of": as_of,
            }
        )
        await asyncio.sleep(0)
        if self._error is not None:
            raise self._error
        return [e.model_copy(deep=True) for e in self._examples]


# ── 실행·비교 ────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    result: dict[str, Any] | None = None
    result_type: str | None = None
    latency_ms: Any = None
    error: tuple[str, str] | None = None
    provider_calls: list[tuple[str, str]] = field(default_factory=list)
    retriever_calls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    inputs_changed: list[str] = field(default_factory=list)
    logs: list[tuple[str, str]] = field(default_factory=list)


class WorkflowLogs(logging.Handler):
    """워크플로가 남긴 로그 줄(수준, 문구). 로거 이름은 두 구현이 다를 수밖에 없어 보지 않는다."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append((record.levelname, record.getMessage()))


@contextmanager
def capture_logs() -> Iterator[WorkflowLogs]:
    """두 구현의 로거(`app.ai.*`, 얼려 둔 사본)를 INFO까지 열고 줄을 모은다. 끝나면 원래 수준으로."""
    handler = WorkflowLogs()
    loggers = [logging.getLogger(name) for name in ("app.ai", "tests.legacy_workflow")]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for lg, level in zip(loggers, levels, strict=True):
            lg.removeHandler(handler)
            lg.setLevel(level)


def make_input(scn: Scenario) -> RewriteInput:
    return RewriteInput(
        user_id="u-parity",
        contact_id=scn.contact.id,
        draft_text=scn.draft,
        draft_version=3,
        situation=scn.situation,
        condition=scn.condition,  # type: ignore[arg-type]
        persona_version=2,
        as_of=scn.as_of,
        adjust=scn.adjust,  # type: ignore[arg-type]
    )


def make_retrievers(scn: Scenario) -> dict[str, StubRetriever]:
    retrievers = {}
    for cond in scn.retriever_conditions:
        mine = cond == scn.condition
        error = fresh(scn.retriever_error) if mine and scn.retriever_error else None
        retrievers[cond] = StubRetriever(f"stub-{cond}-v1", scn.examples if mine else (), error)
    return retrievers


def dump_result(result: Any, outcome: Outcome) -> None:
    outcome.result_type = type(result).__name__
    if isinstance(result, RewriteResult):
        outcome.result = result.model_dump(mode="json")
        outcome.latency_ms = outcome.result.pop("latency_ms")


async def run_one(workflow_cls: Any, scn: Scenario) -> Outcome:
    """시나리오 하나를 구현 하나에 돌린다. 제공자·검색기·입력은 매번 새로 만든다."""
    provider = RecordingProvider(scn.script)
    retrievers = make_retrievers(scn)
    inp = make_input(scn)
    contact = scn.contact.model_copy(deep=True)
    persona = scn.persona.model_copy(deep=True) if scn.persona else None
    before = (inp.model_dump(), contact.model_dump(), persona.model_dump() if persona else None)

    outcome = Outcome()
    workflow = workflow_cls(provider=provider, retrievers=retrievers, prompt_version=scn.prompt_version)
    with capture_logs() as logs:
        try:
            dump_result(await workflow.run(inp, contact, persona), outcome)
        except Exception as exc:  # noqa: BLE001 - 어떤 예외가 올라오는지가 비교 대상이다
            outcome.error = (type(exc).__name__, str(exc))

    after = (inp.model_dump(), contact.model_dump(), persona.model_dump() if persona else None)
    names = ("inp", "contact", "persona")
    outcome.inputs_changed = [n for n, b, a in zip(names, before, after, strict=True) if a != b]
    outcome.logs = logs.lines
    outcome.provider_calls = provider.calls
    outcome.retriever_calls = {cond: r.calls for cond, r in retrievers.items()}
    return outcome


# 값이 같아도 키 순서가 달라지면 따로 알린다 (저장·응답의 JSON 모양이 달라진다).
ORDERED_DICT_FIELDS = ("token_usage", "evidence_refs")


def first_gap(a: str, b: str) -> str:
    """두 문자열이 처음 갈라지는 자리와 그 둘레."""
    i = next((k for k, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
    around = slice(max(i - 30, 0), i + 40)
    return f"{i}번째 글자부터 다름 (길이 {len(a)} / {len(b)}): {a[around]!r} / {b[around]!r}"


def differences(old: Outcome, new: Outcome, old_name: str = "예전", new_name: str = "지금") -> list[str]:
    """두 실행의 바깥 동작 차이를 사람이 읽을 줄로 돌려준다. 같으면 빈 목록."""
    found: list[str] = []

    def note(what: str, a: Any, b: Any) -> None:
        found.append(f"{what}: {old_name}={a!r} / {new_name}={b!r}")

    if old.error != new.error:
        note("예외", old.error, new.error)
    if old.result_type != new.result_type:
        note("돌려준 값의 종류", old.result_type, new.result_type)
    if old.result is not None and new.result is not None:
        for key in sorted(old.result.keys() | new.result.keys()):
            a, b = old.result.get(key), new.result.get(key)
            if a != b:
                note(f"결과 {key}", a, b)
            elif key in ORDERED_DICT_FIELDS and list(a) != list(b):
                note(f"결과 {key} 키 순서", list(a), list(b))

    if len(old.provider_calls) != len(new.provider_calls):
        note("모델 호출 횟수", len(old.provider_calls), len(new.provider_calls))
    for n, (a, b) in enumerate(zip(old.provider_calls, new.provider_calls, strict=False), start=1):
        for part, x, y in (("system", a[0], b[0]), ("user", a[1], b[1])):
            if x != y:
                found.append(f"{n}번째 모델 호출의 {part} 프롬프트: {first_gap(x, y)}")

    if old.retriever_calls != new.retriever_calls:
        note("검색기 호출", old.retriever_calls, new.retriever_calls)
    if old.inputs_changed != new.inputs_changed:
        note("워크플로가 고친 입력", old.inputs_changed, new.inputs_changed)
    if old.logs != new.logs:
        note("로그 줄", old.logs, new.logs)
    return found


# ── 테스트 ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("scn", PARAMS)
async def test_scenario_takes_intended_path(scn: Scenario) -> None:
    """시나리오가 뜻한 길을 실제로 지나는지 (예전 구현 기준). 일부러 만든 검증 실패가 정말 실패하는지."""
    got = await run_one(LegacyRewriteWorkflow, scn)
    assert got.inputs_changed == []
    if scn.raises is not None:
        assert got.error is not None, got.result
        assert got.error[0] == scn.raises.__name__
        return
    assert got.error is None, got.error
    assert got.result is not None
    assert (got.result["status"], got.result["attempts"]) == (scn.status, scn.attempts)
    seen = tuple(frozenset(v["check"] for v in a["violations"]) for a in got.result["attempt_log"])
    assert seen == scn.checks
    assert len(got.provider_calls) == scn.attempts  # 시도 한 번에 모델 호출 한 번
    assert got.result["validation"]["passed"] is (scn.status == REC)
    if scn.status == FALLBACK:
        assert got.result["result_text"] == scn.draft  # 원문 유지


@pytest.mark.anyio
@pytest.mark.parametrize("scn", PARAMS)
async def test_current_workflow_matches_legacy(scn: Scenario) -> None:
    """같은 입력 → 같은 결과·같은 예외·같은 프롬프트(횟수까지)·같은 검색 호출."""
    old = await run_one(LegacyRewriteWorkflow, scn)
    new = await run_one(RewriteWorkflow, scn)
    found = differences(old, new)
    assert not found, f"[{scn.id}] {scn.about}\n" + "\n".join(found)
    if new.result is not None:  # latency_ms는 값만 빼고 모양은 본다
        assert isinstance(new.latency_ms, int) and new.latency_ms >= 0


@pytest.mark.anyio
async def test_scenarios_cover_distinct_paths() -> None:
    """시나리오 표가 실제로 서로 다른 길을 지난다 — 한 길만 여러 번 도는 표가 아니다."""
    assert len(BY_ID) == len(SCENARIOS)  # id 중복 없음
    paths: set[tuple[str, int]] = set()
    errors: set[str] = set()
    seen_checks: set[str] = set()
    with_refs = with_estimate = with_warning_log = with_info_log = 0
    for scn in SCENARIOS:
        got = await run_one(LegacyRewriteWorkflow, scn)
        if got.error is not None:
            errors.add(got.error[0])
            continue
        assert got.result is not None
        paths.add((got.result["status"], got.result["attempts"]))
        seen_checks |= {v["check"] for a in got.result["attempt_log"] for v in a["violations"]}
        with_refs += bool(got.result["evidence_refs"])
        with_estimate += "estimated_input_tokens" in got.result["token_usage"]
        with_warning_log += any(level == "WARNING" for level, _ in got.logs)
        with_info_log += any(level == "INFO" for level, _ in got.logs)

    assert paths == {(REC, 1), (REC, 2), (FALLBACK, 1), (FALLBACK, 2)}
    assert errors == {"ValueError", "RuntimeError", "FileNotFoundError"}
    assert seen_checks == {
        "preserve_missing",
        "fact_added",
        "negation_changed",
        "forbidden_phrase",
        "evidence_unknown",
        "empty",
        "refusal",
    }
    assert {s.condition for s in SCENARIOS} == {"A", "B", "C", "D", "auto"}
    assert {s.adjust for s in SCENARIOS} == {None, "shorter", "longer", "more_formal", "more_casual"}
    assert {s.prompt_version for s in SCENARIOS} >= {"rewrite-v1", "rewrite-v2", "rewrite-v3"}
    assert any(s.persona is None for s in SCENARIOS)
    assert with_refs >= 4 and with_estimate >= 6
    # 로그 비교가 빈 목록끼리의 비교가 아니다: 검색기 예외(WARNING) 셋, 거절·장애(INFO)는 토큰 어림과 같은 수.
    assert with_warning_log == 3 and with_info_log == with_estimate


# ── 한 인스턴스를 여러 요청이 함께 쓸 때 ─────────────────────────────────────


class KeyedProvider:
    """원문별로 응답 줄을 따로 둔다 — 요청이 뒤섞여 들어와도 제 응답을 받는다. 호출마다 양보한다."""

    def __init__(self, scripts: dict[str, tuple[ModelOutput | Exception, ...]]) -> None:
        self._scripts = {draft: [fresh(item) for item in script] for draft, script in scripts.items()}
        self.calls: dict[str, list[tuple[str, str]]] = {draft: [] for draft in scripts}

    async def generate(self, *, system: str, user: str) -> GenerationResult:
        position, draft = next((n, d) for n, d in enumerate(self._scripts) if f"[원문]\n{d}\n" in user)
        self.calls[draft].append((system, user))
        call_no = len(self.calls[draft])
        for _ in range(1 + (position * 3) % 5):  # 요청마다 다른 만큼 쉬어 끝나는 순서를 섞는다
            await asyncio.sleep(0)
        return reply(self._scripts[draft][call_no - 1], call_no, system, user)


def concurrent_cases() -> list[tuple[RewriteInput, tuple[ModelOutput | Exception, ...]]]:
    cases = []
    for n in range(2, 10):
        draft = f"회의 자료 {n}건은 오늘 정리해서 내일 오전 10시까지 보내드리겠습니다."
        good = out("팀장님, " + draft, evidence=["ep-1"] if n % 2 else [])
        bad = out(f"팀장님, 회의 자료 {n}건은 오늘 정리해서 보내드리겠습니다.")
        script = ((good,), (bad, good), (REFUSAL, bad), (bad, UNAVAILABLE))[n % 4]
        inp = RewriteInput(
            user_id="u-parity",
            contact_id=SUPERIOR.id,
            draft_text=draft,
            draft_version=n,
            situation="업무 보고",
            condition="C" if n % 2 else "B",
            as_of=AS_OF,
            adjust="shorter" if n % 3 == 0 else None,
        )
        cases.append((inp, script))
    return cases


def stub_set() -> dict[str, StubRetriever]:
    return {c: StubRetriever(f"stub-{c}-v1", STYLE if c == "C" else ()) for c in "ABCD"}


def strip_latency(result: RewriteResult) -> dict[str, Any]:
    dumped = result.model_dump(mode="json")
    dumped.pop("latency_ms")
    return dumped


@pytest.mark.anyio
async def test_concurrent_runs_on_one_instance_match_legacy_one_by_one() -> None:
    """지금 구현의 인스턴스 하나에 요청 여덟을 한꺼번에 넣어도, 예전 구현으로 하나씩 돌린 것과 같다.

    서버는 워크플로 인스턴스 하나를 모든 요청이 나눠 쓴다 — 실행 사이에 상태가 섞이면 여기서 드러난다.
    """
    cases = concurrent_cases()

    expected, expected_calls = [], {}
    for inp, script in cases:
        provider = KeyedProvider({inp.draft_text: script})
        legacy = LegacyRewriteWorkflow(provider=provider, retrievers=stub_set(), prompt_version="rewrite-v3")
        expected.append(strip_latency(await legacy.run(inp.model_copy(deep=True), SUPERIOR, PERSONA)))
        expected_calls.update(provider.calls)
    assert {(r["status"], r["attempts"]) for r in expected} == {(REC, 1), (REC, 2), (FALLBACK, 2)}

    shared = KeyedProvider({inp.draft_text: script for inp, script in cases})
    workflow = RewriteWorkflow(provider=shared, retrievers=stub_set(), prompt_version="rewrite-v3")
    results = await asyncio.gather(
        *(workflow.run(inp.model_copy(deep=True), SUPERIOR, PERSONA) for inp, _ in cases)
    )

    assert [strip_latency(r) for r in results] == expected
    assert shared.calls == expected_calls


# ── 대조가 차이를 실제로 잡는지 ──────────────────────────────────────────────
# 예전 구현의 사본만 살짝 바꿔(파일은 그대로, monkeypatch) 바꾸지 않은 실행과 견준다.
# 지금 구현과는 상관없이 도는 검사라, 지금 구현이 어떤 상태든 "비교가 눈을 뜨고 있는지"를 말해 준다.


def patch_regenerate_suffix(mp: pytest.MonkeyPatch) -> None:
    original = legacy_workflow.build_regenerate_suffix
    mp.setattr(legacy_workflow, "build_regenerate_suffix", lambda *a: original(*a) + " ")


def patch_no_regeneration(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(legacy_workflow, "MAX_REGENERATIONS", 0)


def patch_validation_always_passes(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(legacy_workflow, "validate_output", lambda *a: ValidationReport(passed=True))


def patch_no_budget_fit(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(legacy_workflow, "fit_examples", lambda examples: examples)


def patch_token_estimate(mp: pytest.MonkeyPatch) -> None:
    original = legacy_workflow.estimate_tokens
    mp.setattr(legacy_workflow, "estimate_tokens", lambda chars: original(chars) + 1)


def patch_adjust_ignored(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(legacy_workflow, "apply_adjust", lambda settings, adjust: settings)


def patch_today(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(legacy_workflow, "today_kst", lambda as_of=None: datetime(2026, 1, 1).date())


def patch_retrieval_error_propagates(mp: pytest.MonkeyPatch) -> None:
    async def retrieve(retriever: Any, inp: RewriteInput) -> list[RetrievedExample]:
        return await retriever.retrieve(
            user_id=inp.user_id,
            contact_id=inp.contact_id,
            draft_text=inp.draft_text,
            situation=inp.situation,
            as_of=inp.as_of,
        )

    mp.setattr(LegacyRewriteWorkflow, "_retrieve_safely", staticmethod(retrieve))


def patch_log_wording(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(legacy_workflow, "exception_kind", lambda exc: str(exc))  # 예외 문구를 로그에 찍는다


def tamper(change: Callable[[RewriteResult], RewriteResult]) -> Callable[[pytest.MonkeyPatch], None]:
    """예전 구현이 돌려주는 결과의 필드 하나만 바꾼다 — 그 필드를 비교가 보는지."""

    def apply(mp: pytest.MonkeyPatch) -> None:
        original = LegacyRewriteWorkflow.run

        async def run(self: Any, inp: Any, contact: Any, persona: Any) -> RewriteResult:
            return change(await original(self, inp, contact, persona))

        mp.setattr(LegacyRewriteWorkflow, "run", run)

    return apply


def reversed_keys(d: dict[str, Any]) -> dict[str, Any]:
    return dict(reversed(list(d.items())))


# (이름, 바꾸는 방법, 이 변화를 반드시 잡아야 하는 시나리오)
MUTATIONS: tuple[tuple[str, Callable[[pytest.MonkeyPatch], None], str], ...] = (
    ("regenerate-suffix-one-space", patch_regenerate_suffix, "fail-then-pass"),
    ("no-regeneration", patch_no_regeneration, "fail-then-pass"),
    ("validation-always-passes", patch_validation_always_passes, "fail-twice"),
    ("no-budget-fit", patch_no_budget_fit, "over-budget"),
    ("token-estimate-plus-one", patch_token_estimate, "refusal-then-pass"),
    ("adjust-ignored", patch_adjust_ignored, "adjust-more-formal"),
    ("today-shifted", patch_today, "pass-first"),
    ("retrieval-error-propagates", patch_retrieval_error_propagates, "retriever-raises"),
    ("log-wording", patch_log_wording, "retriever-raises"),
    (
        "warnings-reversed",
        tamper(lambda r: r.model_copy(update={"warnings": r.warnings[::-1]})),
        "fail-twice",
    ),
    (
        "attempt-log-reversed",
        tamper(lambda r: r.model_copy(update={"attempt_log": r.attempt_log[::-1]})),
        "fail-then-pass",
    ),
    (
        "token-usage-key-order",
        tamper(lambda r: r.model_copy(update={"token_usage": reversed_keys(r.token_usage)})),
        "pass-first",
    ),
    (
        "evidence-refs-key-order",
        tamper(lambda r: r.model_copy(update={"evidence_refs": reversed_keys(r.evidence_refs)})),
        "cond-C-evidence",
    ),
    (
        "evidence-refs-dropped",
        tamper(lambda r: r.model_copy(update={"evidence_refs": {}})),
        "cond-D-evidence",
    ),
    (
        "model-version",
        tamper(lambda r: r.model_copy(update={"model_version": "scripted-1"})),
        "refusal-then-pass",
    ),
    (
        "retrieval-version",
        tamper(lambda r: r.model_copy(update={"retrieval_version": "none"})),
        "pass-first",
    ),
    ("adjust-dropped", tamper(lambda r: r.model_copy(update={"adjust": None})), "adjust-fallback"),
    (
        "result-text-not-stripped",
        tamper(lambda r: r.model_copy(update={"result_text": r.attempt_log[-1].rewritten_text or ""})),
        "padded-output",
    ),
)


async def scenarios_that_differ(apply: Callable[[pytest.MonkeyPatch], None]) -> list[str]:
    """예전 구현을 바꾸기 전과 바꾼 뒤로 모든 시나리오를 돌려, 차이가 난 시나리오 id를 돌려준다."""
    clean = {scn.id: await run_one(LegacyRewriteWorkflow, scn) for scn in SCENARIOS}
    with pytest.MonkeyPatch.context() as mp:
        apply(mp)
        changed = {scn.id: await run_one(LegacyRewriteWorkflow, scn) for scn in SCENARIOS}
    return [scn.id for scn in SCENARIOS if differences(clean[scn.id], changed[scn.id], "원래", "바꾼 쪽")]


@pytest.mark.anyio
@pytest.mark.parametrize(("apply", "must_catch"), [pytest.param(a, s, id=n) for n, a, s in MUTATIONS])
async def test_comparison_catches_a_small_change(
    apply: Callable[[pytest.MonkeyPatch], None], must_catch: str
) -> None:
    caught = await scenarios_that_differ(apply)
    print(f"\n  잡은 시나리오 {len(caught)}/{len(SCENARIOS)}: {', '.join(caught)}")
    assert must_catch in caught


@pytest.mark.anyio
async def test_comparison_ignores_only_latency() -> None:
    """빼기로 한 것은 latency_ms 하나다 — 그것만 바뀌면 차이가 없고, 바꾼 것이 없어도 차이가 없다."""
    assert await scenarios_that_differ(lambda mp: None) == []
    slower = tamper(lambda r: r.model_copy(update={"latency_ms": r.latency_ms + 1234}))
    assert await scenarios_that_differ(slower) == []
