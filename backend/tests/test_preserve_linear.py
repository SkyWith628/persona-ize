"""보존 항목 정규식의 시간 상한 (2026-11-11 보안 검토).

`_QUANTITY`·`_PROMISE`는 숫자·한글만 길게 이어진 글에서 제곱 시간이 들었다(숫자 2,000자에 0.14초, 모델 출력
8,000자 검증에 2.4초 — 이벤트 루프 위). 뽑는 결과는 그대로 두고 시간만 선형으로 고쳤다. 여기서는
① 예전 패턴과 **같은 것을 뽑는지** ② 긴 입력에서도 빨리 끝나는지를 본다. 규칙이 바뀐 것이 아니므로
`evals/preserve_rules.md`는 그대로다.
"""

from __future__ import annotations

import csv
import json
import random
import re
import time
from pathlib import Path

import pytest

from app.ai import preserve
from app.ai.preserve import extract_preserved_items
from app.ai.validation import validate_output
from app.domain.persona import PersonaSettings
from app.domain.rewrite import ModelOutput

ROOT = Path(__file__).resolve().parents[2]

# 고치기 전 패턴 (기준)
OLD_QUANTITY = re.compile(
    r"\d[\d,\.]*\s*(?:개|명|분|원|만\s*원|억|건|번|회|장|페이지|쪽|부|%|퍼센트|km|m|kg|g|일간|주일|주간|시간|분량|배|차|권|대)"
)
OLD_PROMISE = re.compile(
    r"[가-힣]+(?:드리겠습니다|드릴게요|드릴께요|드리겠어요|겠습니다|할게요|할께요|하겠습니다|할게|할께|보낼게|보내드릴|보내겠|맡겠|처리하겠|확인하겠|전달하겠|공유하겠|진행하겠)"
)

PIECES = [
    "3개", "1,000원", "2.5km", "12 명", "30%", "5만 원", "1 만원", "100", "7", ",", ".", " ", "  ", "\n",
    "약 ", "총", "가격은,500원", ".5개", "1..2개", "3분30초", "10시 30분", "11월 5일", "2026",
    "보내드리겠습니다", "확인하겠습니다만", "할게요", "하겠습니다", "그리고", "자료", "겠습니다", "드릴게요!",
    "내일", "팀장님", "어렵습니다", "ㅋㅋ", "a", "kg", "m", "원", "1,", "1.", "12-", "9:25", "만", "부",
]  # fmt: skip


def _old(text: str) -> tuple[list[str], list[str]]:
    quantity = [m.group(0).strip() for m in OLD_QUANTITY.finditer(text) if m.group(0).strip()]
    promise = [m.group(0).strip() for m in OLD_PROMISE.finditer(text) if m.group(0).strip()]
    return quantity, promise


def _new(text: str) -> tuple[list[str], list[str]]:
    quantity = [m.group(1).strip() for m in preserve._QUANTITY.finditer(text)]
    promise = [m.group(0).strip() for m in preserve._PROMISE.finditer(text) if m.group(0).strip()]
    return quantity, promise


def _corpus() -> list[str]:
    texts: list[str] = []
    with (ROOT / "evals" / "eval_cases.csv").open(encoding="utf-8-sig", newline="") as fp:
        texts += [row["original_text"] for row in csv.DictReader(fp)]
    data = json.loads((ROOT / "data" / "synthetic" / "sample_conversations.json").read_text(encoding="utf-8"))
    texts += [m["text"] for conv in data["conversations"] for m in conv["messages"]]
    rng = random.Random(20261111)
    for _ in range(3000):
        texts.append("".join(rng.choice(PIECES) for _ in range(rng.randint(1, 12))))
    return texts


def test_linear_patterns_extract_exactly_what_the_old_ones_did() -> None:
    texts = _corpus()
    assert len(texts) > 3000
    hits = 0
    for text in texts:
        assert _new(text) == _old(text), text
        hits += bool(_old(text)[0] or _old(text)[1])
    assert hits > 1000  # 비교가 빈 결과끼리만 이뤄진 것이 아니다


SEEDS = [
    *("1", "1,", "1.", "가", "겠", "12 ", "1 1 ", "가 ", "1만 ", "3분"),
    # 2026-11-24: 영어 부정·약속과 이중 부정 규칙
    *("can't ", "I will ", "not ", "어렵지 않 ", "can ", "will ", "not only ", "n't able ", "a", "I'll "),
    *("못 가는 것은 ", "못", "안 되지 ", "가능하지 않", "어렵지만", " ", "can \t", "I will\n"),
]


@pytest.mark.parametrize("seed", SEEDS)
def test_preserve_and_validation_stay_fast_on_long_runs(seed: str) -> None:
    """초안 상한(2,000자)의 4배, 모델 출력 길이(8,000자)에서도 수십 ms 안에 끝난다.

    예전에는 숫자만 8,000자인 출력의 검증에 2.4초가 걸렸다.
    """
    text = (seed * 8000)[:8000]
    started = time.perf_counter()
    items = extract_preserved_items(text)
    validate_output(
        "내일 오전 10시까지 3개 보내드릴게요", ModelOutput(rewritten_text=text), items, PersonaSettings(), []
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"{seed!r} × 8000자에 {elapsed:.2f}초"
