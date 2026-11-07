"""호칭 처리: 설정된 호칭 추가는 허용, 원문에 없는 실명 추가는 실패."""

from __future__ import annotations

from app.ai.preserve import extract_preserved_items
from app.ai.validation import validate_output
from app.domain.persona import PersonaSettings
from app.domain.rewrite import ModelOutput

DRAFT = "내일 오전까지 보내드릴게요."


def _run(result: str, persona: PersonaSettings | None):
    return validate_output(
        DRAFT, ModelOutput(rewritten_text=result), extract_preserved_items(DRAFT), persona, []
    )


def test_configured_named_address_term_allowed() -> None:
    persona = PersonaSettings(address_term="지현 팀장님")
    assert _run("지현 팀장님, 내일 오전까지 보내드리겠습니다.", persona).passed


def test_unconfigured_real_name_added_fails() -> None:
    r = _run("서연 선배가 말한 대로 내일 오전까지 보내드리겠습니다.", PersonaSettings())
    assert [v.check for v in r.violations] == ["fact_added"]
