"""워크플로 분기: 통과 / 재생성 후 통과 / 재실패 → 원문 유지 / 모델 장애 → 원문 유지."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.ai.providers.fake import ScriptedProvider
from app.ai.retrieval.base import NullRetriever
from app.ai.workflow import RewriteWorkflow
from app.core.errors import ModelRefusalError, ModelUnavailableError
from app.domain.contact import Contact
from app.domain.persona import PersonaSettings
from app.domain.rewrite import ModelOutput, RewriteInput

DRAFT = "오늘 정리해서 내일 오전까지 보내드릴게요."
GOOD = ModelOutput(
    rewritten_text="팀장님, 오늘 정리해서 내일 오전까지 보내드리겠습니다.", change_reasons=["호칭 추가"]
)
BAD = ModelOutput(rewritten_text="팀장님, 내일 오후까지 보내드리겠습니다.")


def make(provider) -> tuple[RewriteWorkflow, RewriteInput, Contact, PersonaSettings]:
    null = NullRetriever()
    wf = RewriteWorkflow(
        provider=provider,
        retrievers={"A": null, "B": null, "C": null, "D": null},
        prompt_version="rewrite-v1",
    )
    contact = Contact(
        id=uuid4(), user_id="u", alias="박지현", relationship_type="superior", created_at=datetime.now(UTC)
    )
    inp = RewriteInput(
        user_id="u", contact_id=contact.id, draft_text=DRAFT, draft_version=1, situation="업무 보고"
    )
    return wf, inp, contact, PersonaSettings(formality=5, address_term="팀장님")


@pytest.mark.anyio
async def test_pass_first_try() -> None:
    wf, inp, contact, persona = make(ScriptedProvider([GOOD]))
    r = await wf.run(inp, contact, persona)
    assert r.status == "recommended"
    assert r.attempts == 1
    assert r.result_text == GOOD.rewritten_text
    assert r.intent == "report"


@pytest.mark.anyio
async def test_regenerate_once_then_pass() -> None:
    provider = ScriptedProvider([BAD, GOOD])
    wf, inp, contact, persona = make(provider)
    r = await wf.run(inp, contact, persona)
    assert r.status == "recommended"
    assert r.attempts == 2
    # 재생성 프롬프트에 구체적 위반 항목이 들어갔는지
    assert "위반 항목" in provider.prompts[1]
    assert "내일 오후" in provider.prompts[1]
    # 시도별 기록: 1차 실패(어떤 위반인지), 2차 통과
    assert [(a.attempt, a.passed) for a in r.attempt_log] == [(1, False), (2, True)]
    assert r.attempt_log[0].rewritten_text == BAD.rewritten_text
    assert "preserve_missing" in {v.check for v in r.attempt_log[0].violations}  # "내일 오전" 누락
    assert r.attempt_log[1].violations == []


@pytest.mark.anyio
async def test_fail_twice_keeps_original() -> None:
    wf, inp, contact, persona = make(ScriptedProvider([BAD, BAD]))
    r = await wf.run(inp, contact, persona)
    assert r.status == "fallback_original"
    assert r.result_text == DRAFT
    assert r.attempts == 2
    assert not r.validation.passed


@pytest.mark.anyio
async def test_model_unavailable_keeps_original() -> None:
    wf, inp, contact, persona = make(ScriptedProvider([ModelUnavailableError("429")]))
    r = await wf.run(inp, contact, persona)
    assert r.status == "fallback_original"
    assert r.result_text == DRAFT
    assert "429" in " ".join(r.warnings)


@pytest.mark.anyio
async def test_refusal_is_regenerated_once() -> None:
    wf, inp, contact, persona = make(ScriptedProvider([ModelRefusalError("거절"), GOOD]))
    r = await wf.run(inp, contact, persona)
    assert r.status == "recommended"
    assert r.attempts == 2
    assert [
        (a.attempt, a.passed, a.violations[0].check if a.violations else None) for a in r.attempt_log
    ] == [
        (1, False, "refusal"),
        (2, True, None),
    ]


@pytest.mark.anyio
async def test_refusal_twice_keeps_original() -> None:
    wf, inp, contact, persona = make(ScriptedProvider([ModelRefusalError("거절"), ModelRefusalError("거절")]))
    r = await wf.run(inp, contact, persona)
    assert r.status == "fallback_original"
    assert r.attempts == 2
    assert all(a.violations[0].check == "refusal" for a in r.attempt_log)


@pytest.mark.anyio
async def test_condition_a_hides_persona() -> None:
    provider = ScriptedProvider([GOOD])
    wf, inp, contact, persona = make(provider)
    inp = inp.model_copy(update={"condition": "A"})
    await wf.run(inp, contact, persona)
    assert "팀장님" not in provider.prompts[0].split("[상황]")[0]  # 설정 블록에 호칭 없음
    assert "상대 정보 없음" in provider.prompts[0]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_prompt_v2_has_address_rule_and_same_placeholders() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.ai.prompt import available_versions, build_prompt
    from app.domain.contact import Contact

    assert {"rewrite-v1", "rewrite-v2"} <= set(available_versions())
    contact = Contact(
        id=uuid4(), user_id="u", alias="이서연", relationship_type="colleague", created_at=datetime.now(UTC)
    )
    v2 = build_prompt(
        version="rewrite-v2",
        contact=contact,
        persona=PersonaSettings(),
        intent="apology",
        preserved=[],
        draft_text="어제는 제가 좀 예민했던 것 같아요. 미안합니다.",
        situation="사과",
        retrieved=[],
    )
    assert "호칭 규칙" in v2.system and "새 호칭" in v2.system and "warnings" in v2.system
    assert "[원문]" in v2.user and "[문체 예시" in v2.user  # user.md는 v1과 같은 자리표시자


def test_prompt_v3_states_today_and_older_versions_do_not() -> None:
    """v3는 [오늘]을 넣는다 — 원문의 '다음 달'을 그래프의 절대 날짜와 견주려면 기준일이 필요하다."""
    from datetime import UTC, date, datetime
    from uuid import uuid4

    from app.ai.prompt import available_versions, build_prompt
    from app.domain.contact import Contact

    assert "rewrite-v3" in available_versions()
    contact = Contact(
        id=uuid4(), user_id="u", alias="최유진", relationship_type="friend", created_at=datetime.now(UTC)
    )
    kwargs = dict(
        contact=contact,
        persona=PersonaSettings(),
        intent="schedule",
        preserved=[],
        draft_text="다음 달 둘째 주 토요일 저녁 어때?",
        situation="일정 조율",
        retrieved=[],
        today=date(2026, 11, 10),
    )
    v3 = build_prompt(version="rewrite-v3", **kwargs)
    assert "[오늘] 2026-11-10 (화요일)" in v3.user
    assert "날짜 대조" in v3.system and "호칭 규칙" in v3.system  # v2 규칙은 그대로
    assert "[원문]" in v3.user and "[문체 예시" in v3.user
    v2 = build_prompt(version="rewrite-v2", **kwargs)
    assert "[오늘]" not in v2.user  # 옛 버전 재현성: 본문이 바뀌지 않는다


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (datetime(2026, 10, 20, 14, 59, tzinfo=UTC), "2026-10-20"),  # KST 23:59
        (datetime(2026, 10, 20, 15, 0, tzinfo=UTC), "2026-10-21"),  # KST 자정 — 날짜가 넘어간다
    ],
)
async def test_workflow_passes_as_of_date_in_kst_to_prompt(as_of: datetime, expected: str) -> None:
    provider = ScriptedProvider([GOOD])
    null = NullRetriever()
    wf = RewriteWorkflow(
        provider=provider,
        retrievers={"A": null, "B": null, "C": null, "D": null},
        prompt_version="rewrite-v3",
    )
    _, inp, contact, persona = make(provider)
    r = await wf.run(inp.model_copy(update={"as_of": as_of}), contact, persona)
    assert r.status == "recommended"
    assert f"[오늘] {expected}" in provider.prompts[0]


# ── 그래프 구조 (LangGraph) ───────────────────────────────────────────────────


def test_graph_has_fixed_steps_and_two_forks() -> None:
    """단계와 갈림길이 설계대로다 — 경로는 고정이고 갈림길은 generate·validate 뒤 두 곳뿐."""
    wf, *_ = make(ScriptedProvider([]))
    drawn = wf._graph.get_graph()
    assert set(drawn.nodes) == {
        "__start__",
        "analyze",
        "retrieve",
        "compose",
        "generate",
        "validate",
        "recommend",
        "fallback",
        "__end__",
    }
    fixed = {(e.source, e.target) for e in drawn.edges if not e.conditional}
    forks = {(e.source, e.target) for e in drawn.edges if e.conditional}
    assert fixed == {
        ("__start__", "analyze"),
        ("analyze", "retrieve"),
        ("retrieve", "compose"),
        ("compose", "generate"),
        ("recommend", "__end__"),
        ("fallback", "__end__"),
    }
    assert forks == {
        ("generate", "validate"),
        ("generate", "generate"),  # 거절 → 재생성
        ("generate", "fallback"),  # 모델 장애 또는 기회 없음
        ("validate", "recommend"),
        ("validate", "generate"),  # 검증 실패 → 재생성
        ("validate", "fallback"),
    }


@pytest.mark.parametrize(
    ("outcome", "attempts", "expected"),
    [
        ("output", 1, "validate"),
        ("output", 2, "validate"),
        ("refusal", 1, "generate"),
        ("refusal", 2, "fallback"),
        ("unavailable", 1, "fallback"),  # 장애는 기회가 남아도 다시 부르지 않는다
        ("unavailable", 2, "fallback"),
    ],
)
def test_fork_after_generate(outcome: str, attempts: int, expected: str) -> None:
    from app.ai.workflow import _after_generate

    assert _after_generate({"outcome": outcome, "attempts": attempts}) == expected  # type: ignore[typeddict-item]


@pytest.mark.parametrize(
    ("passed", "attempts", "expected"),
    [(True, 1, "recommend"), (True, 2, "recommend"), (False, 1, "generate"), (False, 2, "fallback")],
)
def test_fork_after_validate(passed: bool, attempts: int, expected: str) -> None:
    from app.ai.workflow import _after_validate
    from app.domain.rewrite import ValidationReport

    state = {"report": ValidationReport(passed=passed, violations=[]), "attempts": attempts}
    assert _after_validate(state) == expected  # type: ignore[arg-type]


def test_graph_keeps_no_state_anywhere() -> None:
    """상태에 초안·대화 원문이 들어 있다 — 체크포인터·저장소·캐시가 붙으면 안 된다."""
    wf, *_ = make(ScriptedProvider([]))
    assert wf._graph.checkpointer is None
    assert wf._graph.store is None
    assert wf._graph.cache is None


@pytest.mark.anyio
async def test_tracing_is_off_inside_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """환경변수로 LangSmith 추적을 켜도 꺼져 있다 — 실행 밖에서도, 실행 구간에서도."""
    from langsmith.utils import tracing_is_enabled

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    seen: list[object] = []

    class Peek(ScriptedProvider):
        async def generate(self, *, system: str, user: str):  # type: ignore[no-untyped-def]
            seen.append(tracing_is_enabled())
            return await super().generate(system=system, user=user)

    # workflow 모듈이 import될 때 프로세스 전체에서 꺼 둔다 — 환경변수로 켜도 바깥에서부터 꺼져 있다
    assert tracing_is_enabled() is False
    wf, inp, contact, persona = make(Peek([GOOD]))
    r = await wf.run(inp, contact, persona)
    assert r.status == "recommended"
    assert seen == [False]


def test_graph_is_built_once_and_stays_out_of_repr_and_eq() -> None:
    provider = ScriptedProvider([])
    null = NullRetriever()
    kwargs = dict(provider=provider, retrievers={"A": null}, prompt_version="rewrite-v1")
    a, b = RewriteWorkflow(**kwargs), RewriteWorkflow(**kwargs)  # type: ignore[arg-type]
    assert a._graph is not b._graph
    assert a == b  # 비교는 공개 필드만 본다
    assert "_graph" not in repr(a)
    assert [f.name for f in __import__("dataclasses").fields(a) if f.init] == [
        "provider",
        "retrievers",
        "prompt_version",
    ]


@pytest.mark.anyio
async def test_provider_swapped_after_construction_is_used() -> None:
    """다른 테스트들이 `workflow.provider = ...`로 바꿔 끼운다 — 단계는 실행 때의 값을 읽어야 한다."""
    wf, inp, contact, persona = make(ScriptedProvider([BAD, BAD]))
    graph = wf._graph
    swapped = ScriptedProvider([GOOD])
    wf.provider = swapped
    r = await wf.run(inp, contact, persona)
    assert r.status == "recommended" and swapped.calls == 1
    assert wf._graph is graph  # 다시 컴파일하지 않는다


@pytest.mark.anyio
async def test_unknown_condition_raises_before_any_step() -> None:
    provider = ScriptedProvider([GOOD])
    wf, inp, contact, persona = make(provider)
    with pytest.raises(ValueError, match="워크플로는 A~D 조건만 받습니다: auto"):
        await wf.run(inp.model_copy(update={"condition": "auto"}), contact, persona)
    assert provider.calls == 0


@pytest.mark.anyio
async def test_unavailable_on_second_attempt_does_not_recommend_earlier_output() -> None:
    wf, inp, contact, persona = make(ScriptedProvider([BAD, ModelUnavailableError("503")]))
    r = await wf.run(inp, contact, persona)
    assert r.status == "fallback_original"
    assert r.result_text == DRAFT
    assert r.attempts == 2
    assert [a.violations[0].check for a in r.attempt_log][-1] == "empty"
    assert r.token_usage == {"input_tokens": 1, "output_tokens": 1} | {
        "estimated_input_tokens": r.token_usage["estimated_input_tokens"]
    }
    assert r.token_usage["estimated_input_tokens"] > 0


@pytest.mark.anyio
async def test_concurrent_runs_on_one_instance_do_not_mix_state() -> None:
    """같은 인스턴스(컴파일된 그래프 하나)를 여러 요청이 동시에 써도 상태가 섞이지 않는다."""
    import asyncio

    from app.ai.providers.base import GenerationResult

    drafts = {
        "pass": "오늘 정리해서 내일 오전까지 보내드릴게요.",
        "retry": "회의는 3시에 시작합니다.",
        "fail": "금요일까지 10만원 보내줄게.",
    }

    class ByDraft:
        """원문에 따라 다르게 답하고, 호출마다 양보해 세 요청의 단계가 서로 끼어들게 한다."""

        model_version = "fake-by-draft"

        def __init__(self) -> None:
            self.seen: dict[str, int] = {}

        async def generate(self, *, system: str, user: str) -> GenerationResult:
            key = next(k for k, d in drafts.items() if d in user)
            n = self.seen[key] = self.seen.get(key, 0) + 1
            for _ in range(5):
                await asyncio.sleep(0)
            if key == "fail":
                raise ModelRefusalError(f"거절-{n}")
            text = drafts[key] if key == "pass" or n == 2 else "회의는 4시에 시작합니다."
            return GenerationResult(
                output=ModelOutput(rewritten_text=text),
                model_version=self.model_version,
                usage={"input_tokens": len(key), "output_tokens": 1},
            )

    provider = ByDraft()
    wf, inp, contact, _ = make(provider)
    inputs = {k: inp.model_copy(update={"draft_text": d, "condition": "A"}) for k, d in drafts.items()}
    results = dict(
        zip(inputs, await asyncio.gather(*(wf.run(i, contact, None) for i in inputs.values())), strict=True)
    )

    assert provider.seen == {"pass": 1, "retry": 2, "fail": 2}
    assert (results["pass"].status, results["pass"].attempts) == ("recommended", 1)
    assert results["pass"].result_text == drafts["pass"]
    assert results["pass"].token_usage == {"input_tokens": 4, "output_tokens": 1}
    assert (results["retry"].status, results["retry"].attempts) == ("recommended", 2)
    assert results["retry"].result_text == drafts["retry"]
    assert results["retry"].token_usage == {"input_tokens": 10, "output_tokens": 2}
    assert [a.rewritten_text for a in results["retry"].attempt_log] == [
        "회의는 4시에 시작합니다.",
        drafts["retry"],
    ]
    assert (results["fail"].status, results["fail"].attempts) == ("fallback_original", 2)
    assert results["fail"].result_text == drafts["fail"]
    assert results["fail"].warnings[1:] == ["거절-2"]
    assert set(results["fail"].token_usage) == {"estimated_input_tokens"}
    assert [len(r.attempt_log) for r in results.values()] == [1, 2, 2]
