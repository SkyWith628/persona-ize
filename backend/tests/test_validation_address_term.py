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


def test_kinship_vocative_added_without_setting_fails() -> None:
    draft = "토요일 오전에 차 좀 빌려줄 수 있어?"
    items = extract_preserved_items(draft)
    bad = validate_output(
        draft,
        ModelOutput(rewritten_text="형, 토요일 오전에 차 좀 빌려줄 수 있어?"),
        items,
        PersonaSettings(),
        [],
    )
    assert [v.check for v in bad.violations] == ["fact_added"] and "'형'" in bad.violations[0].detail
    ok = validate_output(
        draft,
        ModelOutput(rewritten_text="형, 토요일 오전에 차 좀 빌려줄 수 있어?"),
        items,
        PersonaSettings(address_term="형"),
        [],
    )
    assert ok.passed
    # 원문에 이미 있으면 통과, 단어 일부('형식')는 호칭이 아니다
    draft2 = "형 토요일 오전에 차 좀 빌려줄 수 있어?"
    assert validate_output(
        draft2,
        ModelOutput(rewritten_text="형, 토요일 오전에 차 빌려줄 수 있어?"),
        extract_preserved_items(draft2),
        PersonaSettings(),
        [],
    ).passed
    assert validate_output(
        draft,
        ModelOutput(rewritten_text="토요일 오전에 차 좀 빌려줄 수 있어? 형식은 상관없어"),
        items,
        PersonaSettings(),
        [],
    ).passed
