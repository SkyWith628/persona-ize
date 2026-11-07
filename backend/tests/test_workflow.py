"""워크플로 분기: 통과 / 재생성 후 통과 / 재실패 → 원문 유지 / 모델 장애 → 원문 유지."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.ai.providers.fake import ScriptedProvider
from app.ai.retrieval.base import NullRetriever
from app.ai.workflow import RewriteWorkflow
from app.core.errors import ModelUnavailableError
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
