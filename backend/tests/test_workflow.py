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
