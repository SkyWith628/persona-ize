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


def test_english_draft_keeps_dates_and_separated_numbers() -> None:
    """영어 대화 (2026-11-23): 달·요일·자릿수 쉼표가 든 수가 바뀌면 실패하고, 표기만 바뀌면 통과한다."""
    draft = "will send the signed agreement by Friday Oct 17. 2.8% from Nov 15 and 4,500 pcs per month"
    items = extract_preserved_items(draft)

    def check(text: str) -> set[str]:
        report = validate_output(draft, ModelOutput(rewritten_text=text), items, None, [])
        return {v.check for v in report.violations}

    good = (
        "I will send the signed agreement by Friday, October 17th. 2.8% from Nov 15 and 4,500 pcs per month."
    )
    assert check(good) == set()
    assert "preserve_missing" in check(good.replace("4,500", "3,500"))
    assert "preserve_missing" in check(good.replace("October 17th", "November 17th"))
    assert "preserve_missing" in check(good.replace("Friday", "Thursday"))


def _violations(draft: str, text: str) -> set[str]:
    report = validate_output(
        draft, ModelOutput(rewritten_text=text), extract_preserved_items(draft), None, []
    )
    return {v.check for v in report.violations}


def test_review_2026_11_23_holes_are_closed() -> None:
    """검증 단계 리뷰(2026-11-23)에서 재현한 것들."""
    # 사실이 바뀌었는데 통과하던 것
    assert "preserve_missing" in _violations("회의는 14:30에 시작합니다", "회의는 15:30에 시작합니다.")
    assert "negation_changed" in _violations("금요일은 어렵습니다", "안녕하세요, 금요일 가능합니다.")
    assert "negation_changed" in _violations(
        "안녕하세요 금요일에 뵙겠습니다", "안녕하세요, 금요일은 어렵겠습니다."
    )
    # 사실을 지켰는데 실패하던 것
    assert _violations("단가는 52000원입니다", "단가는 52,000원입니다.") == set()
    assert _violations("이번 주 일정 공유드립니다", "이번 주의 일정을 공유드립니다.") == set()
    # 고친 뒤에도 진짜 변경은 잡는다
    assert "preserve_missing" in _violations("단가는 52000원입니다", "단가는 53,000원입니다.")
    assert "preserve_missing" in _violations("이번 주 금까지 드릴게요", "이번 주 목까지 드릴게요.")
    assert _violations("오늘 안에 보내드리겠습니다", "오늘 안에 보내 드리겠습니다.") == set()


def test_english_negation_and_promise() -> None:
    """2026-11-24: 영어의 부정·약속 규칙이 없어 can't → can, 약속 누락이 통과했다."""
    assert "negation_changed" in _violations("I can't send it by Friday", "I can send it by Friday.")
    assert "negation_changed" in _violations("I won't be there on Friday", "I will be there on Friday.")
    assert "negation_changed" in _violations("I can send it by Friday", "I can't send it by Friday.")
    assert "negation_changed" in _violations("See you on Friday", "I can’t see you on Friday.")
    missing = _violations("I'll send the draft tomorrow", "The draft is ready tomorrow.")
    assert missing == {"preserve_missing"}
    # 표기만 바뀐 것은 통과한다
    assert _violations("I can't send it by Friday", "I cannot send it by Friday.") == set()
    assert _violations("I can't send it by Friday", "I’m unable to send it by Friday.") == set()
    assert _violations("I can't make Friday", "Friday doesn't work for me.") == set()  # 불가 → 부정
    assert _violations("I'll send the draft tomorrow", "I will send the draft tomorrow.") == set()
    assert _violations("we'll confirm by Friday", "We will confirm by Friday.") == set()
    assert _violations("Not only Friday but also Monday is fine", "Friday and Monday are fine.") == set()


def test_korean_double_negation_flips_direction() -> None:
    """2026-11-24: '어렵지 않습니다'가 일반 부정으로만 읽혀 뒤집힌 결과가 통과했다."""
    assert "negation_changed" in _violations("금요일은 어렵습니다", "금요일은 어렵지 않습니다.")
    assert "negation_changed" in _violations("금요일까지 가능합니다", "금요일까지는 가능하지 않습니다.")
    assert "negation_changed" in _violations("금요일은 어렵지 않습니다", "금요일은 어렵습니다.")
    assert "negation_changed" in _violations("금요일은 어렵습니다", "금요일도 못 가는 건 아닙니다.")
    # 뜻을 지킨 것은 통과한다
    assert _violations("금요일은 어렵습니다", "금요일은 힘들 것 같습니다.") == set()
    assert _violations("금요일은 어렵습니다", "금요일은 어렵지 않을까 싶습니다.") == set()
    assert _violations("어렵지 않습니다", "어렵지 않아요") == set()
    # '어렵지만'도 불가다
    assert "negation_changed" in _violations(
        "금요일은 어렵지만 수요일은 됩니다", "금요일과 수요일 모두 됩니다."
    )
    assert _violations("금요일은 어렵지만 수요일은 됩니다", "금요일은 어렵고 수요일은 됩니다.") == set()
    assert _violations("금요일까지 가능합니다", "금요일까지 가능하지 않을까요?") == set()
