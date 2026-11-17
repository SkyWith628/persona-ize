"""내 말투 지문 — 한 상대에게 보낸 내 메시지에서 규칙으로 읽은 말버릇 (rewrite-v5의 `[내 말투 지문]` 블록).

왜 필요한가 (2026-11-17 실제 모델 확인)
- 같은 초안을 상대 넷에게 돌리면 말투 단계(합쇼체/해요체/반말)는 달라지지만 문장은 거의 같았다. 프롬프트가
  "최소한으로 다듬기"였고, 모델이 보는 내 말투는 설정 수치 다섯 개와 초안과 비슷한 과거 발화 두세 개뿐이었다.
- 문체 예시는 **초안과 비슷한** 발화를 고른다. 내가 이 상대에게 늘 붙이는 'ㅋㅋ', 마침표를 안 찍는 버릇처럼
  주제와 무관한 습관은 예시 두세 개에 우연히 들어 있을 때만 전해진다. 그래서 내 메시지 전체에서 버릇을
  세어 한 블록으로 준다.

무엇을 읽는가 (전부 정규식·집계, 모델 호출 없음)
- 길이, 문장 끝 습관(닫힌 어미 목록), 마침표·느낌표·물결·말줄임, 웃음·울음 표시, 이모지, 자음 줄임말,
  말머리, 자주 쓰는 말.

지키는 것
- **대화 내용을 싣지 않는다.** 블록에 나오는 글자는 이 파일의 닫힌 목록(어미·말머리·버릇말·자음 줄임말)과
  문장부호·웃음 표시뿐이다. 이름·호칭·날짜·사유는 여기서 나올 수 없다 — 호칭은 승인된 설정으로만 들어간다는
  원칙(프롬프트 규칙 6)을 이 블록이 우회하지 못한다.
- 건수를 함께 적는다. 근거가 얇으면(메시지 `MIN_MESSAGES`건 미만) 블록을 만들지 않는다.
- 문턱값은 전부 판단이다(측정한 값이 아니다).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from statistics import median

from app.ai.register import split_sentences

MIN_MESSAGES = 3  # 이보다 적으면 버릇이라고 말하지 않는다
MIN_REPEAT = 2  # 한 번 나온 것은 버릇이 아니다
TOP_ENDINGS = 3
TOP_WORDS = 4

_TRAILING = re.compile(r"[\s.!?~…,;:)(\]\[\"'^*♡♥ㅋㅎㅠㅜ\U0001F300-\U0001FAFF☀-➿]+$")
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿]")
_LAUGH = re.compile(r"[ㅋㅎ]{2,}")
_CRY = re.compile(r"[ㅠㅜ]+")
_ELLIPSIS = re.compile(r"…|\.{2,}")
_FIRST_TOKEN = re.compile(r"^\s*([가-힣ㄱ-ㅎA-Za-z]{1,4})(?=[\s,!~.?…]|$)")
_TOKEN = re.compile(r"[가-힣ㄱ-ㅎ]+")

# 문장 끝 (긴 것부터 맞춘다). 표시는 '~어미'.
_ENDINGS = (
    "드리겠습니다",
    "겠습니다",
    "드립니다",
    "습니다",
    "입니다",
    "합니다",
    "니다",
    "거든요",
    "는데요",
    "을게요",
    "게요",
    "세요",
    "네요",
    "까요",
    "죠",
    "요",
    "거든",
    "는데",
    "잖아",
    "을게",
    "게",
    "자",
    "지",
    "네",
    "냐",
    "니",
    "래",
    "해",
    "야",
    "어",
    "아",
    "다",
)
_NOUN_ENDING = re.compile(r"(?:음|함|임|됨|없음|있음)$")  # 음슴체
# 말머리로 세는 낱말 (호칭·이름은 넣지 않는다)
_OPENERS = frozenset(
    (
        "넵 네 넹 예 옙 응 어 아 아니 헐 오 와 음 흠 엇 앗 아하 오키 굿 "
        "근데 그럼 그래 일단 혹시 저 저기 참 아참"
    ).split()
)
# 자주 쓰는 말로 세는 낱말
_HABIT_WORDS = frozenset(
    (
        "진짜 좀 약간 일단 혹시 그냥 완전 너무 넘 되게 엄청 살짝 아마 솔직히 사실 근데 암튼 아무튼 정말 꼭"
    ).split()
)
_CONSONANT_SLANG = frozenset("ㄱ ㄱㄱ ㄴㄴ ㅇㅋ ㅇㅇ ㄱㅅ ㅈㅅ ㅊㅋ ㅇㅈ ㄷㄷ".split())


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0


def _ending_of(sentence: str) -> str | None:
    core = _TRAILING.sub("", sentence)
    if not core:
        return None
    if _NOUN_ENDING.search(core):
        return "~음/~함 (명사로 끝냄)"
    for ending in _ENDINGS:
        if core.endswith(ending) and len(core) > len(ending):
            return f"~{ending}"
    return None


def _quoted(items: Sequence[str]) -> str:
    return ", ".join(f"'{item}'" for item in items)


def render_voice_block(texts: Sequence[str]) -> str | None:  # noqa: PLR0912, PLR0915 — 버릇별 규칙을 한곳에 나열한다
    """내 메시지 본문들 → `[내 말투 지문]` 블록 본문. 말할 버릇이 없거나 메시지가 적으면 None."""
    messages = [t.strip() for t in texts if t and t.strip()]
    n = len(messages)
    if n < MIN_MESSAGES:
        return None
    lines: list[str] = []

    # 길이
    lengths = [len(m) for m in messages]
    sentence_counts = [max(len(split_sentences(m)), 1) for m in messages]
    lines.append(
        f"길이: 메시지 하나가 보통 {int(median(lengths))}자 안팎, 문장 {int(median(sentence_counts))}개"
    )

    # 문장 끝
    endings: Counter[str] = Counter()
    for m in messages:
        for s in split_sentences(m):
            ending = _ending_of(s)
            if ending:
                endings[ending] += 1
    top = [(e, c) for e, c in endings.most_common(TOP_ENDINGS) if c >= MIN_REPEAT]
    if top:
        lines.append("자주 쓰는 문장 끝: " + ", ".join(f"'{e}' {c}번" for e, c in top))

    # 문장부호
    period = sum(1 for m in messages if m.rstrip().endswith("."))
    if _share(period, n) <= 0.2:
        lines.append(f"마침표: 거의 찍지 않음 ({n}건 중 {period}건)")
    elif _share(period, n) >= 0.7:
        lines.append(f"마침표: 문장 끝에 찍음 ({n}건 중 {period}건)")
    for label, count in (
        ("느낌표", sum(1 for m in messages if "!" in m)),
        ("물결(~)", sum(1 for m in messages if "~" in m)),
        ("말줄임(… 또는 ..)", sum(1 for m in messages if _ELLIPSIS.search(m))),
    ):
        if count >= MIN_REPEAT and _share(count, n) >= 0.2:
            lines.append(f"{label}: 자주 씀 ({n}건 중 {count}건)")

    # 웃음·울음·이모지
    for label, pattern in (("웃음 표시", _LAUGH), ("울음 표시", _CRY)):
        forms: Counter[str] = Counter()
        used = 0
        for m in messages:
            found = pattern.findall(m)
            if found:
                used += 1
                forms.update(found)
        if used >= MIN_REPEAT and _share(used, n) >= 0.15:
            shown = _quoted([f for f, _ in forms.most_common(2)])
            lines.append(f"{label}: {shown}을 붙임 ({n}건 중 {used}건)")
        elif used == 0 and n >= 8:
            lines.append(f"{label}: 쓰지 않음 ({n}건 중 0건)")
    emoji = sum(1 for m in messages if _EMOJI.search(m))
    if emoji >= MIN_REPEAT and _share(emoji, n) >= 0.15:
        lines.append(f"이모지: 씀 ({n}건 중 {emoji}건)")

    # 자음 줄임말·말머리·자주 쓰는 말
    slang: Counter[str] = Counter()
    openers: Counter[str] = Counter()
    habits: Counter[str] = Counter()
    for m in messages:
        tokens = set(_TOKEN.findall(m))
        slang.update(tokens & _CONSONANT_SLANG)
        habits.update(tokens & _HABIT_WORDS)
        first = _FIRST_TOKEN.match(m)
        if first and first.group(1) in _OPENERS:
            openers[first.group(1)] += 1
    if slang:
        lines.append("자음 줄임말: " + _quoted([s for s, _ in slang.most_common(3)]) + " 같은 표현을 씀")
    repeated_openers = [w for w, c in openers.most_common(3) if c >= MIN_REPEAT]
    if repeated_openers:
        lines.append("말머리: " + _quoted(repeated_openers) + "(으)로 시작하는 일이 많음")
    repeated_habits = [w for w, c in habits.most_common(TOP_WORDS) if c >= MIN_REPEAT]
    if repeated_habits:
        lines.append("자주 쓰는 말: " + _quoted(repeated_habits))

    if len(lines) <= 1:  # 길이밖에 말할 것이 없으면 지문이라고 하기 어렵다
        return None
    return "\n".join([f"(이 상대에게 보낸 내 메시지 {n}건에서 센 것)", *(f"- {line}" for line in lines)])
