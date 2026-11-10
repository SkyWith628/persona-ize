"""내 과거 메시지에서 문체 수치를 규칙으로 추정한다 (`GET /v1/contacts/{id}/persona/suggestion`).

모델을 부르지 않는다. 보는 것은 **글의 겉모습**뿐이다 — 성격·감정·관계의 질은 추정하지 않는다.
- 격식(formality): 메시지 끝 종결 어미의 비율 ('-습니다'체 / '-요'체 / 반말).
- 이모지(emoji): 이모지가 든 메시지의 비율.
- 친근함(friendliness): ㅋㅋ·ㅎㅎ·ㅠㅠ·물결·느낌표·이모지 같은 구어 표지가 든 메시지의 비율.
- 호칭(address_term): 메시지 첫머리에 반복해서 나오는 부르는 말 ('팀장님,' '유진아').
직접성·길이는 겉모습으로 믿을 만하게 재기 어려워 추정하지 않는다 (기준값 그대로).

결과는 제안일 뿐이다. 저장하지 않으며 사용자가 화면에서 확인하고 고쳐서 저장한다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.domain.persona import PersonaSettings

MAX_MESSAGES = 500  # 최근 것부터 이만큼만 본다
TAIL_CHARS = 60  # 종결 어미를 찾을 때 보는 마지막 줄의 끝부분 길이

_TRAILING = re.compile(r"[\s.!?~…,;:)(\]\[\"'^*♡♥ㅋㅎㅠㅜ\U0001F300-\U0001FAFF\u2600-\u27BF]+$")
_HANGUL = re.compile(r"[가-힣]")
_HAPSYO = re.compile(r"(?:니다|니까|십시오|시오)$")
_HAEYO = re.compile(r"(?:요|죠)$")
_POLITE_REPLIES = {"네", "넵", "넹", "예", "옙"}  # 한 낱말 대답도 존댓말로 센다
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF\u2600-\u27BF]")
_CASUAL_MARK = re.compile(r"ㅋ{2,}|ㅎ{2,}|ㅠ{2,}|ㅜ{2,}|\^\^|~|!")
_LEAD = re.compile(r"^\s*([가-힣A-Za-z]{1,10}(?: [가-힣A-Za-z]{1,10})?)\s*[,，]")
_FIRST_TOKEN = re.compile(r"^\s*([가-힣A-Za-z]{1,10})(?=[\s,!~.?]|$)")
_KINSHIP = {
    "형",
    "형님",
    "누나",
    "누님",
    "언니",
    "오빠",
    "엄마",
    "아빠",
    "어머니",
    "아버지",
    "선배",
    "선배님",
    "쌤",
    "선생님",
    "교수님",
    "자기야",
    "여보",
}


@dataclass(frozen=True)
class Signal:
    field: str
    value: Any
    reason: str


def _ending(text: str) -> str | None:
    """마지막 문장의 종결 유형: hapsyo('-습니다') / haeyo('-요') / banmal. 판단할 수 없으면 None."""
    last = next((ln for ln in reversed(text.splitlines()) if ln.strip()), "")
    core = _TRAILING.sub("", last[-TAIL_CHARS:])  # 끝부분만 본다 (긴 줄에서 정규식이 느려지지 않게)
    if not core or not _HANGUL.search(core[-3:]):
        return None
    if _HAPSYO.search(core) and not core.endswith("아니다"):
        return "hapsyo"
    if _HAEYO.search(core) or core in _POLITE_REPLIES:
        return "haeyo"
    return "banmal"


def _formality(texts: list[str]) -> Signal | None:
    kinds = Counter(k for k in (_ending(t) for t in texts) if k)
    total = sum(kinds.values())
    if total == 0:
        return None
    hapsyo, haeyo, banmal = kinds["hapsyo"], kinds["haeyo"], kinds["banmal"]
    polite = hapsyo + haeyo
    if hapsyo / total >= 0.9:
        return Signal("formality", 5, f"내 메시지 {total}개 중 {hapsyo}개가 '-습니다'체")
    if hapsyo / total >= 0.5:
        return Signal("formality", 4, f"내 메시지 {total}개 중 {hapsyo}개가 '-습니다'체")
    if polite / total >= 0.7:
        return Signal(
            "formality",
            3,
            f"내 메시지 {total}개 중 {polite}개가 존댓말 ('-요'체 {haeyo}개, '-습니다'체 {hapsyo}개)",
        )
    if polite / total >= 0.3:
        return Signal("formality", 2, f"존댓말과 반말이 섞여 있음 (존댓말 {polite}개, 반말 {banmal}개)")
    return Signal("formality", 1, f"내 메시지 {total}개 중 {banmal}개가 반말")


def _emoji(texts: list[str]) -> Signal:
    n = len(texts)
    with_emoji = sum(1 for t in texts if _EMOJI.search(t))
    share = with_emoji / n
    value = "often" if share >= 0.2 else "rare" if with_emoji and share >= 0.03 else "none"
    return Signal("emoji", value, f"내 메시지 {n}개 중 {with_emoji}개에 이모지")


def _friendliness(texts: list[str]) -> Signal:
    n = len(texts)
    marked = sum(1 for t in texts if _CASUAL_MARK.search(t) or _EMOJI.search(t))
    share = marked / n
    value = 5 if share >= 0.5 else 4 if share >= 0.25 else 3 if share >= 0.08 else 2
    return Signal("friendliness", value, f"내 메시지 {n}개 중 {marked}개에 ㅋㅋ·ㅠㅠ·물결·느낌표·이모지")


def _is_vocative(candidate: str, alias: str) -> bool:
    word = candidate.replace(" ", "")
    if len(word) < 2 and candidate not in _KINSHIP:
        return False
    if candidate in _KINSHIP or word in _KINSHIP:
        return True
    if word.endswith(("님", "씨")):
        return True
    given = alias[1:] if len(alias) == 3 and _HANGUL.fullmatch(alias[0]) else alias
    if word in (alias, given):
        return True
    # '유진아', '준호야' — 이름(별칭)에 든 말에 붙은 호격만 인정한다 ('좋아', '뭐야' 같은 말을 걸러 낸다)
    return word.endswith(("아", "야")) and len(word) >= 3 and word[:-1] in alias


def _address_term(texts: list[str], alias: str) -> Signal | None:
    counts: Counter[str] = Counter()
    for t in texts:
        m = _LEAD.match(t) or _FIRST_TOKEN.match(t)
        if m and _is_vocative(m.group(1).strip(), alias):
            counts[m.group(1).strip()] += 1
    if not counts:
        return None
    term, count = counts.most_common(1)[0]
    n = len(texts)
    if count < 2 or count / n < 0.1 or len(term) > 30:
        return None
    return Signal("address_term", term, f"내 메시지 {n}개 중 {count}개가 '{term}'(으)로 시작")


def suggest_persona(
    my_texts: list[str], base: PersonaSettings, contact_alias: str
) -> tuple[PersonaSettings | None, list[Signal]]:
    """내 메시지(시간 순)에서 추정한 설정과 근거. 메시지가 없으면 (None, []).

    `base`는 추정하지 않는 항목의 값이다 (저장된 페르소나가 있으면 그것, 없으면 관계 유형 기본값).
    """
    texts = [t for t in my_texts if t and t.strip()][-MAX_MESSAGES:]
    if not texts:
        return None, []
    signals = [
        s
        for s in (
            _formality(texts),
            _friendliness(texts),
            _emoji(texts),
            _address_term(texts, contact_alias.strip()),
        )
        if s is not None
    ]
    settings = base.model_copy(update={s.field: s.value for s in signals})
    return settings, signals
