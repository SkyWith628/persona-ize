"""개인정보 가림의 정탐·오탐 목록과 **시간 상한** (2026-11-11 보안·정확성 검토).

- 시간: 계좌 낱말 패턴의 `\\s*[:：]?\\s*`가 맞닿아 제곱 시간이었다 — "은행" 뒤 공백 3만 자(30KB 요청 한 건)에
  23초 동안 서버 전체가 멈췄다. 이제 모든 패턴의 반복 수가 정해져 있다. 패턴을 고치면 여기 테스트를 돌린다.
- 정탐·오탐: 검토에서 나온 두 목록을 그대로 둔다 (가려야 하는 것 / 가리면 안 되는 것).
"""

from __future__ import annotations

import re
import time

import pytest

from app.importers import parse_import, pii
from app.importers.pii import mask_pii

BIG = 400_000  # 이 길이에서 제곱 시간이면 분 단위가 걸린다


def _seconds(fn, *args, **kwargs) -> float:  # noqa: ANN001, ANN002, ANN003
    started = time.perf_counter()
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001, S110 - 시간만 잰다 (형식 오류 422는 정상)
        pass
    return time.perf_counter() - started


ADVERSARIAL = {
    "은행 + 공백": lambda n: "은행" + " " * n + "1",
    "계좌 + 줄바꿈": lambda n: "계좌" + "\n" * n + "1",
    "계좌 번호 + 탭": lambda n: "계좌 번호" + "\t" * n + "1",
    "주민 + 공백": lambda n: "주민" + " " * n + "1",
    "주민번호 반복": lambda n: "주민번호 " * (n // 5) + "1",
    "은행 반복": lambda n: "은행 " * (n // 3) + "1",
    "a 반복": lambda n: "a" * n,
    "a@ 반복": lambda n: "a@" * (n // 2),
    "a@a.a.a…": lambda n: "a@" + "a." * (n // 2),
    "숫자만": lambda n: "010" * (n // 3),
    "12- 반복": lambda n: "12-" * (n // 3),
    "1234 반복": lambda n: "1234 " * (n // 5),
    "+82 반복": lambda n: "+82 " * (n // 4),
    "(010) 반복": lambda n: "(010)" * (n // 5),
    "0 1 0 띄어쓰기": lambda n: "0 1 " * (n // 4),
    "하이픈만": lambda n: "1" + "-" * n,
    "공백만 + 숫자": lambda n: " " * n + "1",
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_mask_pii_is_linear_on_adversarial_input(name: str) -> None:
    text = ADVERSARIAL[name](BIG)
    elapsed = _seconds(mask_pii, text)
    assert elapsed < 2.0, f"{name}: {len(text):,}자에 {elapsed:.1f}초"


def test_every_pii_pattern_has_bounded_adjacent_repeats() -> None:
    """열린 반복(`*`, `+`)의 `\\s`·공백류가 패턴에 없다 — 제곱 시간의 원인이었다."""
    patterns = {k: v.pattern for k, v in vars(pii).items() if isinstance(v, re.Pattern)}
    assert len(patterns) >= 10
    for name, pattern in patterns.items():
        assert not re.search(r"(?:\\s|\[ \\t\]|\\D|\[\^\\d\\n\]| )[*+]", pattern), (name, pattern)


@pytest.mark.parametrize(
    "text",
    [
        "2026년 10월 5일 오전 9:25, 가 : 계좌"
        + "\n" * 300_000
        + "1\n",  # 카카오톡: 빈 줄 30만 개가 낀 메시지
        "2026-10-05 09:25 | 가 | 은행" + " " * 300_000 + "1\n",  # 붙여넣기: 공백 30만 자
        "--------------- 2026년 10월 5일 월요일 ---------------\n[가] [오전 9:25] 은행"
        + " " * 300_000
        + "1\n",
        "Date,User,Message\n" + '2026-10-05 09:25:10,"가","계좌' + " " * 300_000 + '1"\n',
        '[{"speaker": "me", "sent_at": "2026-10-05T09:00:00+09:00", "text": "은행' + " " * 300_000 + '1"}]',
    ],
    ids=["kakao-android", "plain", "kakao-pc", "kakao-csv", "json"],
)
def test_parse_import_with_masking_is_fast_on_pathological_messages(text: str) -> None:
    """예전: 4만 자에 24초. 4000자를 넘는 메시지는 가림보다 먼저 버려지고, 패턴 자체도 선형이다."""
    elapsed = _seconds(parse_import, text, "auto", 540, mask=True, max_chars=2_000_000)
    assert elapsed < 2.0, f"{elapsed:.1f}초"
    ok = text.replace(" " * 300_000, " ").replace("\n" * 300_000, "\n")
    assert _seconds(parse_import, ok, "auto", 540, mask=True) < 1.0


def test_overlong_messages_are_dropped_before_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real = pii.mask_pii

    def spy(text: str) -> tuple[str, int]:
        seen.append(len(text))
        return real(text)

    monkeypatch.setattr("app.importers.mask_pii", spy)
    text = (
        "2026-10-05 09:25 | 가 | 010-1234-5678 " + "긴 글 " * 2000 + "\n"
        "2026-10-05 09:26 | 가 | 제 번호는 010-1234-5678 입니다\n"
    )
    parsed = parse_import(text, mask=True)
    assert [m.text for m in parsed.messages] == ["제 번호는 [전화번호] 입니다"]
    assert parsed.masked_pii == 1  # 버린 메시지의 번호는 세지 않는다
    assert seen and max(seen) <= 4000
    assert "4000자를 넘는" in parsed.problems[0]


# ---- 가려야 하는 것 (검토의 미탐 목록 + 기존) ----
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(010)1234-5678", "[전화번호]"),
        ("010 - 1234 - 5678", "[전화번호]"),
        ("010·1234·5678로 주세요", "[전화번호]로 주세요"),
        ("+82 (0)10-1234-5678", "[전화번호]"),
        ("유선은 0212345678", "유선은 [전화번호]"),
        ("사무실 02)123-4567", "사무실 [전화번호]"),
        ("(031) 123-4567 / 070-1234-5678", "[전화번호] / [전화번호]"),
        ("주민번호 900101 1234567", "주민번호 [주민등록번호]"),
        ("900101 1234567 입니다", "[주민등록번호] 입니다"),
        ("9001011234567", "[주민등록번호]"),
        ("0503153123456 (2005년생)", "[주민등록번호] (2005년생)"),
        ("계좌는 3333011234567", "계좌는 [계좌번호]"),
        ("신한 110123456789", "신한 [계좌번호]"),
        ("국민 12345678901234", "국민 [계좌번호]"),
        ("계좌 번호 : 1101234567890", "계좌 번호 : [계좌번호]"),  # 13자리지만 '계좌' 바로 뒤 → 계좌
        ("토스 1000-1234-5678", "토스 [계좌번호]"),  # 4-4-4도 은행 이름이 있으면
        ("농협 302-1234-5678-91", "농협 [계좌번호]"),
        ("카드 1234.5678.9012.3456", "카드 [카드번호]"),
    ],
)
def test_review_misses_are_now_masked(text: str, expected: str) -> None:
    out, count = mask_pii(text)
    assert out == expected and count >= 1


# ---- 가리면 안 되는 것 (검토의 오탐 목록) ----
@pytest.mark.parametrize(
    "text",
    [
        "배드민턴 점수 21-19-21-17-25",
        "로또 03-12-23-34-41",
        "운송장 6012-3456-7890",
        "농협 2026100512 주문",
        "주문번호 20261005123456 확인",
        "바코드 8801043015394",  # 13자리: 성별 숫자 3 + 88년생은 미래(2088) → 주민등록번호가 아니다
        "ISBN 978-89-123-4567-8",
        "문서번호 2026-10-05-01",
        "대표번호 1588-1234",
        "소수 0.0212345678",
        "3-2로 이겼고 좌석은 12-3, 기간은 10-05~10-12",
        "회의는 2026.10.05(월) 14:00~15:30, 3층 302호",
        "총 1,234,000원, 수량 3000000000개",
    ],
)
def test_review_false_positives_are_left_alone(text: str) -> None:
    assert mask_pii(text) == (text, 0)
