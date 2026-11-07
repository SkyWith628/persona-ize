"""출력 검증. 사실 보존 실패는 다른 점수로 상쇄되지 않는다."""

from __future__ import annotations

from datetime import UTC, datetime

from app.ai.preserve import extract_preserved_items
from app.ai.validation import validate_output
from app.domain.persona import PersonaSettings
from app.domain.rewrite import ModelOutput, RetrievedExample

DRAFT = "오늘 정리해서 내일 오전까지 보내드릴게요."


def run(
    result: str, persona: PersonaSettings | None = None, evidence: list[str] | None = None, retrieved=None
):
    out = ModelOutput(rewritten_text=result, evidence_ids=evidence or [])
    return validate_output(DRAFT, out, extract_preserved_items(DRAFT), persona, retrieved or [])


def test_style_change_passes() -> None:
    assert run("팀장님, 오늘 정리해서 내일 오전까지 보내드리겠습니다.").passed


def test_date_shift_fails() -> None:
    r = run("팀장님, 오늘 정리해서 내일 오후까지 보내드리겠습니다.")
    assert not r.passed
    checks = {v.check for v in r.violations}
    assert "preserve_missing" in checks  # 내일 오전 사라짐
    assert "fact_added" in checks  # 내일 오후 추가


def test_added_reason_with_date_fails() -> None:
    r = run("오늘은 회의가 3시에 있어 정리가 어렵고, 내일 오전까지 보내드리겠습니다.")
    assert not r.passed
    assert {"fact_added", "negation_changed"} <= {v.check for v in r.violations}


def test_dropped_promise_fails() -> None:
    r = run("오늘 정리해서 내일 오전까지요.")
    assert not r.passed
    assert any("약속" in v.detail for v in r.violations)


def test_forbidden_phrase() -> None:
    persona = PersonaSettings(forbidden_phrases=["ㅋㅋ"])
    r = run("오늘 정리해서 내일 오전까지 보내드릴게요 ㅋㅋ", persona=persona)
    assert [v.check for v in r.violations] == ["forbidden_phrase"]


def test_unknown_evidence_rejected() -> None:
    retrieved = [
        RetrievedExample(
            id="ep1", purpose="style", text="네 팀장님", speaker_role="me", sent_at=datetime.now(UTC)
        )
    ]
    ok = run("오늘 정리해서 내일 오전까지 보내드리겠습니다.", evidence=["ep1"], retrieved=retrieved)
    bad = run("오늘 정리해서 내일 오전까지 보내드리겠습니다.", evidence=["ep999"], retrieved=retrieved)
    assert ok.passed
    assert [v.check for v in bad.violations] == ["evidence_unknown"]


def test_empty_output() -> None:
    r = run("   ")
    assert [v.check for v in r.violations] == ["empty"]
