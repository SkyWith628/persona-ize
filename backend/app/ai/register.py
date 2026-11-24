"""상대별 말투 관찰과 말투 프로필 (docs/graph-redesign-2026-11-16.md 3.1, 3.2).

사용자가 한 상대에게 보낸 메시지에서 **말투 단계**(합쇼체 / 해요체 / 반말)와 **호칭**을 규칙으로 읽고,
시간 순으로 구간을 나눠 "지금 값 / 이전 값 / 언제부터"를 계산한다.

지키는 것
- 순수 함수다. 입출력도 모델 호출도 없다. 같은 입력이면 입력 순서와 무관하게 같은 결과가 나온다.
- 글의 겉모습(종결 어미, 부르는 말)만 본다. 성격·감정·친밀도는 추정하지 않는다.
- 결과는 제안과 표시의 근거일 뿐이다. 여기서 읽은 호칭이 첨삭 문장에 들어가려면 사용자가 설정에
  저장해야 한다 (이 모듈은 저장하지 않는다).

`style_suggest._ending`을 쓰지 않는 이유: 그 함수는 마지막 줄의 끝 어미만 보고, '알 수 없음'이 없으며,
'-니다/-요/-죠'가 아닌 것을 모두 반말로 본다. 여기서는 문장마다 읽고, 서술어 어미가 없는 줄
("내일 10시 회의실 B", "확인 완료", "별일 아님", "넵")은 `unknown`으로 두어 집계에서 뺀다.

분류 규칙에서 정해 둔 것 (전부 판단이다. 손 라벨 문장으로 정확도를 재기 전까지는 가설이다)
- 합쇼체: -습니다/-ㅂ니다, -습니까/-ㅂ니까, -십시오/-ㅂ시오/-십쇼, 채팅 표기 -니당/-니닷.
  청유 '-ㅂ시다'("수요일로 갑시다")도 합쇼체로 센다 (문법서는 하오체로 보지만 직장 대화에서는
  '-습니다'와 같은 자리에서 쓰인다).
- 해요체: -요/-죠 ("-시죠?", "-세요", "-네요", "-ㄹ게요" 모두 여기). 채팅 표기 -용/-욤/-염/-여는
  바로 앞 글자가 어미일 때만 인정한다 ("했어용"은 해요체, "내용"·"비용"은 명사).
  명사에 붙은 '-요'("내일요", "저요")도 해요체다. '필요'·'중요'처럼 '요'로 끝나는 명사는 뺀다.
- 반말(해체): -어/-아/-해/-지/-네/-자/-니/-냐/-ㄹ게/-ㄹ래/-ㄹ까/-거든/-잖아/-는데 …, '한다/했다/좋다'
  같은 평서형 '-다'. '요'가 빠진 연결 어미로 **메시지가 끝난** 말("준비해야 돼서", "내일 하려고",
  "가고 싶은데")도 반말로 센다 (공손하게 말할 때는 '-요'를 붙여 "돼서요"라고 쓰기 때문이다).
  메시지 중간의 연결 어미("죄송한데... 내일 가능할까요?", "늦어서ㅠㅠ 죄송합니다")는 말이 아직 끝나지
  않은 것이라 `unknown`이다 — 말줄임·ㅠㅠ에서 조각이 나뉘었을 뿐, 말투는 뒤에 오는 종결 어미가 정한다.
- 알 수 없음: 서술어 어미가 없는 줄, 음슴체("별일 아님", "확인함"), 자모·이모지·URL만 있는 줄,
  한 낱말 대답(네/넵/예/응/아니요). '네'는 해요체에도 합쇼체에도 붙는 대답이라 단계를 알려 주지 않는다.
  (`style_suggest._ending`은 이런 대답을 존댓말로 센다 — 여기서는 세지 않는다.)
- 이름·명사에 붙은 '-야'("진짜야", "준호야")는 서술격인지 호격인지 겉으로 구분할 수 없어 `unknown`이다.

명사를 서술어로 잘못 읽지 않으려고 받침·모음 조건과 제외 낱말 목록을 쓴다. 목록은 완전하지 않다.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from app.domain.titles import ORG_TITLES, SENIOR_TITLES, is_senior_term, strip_honorific

Level = Literal["hapsyo", "haeyo", "hae", "unknown"]
MessageLabel = Literal["hapsyo", "haeyo", "hae", "mixed", "unknown"]

LEVEL_KO: dict[str, str] = {
    "hapsyo": "합쇼체",
    "haeyo": "해요체",
    "hae": "반말",
    "mixed": "혼용",
    "unknown": "알 수 없음",
}

# ── 임계값. 전부 판단이다 (근거 논문 없음, 설계 3.2). 바꾸면 K 민감도 표를 다시 낸다 ─────────────────
MIXED_THRESHOLD = 0.7
"""메시지 라벨: 분류된 문장 중 존댓말(합쇼체+해요체)도 반말도 이 비율에 못 미치면 '혼용'."""
SEGMENT_K = 2
"""새 말투 구간을 여는 데 필요한, 같은 단계로 일치하는 연속 메시지 수."""
ADDRESS_CONFIRM_COUNT = 2
"""호칭이 '확정'이 되는 연속 사용 횟수. 그보다 적으면 '후보'."""
TWO_VALUED_WINDOW = 6
"""두 값 상태를 볼 때 살피는, 마지막 변화 시점 이후의 최근 분류 메시지 수."""
TWO_VALUED_MIN_COUNT = 1
"""두 값 상태로 보려면 둘째 단계가 최근 창 안에 이만큼은 있어야 한다."""
RECENT_WINDOW = TWO_VALUED_WINDOW
"""현재 말투 구간이 '지금'의 것인지 볼 때 살피는 최근 메시지 수 (말투를 알 수 없는 메시지는 빼고 센다).
이 안에 현재 구간의 메시지가 하나도 없으면 구간을 확정으로 내보내지 않는다 (후보로 낮춘다)."""
MAX_TRAILING_CALLS = 3
"""문장 끝에서 떼어 보는 부르는 말("감사합니다 팀장님")의 최대 낱말 수."""
VOCATIVE_PARTICLE_COUNTS_AS_HAE = True
"""어미로는 알 수 없는 메시지에 이름+아/야 호격("서연아 오늘 국밥 ㄱ?")이 있으면 반말로 본다.
호격 조사 아/야는 말을 놓는 상대에게만 쓰기 때문이다. 어미로 분류된 메시지에는 적용하지 않는다."""

_LEVELS: tuple[str, ...] = ("hapsyo", "haeyo", "hae")

# ── 한글 음절 분해 ───────────────────────────────────────────────────────────────────────────────
_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = ["", *"ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"]


def _is_syllable(ch: str) -> bool:
    return len(ch) == 1 and "가" <= ch <= "힣"


def _cho(ch: str) -> str:
    return _CHO[(ord(ch) - 0xAC00) // 588] if _is_syllable(ch) else ""


def _jung(ch: str) -> str:
    return _JUNG[((ord(ch) - 0xAC00) // 28) % 21] if _is_syllable(ch) else ""


def _jong(ch: str) -> str:
    """받침. 없거나 한글 음절이 아니면 빈 문자열."""
    return _JONG[(ord(ch) - 0xAC00) % 28] if _is_syllable(ch) else ""


# ── 문장 나누기 ─────────────────────────────────────────────────────────────────────────────────
_EMOJI_CLASS = "\U0001f000-\U0001faff☀-➿"
_URL = re.compile(r"(?:https?://|www\.)\S+")
# 문장 경계: 마침표·물음표·느낌표·말줄임·물결, ㅋㅋ/ㅎㅎ/ㅠㅠ, 이모지 뒤의 공백. 채팅은 마침표 없이
# "알지ㅋㅋ 토요일 저녁 맞지?"처럼 이어 쓰므로 ㅋㅋ류도 경계로 본다. 쉼표는 경계가 아니다.
_BOUNDARY = re.compile(rf"(?:(?<=[.!?…~^])|(?<=[ㅋㅎㅠㅜ])|(?<=[{_EMOJI_CLASS}]))\s+")
# 어미를 읽기 전에 떼는 꼬리: 문장부호, ㅋㅋ/ㅎㅎ/ㅠㅠ, 물결, 이모지(변형 선택자·결합자 포함), 괄호·따옴표.
_TRAIL_CHARS: frozenset[str] = frozenset(".!?~…,;:)(][\"'`^*><=/_-♡♥ㅋㅎㅠㅜㅡ\ufe0f\u200d")
_LEADING = re.compile(r"^[\s\"'`(\[<*\-]+")


def _is_trailing(ch: str) -> bool:
    return ch in _TRAIL_CHARS or ch.isspace() or "\U0001f000" <= ch <= "\U0001faff" or "☀" <= ch <= "➿"


def _strip_trailing(text: str) -> str:
    """꼬리 글자를 뒤에서부터 떼어 낸다.

    `[...]+$` 정규식 치환을 쓰지 않는 이유: 꼬리 글자가 길게 이어진 뒤에 다른 글자가 하나 오면
    ("ㅋㅋㅋ…ㅋ가") 치환이 줄의 모든 자리에서 다시 시도해 제곱 시간이 든다. 뒤에서 한 번 훑으면 선형이다.
    """
    end = len(text)
    while end and _is_trailing(text[end - 1]):
        end -= 1
    return text[:end]


def split_sentences(text: str) -> list[str]:
    """메시지를 문장 단위로 나눈다. 줄바꿈, 문장부호·ㅋㅋ류·이모지 뒤의 공백이 경계다. 빈 조각은 버린다."""
    out: list[str] = []
    for line in text.splitlines():
        for piece in _BOUNDARY.split(line):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


# ── 문장 분류 ───────────────────────────────────────────────────────────────────────────────────
# 한 낱말 대답. 말투 단계를 알려 주지 않는다 (모듈 설명 참고).
_REPLIES: frozenset[str] = frozenset(
    {"네", "넵", "넹", "넴", "넷", "예", "옙", "네네", "넵넵", "예예", "응", "웅", "엉", "어", "응응"}
    | {"아니", "아뇨", "아니요", "아니오"}
)

_HAPSYO_SUFFIXES: tuple[str, ...] = ("니다만", "니다", "니까", "니당", "니닷", "시오", "시다")

# '요'로 끝나지만 종결 어미가 아닌 명사.
_YO_NOUN_SUFFIXES: tuple[str, ...] = ("필요", "중요", "개요", "수요", "동요", "소요", "주요", "강요", "민요")
# 채팅 표기 -용/-욤/-염/-여 앞에 올 수 있는 글자 (어미의 끝 글자). '내용'·'비용'·'참여'를 거른다.
_CHAT_YO_PREV = "어아해세네에예게래까죠"

# 받침 있는 용언 어간으로 볼 받침. ㄴ·ㄹ·ㅁ·ㅇ은 명사가 많아 뺀다 ('편지', '공지', '동네').
_STEM_JONG: frozenset[str] = frozenset("ㄱㄲㄳㄷㅂㅄㅅㅆㅈㅊㅌㅍㅎㄵㄶㅀㄺㄻㄼㄾㄿ")
# 모음 조화: '-아'는 ㅏ·ㅗ 뒤에, '-어'는 그 밖의 모음 뒤에 붙는다 ('좋아', '먹어'). '단어'·'유진아'를 거른다.
_YANG_VOWELS = "ㅏㅗㅑ"
# 받침 없는 어간에 '-아/-어'가 녹아든 글자의 모음 ('가서', '해서', '돼서', '와서', '사라').
_CONTRACTED_VOWELS = "ㅏㅓㅐㅕㅘㅝㅙ"
# 'ㅣ+어'가 줄어든 끝 글자의 첫소리 ('기다려', '가져', '마셔', '고쳐', '생겨', '잡혀', '느껴', '버텨').
_YEO_INITIALS = "ㄹㅈㅅㅊㅋㅍㄱㅎㄲㄷㅌ"
# 평서형 '-다' 앞에 오는 받침 없는 어간의 끝 글자 ('다행이다', '아니다', '크다', '예쁘다').
_DA_VOWEL_STEMS = "이니하크쁘프르되지리치시히기끼내"
_JA_VOWEL_STEMS = "하가보주되쓰오사나자타서두"

# '요'가 빠진 연결 어미·인용 어미. 말을 끝낸 것일 수도, 뒤 문장으로 이어지는 중일 수도 있다.
# 뒤에 종결 어미가 읽히는 문장이 없을 때만 반말로 센다 (`classify_sentence(final=...)`, `_sentence_counts`).
_CONNECTIVE_SUFFIXES: tuple[str, ...] = (
    *("다며", "다면서", "다니까", "니까"),
    *("려고", "다고", "냐고", "자고", "라고", "라구", "다구", "던데"),
)
# 두 글자 이상이라 명사와 헷갈릴 일이 적은 반말 어미·낱말 끝.
_HAE_SUFFIXES: tuple[str, ...] = (
    *("거든", "잖아", "더라", "더라고", "구나", "구만"),
    *("거야", "꺼야", "뭐야", "아니야", "누구야", "어디야", "언제야", "이야"),
    *("몰라", "달라", "빨라", "골라", "잘라", "올라", "놀라", "불러", "눌러", "서둘러", "흘러", "들러"),
    *("아파", "고파", "슬퍼", "바빠", "나빠", "기뻐", "예뻐", "이뻐"),
    *("보내", "끝내", "지내", "힘내", "화내", "꺼내", "혼내", "다녀"),
    *("보여", "먹여", "줄여", "붙여", "모여", "쌓여", "놓여", "어때", "지마"),
    *("나와", "어와", "아와", "러와", "녀와", "져와", "려와"),
    *("인가", "건가", "는가", "은가", "던가", "는군", "이군", "하군"),
)
_HAE_WORDS: frozenset[str] = frozenset(
    {"해", "돼", "봐", "줘", "써", "놔", "둬", "그래", "이래", "저래", "알지", "모르지", "그치", "그지"}
)
# 한 글자 동사의 반말 꼴. 조사 '와/가'는 앞말에 붙여 쓰므로, 띄어 쓴 낱말로 문장 끝에 오면 동사로 본다
# ("다음엔 꼭 와", "먼저 가", "잘 자"). 낱말 하나뿐인 줄("와!")에는 적용하지 않는다.
_BARE_VERBS: frozenset[str] = frozenset({"와", "가", "자"})
_JI_SUFFIXES: tuple[str, ...] = ("거지", "하지", "이지", "아니지", "되지", "시지", "르지", "알지", "말지")
_NE_SUFFIXES: tuple[str, ...] = (
    *("하네", "되네", "이네", "시네", "가네", "오네", "크네", "쁘네", "프네", "르네", "러네"),
    *("아니네", "지네", "치네", "주네", "보네", "나네", "사네", "기네"),
)
_NI_SUFFIXES: tuple[str, ...] = ("하니", "거니", "뭐니", "가니", "되니")

# 반말 어미처럼 끝나지만 서술어가 아닌 낱말. 낱말 전체가 같을 때만 거른다 ('이해'는 거르고 '이해해'는 둔다).
_NOT_PREDICATE: frozenset[str] = frozenset(
    {
        *("언어", "영어", "국어", "용어", "연어", "붕어", "잉어", "인어", "은어", "문어", "숭어"),
        *("검색어", "유행어", "표준어", "중국어", "금지어"),
        *("올해", "새해", "지난해", "다음해", "피해", "손해", "견해", "동해", "서해", "남해", "재해"),
        *("상해", "이해", "오해", "방해", "양해", "화해", "분해", "침해", "항해"),
        *("파워", "샤워", "타워"),
        *("쪽지", "잡지", "꼭지", "딱지", "습지", "낙지"),
        *("너네", "걔네", "얘네", "쟤네", "우리네", "동네", "그네"),
        *("과자", "피자", "의자", "혼자", "여자", "남자", "문자", "숫자", "글자", "기자", "모자"),
        *("상자", "부자", "감자", "제자", "투자", "환자", "타자", "그림자", "국자", "주전자"),
        *("한자", "원자", "이자", "사자", "저자", "독자", "액자", "탁자", "각자", "정자", "양자"),
        *("후자", "전자", "사용자", "담당자", "참석자", "관계자", "개발자", "책임자", "관리자"),
        *("판다", "바다", "수다", "케냐", "빨래"),
        *("군대", "반대", "안대", "연대", "현대", "근대"),
        *("고려", "염려", "우려", "배려", "격려", "장려", "독려", "오히려"),
        *("사서", "저서", "나라", "우리나라", "자라", "콜라"),
        *("가운데", "한가운데", "근데", "그런데", "한데"),
        "이야",
    }
)


def _core(text: str) -> str:
    """어미를 읽을 몸통: URL과 꼬리(문장부호·ㅋㅋ·이모지)를 떼고, 늘여 쓴 끝 글자를 줄인다."""
    core = _strip_trailing(_URL.sub(" ", text))
    core = _LEADING.sub("", core)
    # "좋아아", "했어어" → 끝 글자 하나만 남긴다. "요오"·"요옹"은 '요'로.
    # (끝에 닻을 둔 정규식 치환을 쓰지 않는다 — 꼬리 떼기와 같은 이유로 제곱 시간이 든다.)
    end = len(core)
    while end and core[end - 1] in "오옹":
        end -= 1
    if 0 < end < len(core) and core[end - 1] == "요":
        core = core[:end]
    end = len(core)
    while end >= 2 and core[end - 1] in "어아" and core[end - 2] == core[end - 1]:
        end -= 1
    return core[:end]


def _is_hapsyo(word: str) -> bool:
    if word.endswith("십쇼"):
        return True
    for suffix in _HAPSYO_SUFFIXES:
        # '-ㅂ니다'의 ㅂ 받침을 확인한다. '아니다', '다니다', '하니까'는 합쇼체가 아니다.
        if word.endswith(suffix) and len(word) > len(suffix) and _jong(word[-len(suffix) - 1]) == "ㅂ":
            return True
    return False


def _is_haeyo(word: str) -> bool:
    if word.endswith(_YO_NOUN_SUFFIXES):
        return False
    if word.endswith(("요", "죠")):
        return True
    return len(word) >= 2 and word[-1] in "용욤염여" and word[-2] in _CHAT_YO_PREV


def _is_hae(word: str, core: str, final: bool) -> bool:  # noqa: PLR0911, PLR0912 — 어미별 규칙을 한곳에 나열한다
    """반말 어미인가. `final`이 거짓이면(메시지 중간의 조각) 연결 어미는 반말로 세지 않는다."""
    if word in _NOT_PREDICATE:
        return False
    if word in _HAE_WORDS or word.endswith(_HAE_SUFFIXES) or core.endswith("지 마"):
        return True
    if word.endswith(_CONNECTIVE_SUFFIXES):
        return final
    if len(word) < 2:
        return False
    last, prev = word[-1], word[-2]
    pj, pv = _jong(prev), _jung(prev)
    if last == "다":  # 평서형 '-다': 한다, 했다, 좋다, 다행이다
        return not word.endswith(("보다", "에다")) and (bool(pj) or prev in _DA_VOWEL_STEMS)
    if last == "어":  # 했어, 있어, 먹어 (과거 'ㅆ' 뒤에는 모음과 상관없이 '-어')
        return pj in ("ㅆ", "ㅄ") or (bool(pj) and pv not in _YANG_VOWELS)
    if last == "아":  # 좋아, 같아, 괜찮아
        return bool(pj) and pv in _YANG_VOWELS
    if last in "해돼줘봐놔둬써워":  # 미안해, 안 돼, 알려줘, 해봐, 고마워
        return True
    if last == "지":  # 맞지, 했지, 좋지
        return pj in _STEM_JONG or word.endswith(_JI_SUFFIXES)
    if last == "네":  # 써야겠네, 좋네
        return pj in _STEM_JONG or word.endswith(_NE_SUFFIXES)
    if last == "자":  # 먹자, 하자
        return bool(pj) or prev in _JA_VOWEL_STEMS
    if last == "냐":
        return True
    if last == "니":  # 했니, 뭐하니
        return pj in _STEM_JONG or word.endswith(_NI_SUFFIXES)
    if last in "게께래까걸":  # 살게, 할께, 먹을래, 갈까, 할걸
        return pj == "ㄹ"
    if last == "데":  # 싶은데, 하는데, 건데 (연결 어미)
        return final and pj == "ㄴ"
    if last == "고":  # 했고 (연결 어미)
        return final and pj == "ㅆ"
    if last == "서":  # 돼서, 해서, 와서 (연결 어미)
        return final and not pj and pv in _CONTRACTED_VOWELS
    if last == "라":  # 사라, 해라, 먹어라
        return not pj and pv in _CONTRACTED_VOWELS
    if last == "대":  # 맛있었대, 간대
        return pj in ("ㄴ", "ㅆ", "ㅄ", "ㅎ")
    if last in "나군":  # 했나, 했군
        return pj in ("ㅆ", "ㅄ")
    # 'ㅣ+어'가 줄어든 꼴: 기다려, 가져, 마셔, 고쳐, 생겨
    return not _jong(last) and _jung(last) == "ㅕ" and _cho(last) in _YEO_INITIALS


def _plain_name(word: str) -> bool:
    """별칭에 없어도 '씨' 앞에서 사람 이름으로 볼 만한 꼴: 한글 2~3글자 (이름 두 글자, 성+이름 세 글자)."""
    return 2 <= len(word) <= 3 and all(_is_syllable(ch) for ch in word)


def classify_sentence(text: str, *, final: bool = True) -> Level:
    """문장 하나의 종결 어미로 말투 단계를 읽는다. 서술어 어미가 없으면 `unknown`.

    `final`은 이 문장이 말을 끝내는 자리인가다 (뒤에 종결 어미가 읽히는 문장이 없다). 거짓이면 '요'가 빠진
    연결 어미("죄송한데...", "늦어서ㅠㅠ")를 반말로 세지 않는다 — 뒤에 오는 문장이 말을 끝내기 때문이다.
    메시지 전체를 볼 때는 `message_label`이 문장마다 이 값을 정해 부른다.
    """
    core = _core(text)
    # 문장 끝에 붙은 부르는 말("수고했어요 민수씨", "감사합니다 팀장님")은 떼고 그 앞의 어미를 읽는다.
    # 재귀가 아니라 횟수를 정한 반복이다 — "가님 나님 다님 …"이 수천 낱말 이어진 줄에서 죽지 않게.
    for _ in range(MAX_TRAILING_CALLS + 1):
        if not core or not _is_syllable(core[-1]):
            return "unknown"  # 자모·영문·숫자·이모지로 끝난다: "ㅇㅋ", "국밥 ㄱ?", "회의실 B"
        if core.replace(" ", "").replace(",", "") in _REPLIES:
            return "unknown"
        words = core.rsplit(None, 1)
        word = _LEADING.sub("", words[-1])
        if not word:
            return "unknown"
        if _is_hapsyo(word):
            return "hapsyo"
        if _is_haeyo(word):
            return "haeyo"
        if _is_hae(word, core, final):
            return "hae"
        if len(words) < 2:
            return "unknown"
        if word in _BARE_VERBS:
            return "hae"
        if word == "씨":
            # 띄어 쓴 '이름 씨'("확인했습니다 영수 씨"): 이름까지 두 낱말을 떼고 그 앞의 어미를 읽는다.
            rest = words[0].rsplit(None, 1)
            if len(rest) < 2 or not _plain_name(rest[1]):
                return "unknown"
            core = _core(rest[0])
            continue
        if not (len(word) >= 2 and word.endswith(("님", "씨"))):
            return "unknown"
        core = _core(words[0])
    return "unknown"


def _sentence_counts(text: str) -> dict[str, int]:
    """문장별 단계 건수. '요'가 빠진 연결 어미는 그 뒤에 종결 어미가 읽히는 문장이 없을 때만 반말로 센다.

    "죄송한데... 내일 가능할까요?"의 '죄송한데'는 뒤 문장이 말을 끝내므로 세지 않는다(unknown).
    "내일 하려고 ㅋㅋ 걱정 ㄴㄴ"의 '하려고'는 뒤에 어미가 읽히는 문장이 없으므로 말을 끝낸 것으로 본다.
    """
    pieces = split_sentences(text)
    levels: list[str] = [classify_sentence(piece, final=False) for piece in pieces]
    closed = max((i for i, level in enumerate(levels) if level != "unknown"), default=-1)
    for i in range(closed + 1, len(pieces)):
        levels[i] = classify_sentence(pieces[i])
    counts: Counter[str] = Counter(levels)
    return {level: counts[level] for level in (*_LEVELS, "unknown")}


def _label_from_counts(counts: dict[str, int]) -> MessageLabel:
    """문장별 건수 → 메시지 라벨. 먼저 반말/존댓말을 가르고, 존댓말이면 그 안에서 단계를 고른다.

    합쇼체와 해요체가 섞인 메시지("확인했습니다. 내일 봬요!")는 혼용이 아니다 — 자연스러운 존댓말은 둘을
    섞어 쓴다. 이것을 혼용으로 빼면 존댓말로 바꾼 뒤의 메시지가 구간에서 통째로 빠져, 예전 반말 구간이
    '지금 말투'로 남는다. 혼용은 반말과 존댓말이 한 메시지에 섞였을 때만이다.
    존댓말 안에서는 문장이 많은 단계, 같으면 합쇼체다 (인사 "안녕하세요" 뒤에 합쇼체 본문이 오는
    메시지가 흔하다 — 판단).
    """
    polite_n = counts["hapsyo"] + counts["haeyo"]
    classified = polite_n + counts["hae"]
    if classified == 0:
        return "unknown"
    if polite_n / classified >= MIXED_THRESHOLD:
        return "hapsyo" if counts["hapsyo"] >= counts["haeyo"] else "haeyo"
    if counts["hae"] / classified >= MIXED_THRESHOLD:
        return "hae"
    return "mixed"


def message_label(text: str) -> MessageLabel:
    """메시지 라벨. 분류된 문장에서 존댓말도 반말도 70%에 못 미치면 mixed, 분류된 문장이 없으면 unknown.

    존댓말 쪽이면 합쇼체·해요체 가운데 문장이 많은 단계다 (같으면 합쇼체).
    """
    return _label_from_counts(_sentence_counts(text))


def polite(label: str) -> bool | None:
    """존댓말인가. 합쇼체·해요체 → True, 반말 → False, 혼용·알 수 없음 → None."""
    if label in ("hapsyo", "haeyo"):
        return True
    if label == "hae":
        return False
    return None


def formality_for(level: str, senior: bool) -> int | None:
    """말투 단계에 대응하는 격식 값 (설계 5.1): 반말 → 1, 해요체 → 3, 합쇼체 → 4. 그 밖은 None.

    `senior`는 지금은 값을 바꾸지 않는다 (계약상 고정 대응). "이미 더 격식 있는 설정은 낮추지 않는다"
    같은 규칙은 부르는 쪽이 적용한다.
    """
    del senior
    return {"hae": 1, "haeyo": 3, "hapsyo": 4}.get(level)


def level_of_formality(formality: int) -> str:
    """격식 값이 뜻하는 말투 단계: 1~2 → 반말, 3 → 해요체, 4~5 → 합쇼체. 범위 밖은 가까운 쪽으로 본다."""
    if formality <= 2:
        return "hae"
    if formality == 3:
        return "haeyo"
    return "hapsyo"


# ── 호칭 ────────────────────────────────────────────────────────────────────────────────────────
# 님·씨 없이도 부르는 말로 쓰이는 친족·관계어. 메시지 첫머리에 오면 쉼표가 없어도 호칭으로 본다.
_KINSHIP: frozenset[str] = frozenset(
    {
        *("형", "형님", "누나", "누님", "언니", "오빠", "엄마", "아빠", "어머니", "아버지"),
        *("어머님", "아버님", "할머니", "할아버지", "이모", "삼촌", "고모", "선배", "선배님"),
        *("쌤", "선생님", "교수님", "여보", "자기야"),
    }
)
_ADDRESS_TITLES: frozenset[str] = ORG_TITLES | SENIOR_TITLES
# 첫머리의 호칭 앞에 올 수 있는 말 ("네 팀장님, 맡겠습니다").
_INTERJECTIONS: frozenset[str] = _REPLIES | {"아", "앗", "헐", "저", "저기", "참", "근데", "그리고"}
# 낱말 뒤에 붙은 문장부호·ㅋㅋ류. `str.rstrip`으로 뗀다 (끝에 닻을 둔 정규식 치환은 긴 줄에서 제곱 시간).
_TERM_TRAIL_CHARS = ",，.!?~…ㅋㅎㅠㅜ^;:)\"'"
_SET_OFF = (",", "，", "!", "~")
# 앞 낱말이 이것으로 끝났으면 그 뒤는 새 문장의 첫머리다.
_SENTENCE_END_CHARS = ".!?…"
# 호칭처럼 보이는 말 둘 뒤에 이 말이 오면 부른 것이 아니라 나열이다 ("팀장님, 과장님 다 휴가래요").
_ENUMERATION_WORDS: frozenset[str] = frozenset({"모두", "다", "둘", "둘다", "전부", "전원"})
_TOKEN = re.compile(r"\S+")
_HANGUL_WORD = re.compile(r"[가-힣]+")


def _name_forms(contact_alias: str) -> frozenset[str]:
    """별칭에서 이름으로 쓰일 수 있는 꼴. '이서연' → {'이서연', '서연'}, '지현 팀장님' → {'지현'}."""
    names: set[str] = set()
    for token in contact_alias.replace(",", " ").split():
        base = strip_honorific(token)
        if not base or base in _ADDRESS_TITLES or token in _KINSHIP:
            continue
        names.add(base)
        if _HANGUL_WORD.fullmatch(base):
            if len(base) == 3:  # 성 한 글자 + 이름 두 글자
                names.add(base[1:])
            elif len(base) == 4:  # 두 글자 성 또는 이름 세 글자 — 끝 두 글자를 이름으로 본다
                names.add(base[2:])
    return frozenset(names)


def _is_name_vocative(word: str, names: frozenset[str]) -> bool:
    """이름 + 호격 조사: '서연아', '준호야'. 이름(별칭)에서 나온 말만 인정한다 ('좋아', '뭐야'를 거른다)."""
    for name in names:
        if _HANGUL_WORD.fullmatch(name) and word == name + ("아" if _jong(name[-1]) else "야"):
            return True
    return False


def _title_part(word: str, names: frozenset[str]) -> bool:
    """낱말이 직함·친족어이거나 (성 한 글자 또는 이름)+직함인가: '팀장님', '선배', '김팀장님', '서연언니'."""
    base = strip_honorific(word)
    if base in _ADDRESS_TITLES or word in _KINSHIP:
        return True
    for title in _ADDRESS_TITLES:
        if len(base) > len(title) and base.endswith(title):
            prefix = base[: -len(title)]
            if prefix in names or (len(prefix) == 1 and _is_syllable(prefix)):
                return True
    return False


def _free_standing(word: str) -> bool:
    """쉼표 없이 첫머리에만 와도 호칭으로 볼 만한 꼴: 님·씨가 붙었거나 친족어.

    '대리', '책임', '대표'처럼 님 없는 직함과 맨 이름은 일반 명사·제3자 언급과 헷갈려 쉼표가 있어야 한다.
    """
    return word in _KINSHIP or (len(word) >= 2 and word.endswith(("님", "씨")))


def _address_like(word: str, names: frozenset[str]) -> bool:
    """호칭 꼴의 낱말인가 (직함·친족어·별칭의 이름). 나열을 알아보는 데 쓴다."""
    return bool(word) and (_title_part(word, names) or word in names or strip_honorific(word) in names)


def _bare_title(word: str) -> bool:
    """님·씨가 붙지 않은 직함인가 ('이사', '책임', '김대리'). 친족어('형', '선배')는 여기 들지 않는다."""
    return word not in _KINSHIP and strip_honorific(word) == word


# '씨'로 끝나지만 사람을 부르는 말이 아닌 낱말 (끝이 같으면 거른다: '글솜씨', '손글씨').
_SSI_NOUN_SUFFIXES: tuple[str, ...] = (
    *("날씨", "솜씨", "글씨", "말씨", "맘씨", "마음씨", "불씨", "볍씨", "꽃씨", "풀씨", "홀씨"),
    *("아저씨", "아가씨", "섭씨", "화씨", "종씨", "맵씨"),
)
_SENTENCE_FINAL_TRAIL = frozenset(_SENTENCE_END_CHARS + "~")
_SSI_LOOKBACK = 6
"""문장 끝의 '이름 씨' 앞에서 어미를 읽으려고 돌아보는 낱말 수."""
_Token = tuple[int, str, str]  # (시작 위치, 낱말, 뒤따르는 문장부호)


def _ssi_name(tokens: Sequence[_Token], i: int, names: frozenset[str]) -> tuple[int, str, bool] | None:
    """i번째 낱말에서 끝나는 '이름 씨' / '이름씨' → (호칭이 시작하는 낱말 위치, 이름, 별칭의 이름인가).

    이름은 상대 별칭에서 나온 것이거나 한글 2~3글자다. '날씨'·'솜씨'·'아저씨' 같은 보통 낱말, 직함·친족어에
    붙은 '씨'는 여기서 다루지 않는다 (None).
    """
    word = tokens[i][1]
    if word == "씨":
        if i == 0 or tokens[i - 1][2]:  # 앞 낱말과 문장부호 없이 이어져야 한다
            return None
        head, name = i - 1, tokens[i - 1][1]
    elif len(word) >= 2 and word.endswith("씨"):
        head, name = i, word[:-1]
    else:
        return None
    if (name + "씨").endswith(_SSI_NOUN_SUFFIXES) or _title_part(name, names):
        return None
    known = name in names
    if not (known or _plain_name(name)):
        return None
    return head, name, known


def _ssi_enumerated(tokens: Sequence[_Token], i: int, names: frozenset[str]) -> bool:
    """i번째 낱말의 '이름 씨,' 뒤에 호칭 꼴이 또 이어지는 나열인가 ("영수 씨, 민수 씨 다 오셨어요")."""
    for j in (i + 1, i + 2):
        if j >= len(tokens):
            break
        nxt = _ssi_name(tokens, j, names)
        if (nxt is not None and nxt[0] == i + 1) or (j == i + 1 and _address_like(tokens[j][1], names)):
            if tokens[j][2].startswith((",", "，")):
                return True
            if j + 1 < len(tokens) and tokens[j + 1][1] in _ENUMERATION_WORDS:
                return True
    return False


def _closes_sentence(line: str, tokens: Sequence[_Token], head: int, i: int, first: int) -> bool:
    """head~i번째 낱말(호칭 꼴)이 문장 끝의 부르는 자리에 있는가.

    서술어 어미로 끝난 말 바로 뒤에 붙고("확인했습니다 영수 씨.", "알겠습니다 팀장님"), 거기서 문장이나
    줄이 끝난다. 뒤에 쉼표가 오면 문장 가운데다 ("죄송합니다 팀장님, 내일 드리겠습니다").
    """
    trail = tokens[i][2]
    if head <= first or trail.startswith((",", "，")):
        return False
    # 뒤에 ㅎㅎ·ㅠㅠ·이모지만 따로 온 것("잘 먹었습니다 선배님 ㅎㅎ")도 문장 끝이다 (낱말이 빈 조각).
    last = i == len(tokens) - 1 or not tokens[i + 1][1]
    if not last and not _SENTENCE_FINAL_TRAIL.intersection(trail):
        return False
    # 앞말의 어미는 끝의 몇 낱말만 보면 읽힌다 — 줄 전체를 넘기면 호칭이 수천 번 나오는 줄에서
    # 제곱 시간이 든다.
    before = line[tokens[max(0, head - _SSI_LOOKBACK)][0] : tokens[head][0]]
    return classify_sentence(before, final=False) != "unknown"


def _ssi_called(line: str, tokens: Sequence[_Token], i: int, first: int, names: frozenset[str]) -> str | None:
    """i번째 낱말에서 끝나는 '이름 씨'가 부르는 자리에 있으면 띄어 쓴 꼴('영수 씨'), 아니면 None.

    부르는 자리 (직함 호칭과 같은 기준에 문장 끝을 더했다)
    - 문장 첫머리 + 바로 뒤 쉼표·느낌표·물결: "영수 씨, 확인했습니다".
    - 줄의 처음, 문장부호 없이: 별칭의 이름일 때만 ("영수씨 내일 봬요"). 별칭에 없는 이름은 제3자 언급
      ("민수 씨 내일 온대요")과 겉으로 구분할 수 없다.
    - 문장 끝: 서술어 어미로 끝난 말 바로 뒤에 붙고, 거기서 문장(또는 줄)이 끝날 때
      ("확인했습니다 영수 씨.", "수고하셨어요 영수 씨. 내일 봬요").
    조사가 붙은 말('씨가', '씨한테')은 낱말이 '씨'로 끝나지 않으므로 애초에 여기 오지 않는다.
    """
    found = _ssi_name(tokens, i, names)
    if found is None:
        return None
    head, name, known = found
    trail = tokens[i][2]
    term = f"{name} 씨"
    at_start = head == first
    after_sentence = head > first and tokens[head - 1][2].endswith(tuple(_SENTENCE_END_CHARS))
    if at_start or after_sentence:
        if trail.startswith(_SET_OFF):
            return None if _ssi_enumerated(tokens, i, names) else term
        return term if at_start and known else None
    return term if _closes_sentence(line, tokens, head, i, first) else None


def address_terms(text: str, contact_alias: str) -> list[str]:  # noqa: PLR0912 — 조건을 한곳에 나열한다
    """메시지에서 상대를 부른 말을 나온 문자열 그대로 돌려준다 (나온 순서, 메시지 안 중복 제거).
    '이름 씨'만 띄어 쓴 꼴로 통일한다 ('영수씨' → '영수 씨').

    호격으로 쓰인 것만 센다.
    - 이름 + 씨 ('영수 씨', '영수씨'): 문장 첫머리(아래와 같은 기준)와 문장 끝 (`_ssi_called`).
      별칭에 없는 이름도 읽는다 (한글 2~3글자). '날씨'·'솜씨'·'아저씨'는 거른다.
    - 이름 + 아/야 ('서연아', '준호야'): 어디에 있든.
    - 직함·친족어·이름(+님/씨): **문장 첫머리**(줄의 처음, 대답·감탄사 뒤, 마침표·물음표·느낌표 뒤)에서만.
      바로 뒤에 쉼표·느낌표·물결이 있거나, 줄의 처음에 온 님·씨 붙은 말·친족어일 때.
      문장 가운데의 쉼표는 나열이나 이어 쓰기일 뿐이다 ("다음 주 이사, 잘 마무리할게").
    - 직함·친족어·님 붙은 이름은 **문장 끝**에서도 읽는다: 서술어 어미로 끝난 말 바로 뒤에 붙고 거기서
      문장이나 줄이 끝날 때 ("알겠습니다 팀장님", "감사합니다 지현 팀장님.", "고마워 형").
      맨 이름은 읽지 않는다.
    - 님 없는 직함('이사', '책임', '의사')은 보통 명사로도 쓰이므로 상대 별칭에 들어 있거나 별칭의 이름이
      앞에 붙었을 때만 호칭이다 ("의사, 간호사 모두 친절했습니다"는 호칭이 아니다).
    - 호칭 꼴이 연달아 나오는 나열("부장님, 차장님, 과장님 모두 참석하십니다")은 뺀다.
    - 조사가 붙은 말은 낱말 전체가 호칭 꼴과 같지 않으므로 빠진다 ('팀장님한테', '팀장님이랑', '서연이가').
    알려진 한계: 첫머리의 제3자 언급("팀장님 오늘 휴가래", "엄마 생신이 언제지")을 호칭으로 읽는다.
    문장 가운데의 호칭("죄송합니다 팀장님, 내일 드릴게요")은 읽지 않는다.
    """
    alias = contact_alias.strip()
    names = _name_forms(alias)
    alias_words = frozenset(alias.replace(",", " ").split())
    found: list[str] = []
    for line in text.splitlines():
        tokens: list[_Token] = []
        for m in _TOKEN.finditer(line):
            raw = m.group()
            lead = len(raw) - len(_LEADING.sub("", raw))
            word = raw[lead:].rstrip(_TERM_TRAIL_CHARS)
            tokens.append((m.start() + lead, word, raw[lead + len(word) :]))
        first = 0
        while first < len(tokens) and tokens[first][1] in _INTERJECTIONS:
            first += 1

        for i, (start, word, trail) in enumerate(tokens):
            if not word:
                continue
            if _is_name_vocative(word, names):
                found.append(word)
                continue
            if word.endswith("씨"):
                called = _ssi_called(line, tokens, i, first, names)
                if called is not None:
                    found.append(called)
                    continue
                if _ssi_name(tokens, i, names) is not None:
                    continue  # '이름 씨' 꼴이지만 부르는 자리가 아니다
            titled = _title_part(word, names)
            named = word in names or strip_honorific(word) in names
            if not (titled or named):
                continue
            head = i
            if titled and i > first:
                # '서연 파트장님', '박 팀장님': 앞 낱말이 이름이고 바로 이어지면 한 호칭이다.
                # 성 한 글자는 줄 첫머리에 있을 때만 붙인다 ("다음 주 이사"의 '주'는 성이 아니다).
                _, before, before_trail = tokens[i - 1]
                surname = i - 1 == first and len(before) == 1 and _is_syllable(before)
                if not before_trail and (before in names or surname):
                    head = i - 1
            at_start = head == first
            after_sentence = head > first and tokens[head - 1][2].endswith(tuple(_SENTENCE_END_CHARS))
            term = line[tokens[head][0] : start + len(word)]
            # 님 없는 직함: 이 상대의 것이라는 표시(별칭·이름)가 있어야 한다
            unmarked = (
                titled
                and not named
                and _bare_title(word)
                and word not in alias_words
                and not any(len(name) >= 2 and name in term for name in names)
            )
            if not (at_start or after_sentence):
                # 문장 끝: 직함·친족어, 님·씨 붙은 이름 ("알겠습니다 팀장님", "고마워 형").
                # 맨 이름은 읽지 않는다.
                called = (titled or word not in names) and not unmarked
                if called and _closes_sentence(line, tokens, head, i, first):
                    found.append(term)
                continue
            if trail.startswith(_SET_OFF):
                if (i + 1 < len(tokens) and _address_like(tokens[i + 1][1], names)) and (
                    tokens[i + 1][2].startswith((",", "，"))
                    or (i + 2 < len(tokens) and tokens[i + 2][1] in _ENUMERATION_WORDS)
                ):
                    continue  # 나열
                if unmarked:
                    continue
                found.append(term)
            elif at_start and _free_standing(word):
                found.append(term)
    return list(dict.fromkeys(found))


# ── 관찰과 프로필 ───────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Observation:
    """내 메시지 하나에서 읽은 것. `counts`는 문장별 단계 건수 (hapsyo/haeyo/hae/unknown)."""

    message_id: str
    sent_at: datetime
    label: MessageLabel
    counts: dict[str, int]
    address_terms: tuple[str, ...]


@dataclass(frozen=True)
class Segment:
    """같은 값이 이어진 구간. `end`는 다음 구간이 시작한 시각이고, 마지막 구간은 None이다.

    `count`·`message_ids`는 이 값으로 관찰된 메시지만 센다 (구간 안의 다른 값·알 수 없음은 빼고).
    `start`는 값이 바뀐 시각이다 — 존댓말로 바뀐 첫 메시지가 해요체이고 구간 값이 합쇼체면, 그 해요체
    메시지의 시각이 시작이고 `message_ids`에는 합쇼체 메시지만 들어간다.
    `status`: 마지막 구간은 confirmed 또는 candidate, 그 앞의 구간은 모두 previous.
    """

    value: str
    start: datetime
    end: datetime | None
    count: int
    message_ids: tuple[str, ...]
    status: Literal["confirmed", "candidate", "previous"]


@dataclass(frozen=True)
class RegisterProfile:
    """(나 → 상대)의 말투 프로필. 화면에는 `(나)-[:REGISTER]->(상대)` 간선처럼 보인다."""

    speech: tuple[Segment, ...]  # 시간 순 말투 구간 (value는 hapsyo/haeyo/hae)
    address: tuple[Segment, ...]  # 시간 순 호칭 구간 (value는 호칭 문자열)
    current_speech: Segment | None
    previous_speech: Segment | None  # 현재 구간 직전의 다른 값 구간
    current_address: Segment | None  # 확정된 호칭(2회 이상). 확정된 것이 없으면 마지막 후보(1회)
    previous_address: Segment | None
    two_valued: dict[str, int] | None  # 최근 6건에 두 단계가 섞여 있으면 {level: 건수}, 아니면 None
    senior_hint: bool  # 호칭의 직함으로 본 '상대가 윗사람'
    observations: int  # as_of 이전의 내 메시지 수
    unknown: int  # 그중 말투를 알 수 없는 메시지 수
    # 확정된 호칭 뒤에 한 번 나온 다른 호칭. 확정된 호칭을 밀어내지 않고 따로 둔다 — 제안이 "1회 관찰"로
    # 보여 줄 수는 있지만, 위계 힌트·상태 블록·경고처럼 사용자 확인 없이 쓰이는 값은 움직이지 않는다.
    candidate_address: Segment | None = None


def observe(message_id: str, sent_at: datetime, text: str, contact_alias: str) -> Observation:
    """메시지 하나의 말투 라벨과 호칭."""
    counts = _sentence_counts(text)
    label = _label_from_counts(counts)
    terms = tuple(address_terms(text, contact_alias))
    if label == "unknown" and VOCATIVE_PARTICLE_COUNTS_AS_HAE:
        names = _name_forms(contact_alias.strip())
        if any(_is_name_vocative(term, names) for term in terms):
            label = "hae"
    return Observation(message_id, sent_at, label, counts, terms)


def _aware(moment: datetime) -> datetime:
    """시간대가 없는 시각은 UTC로 본다 (시간대 있는 것과 없는 것을 비교하다 죽지 않게)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


_Item = tuple[str, datetime, str]  # (값, 보낸 시각, 메시지 id)
_Group = tuple[str, datetime, list[_Item]]  # (구간 값, 구간이 시작한 시각, 그 값으로 관찰된 메시지)


def _same_side(a: str, b: str) -> bool:
    """두 말투 단계가 반말/존댓말의 같은 쪽인가. 합쇼체와 해요체는 같은 쪽이다."""
    return (a == "hae") == (b == "hae")


def _segment(items: Sequence[_Item], k: int) -> list[_Group]:
    """시간 순 말투 관찰을 구간으로 나눈다. 현재 구간과 다른 말투가 k번 연속되면 새 구간이 열린다.

    연속은 **반말/존댓말의 같은 쪽**이면 이어진다. 반말을 쓰다가 "알겠습니다" → "알겠어요"로 넘어간 두 건은
    단계는 달라도 둘 다 존댓말이므로 존댓말 구간을 연다 (단계가 같아야만 열면, 합쇼체와 해요체를 섞어 쓰는
    사람은 영영 구간이 안 열려 예전 반말 구간이 '지금 말투'로 남는다). 새 구간의 값은 그 연속 안에서
    건수가 많은 단계(같으면 합쇼체)이고, 그 값으로 관찰된 메시지만 구간에 센다.
    현재 구간과 다른 값이 k번에 못 미치게 끼어든 것은 구간을 열지도 끊지도 않는다 (어느 구간에도 세지 않는다).
    """
    groups: list[_Group] = []
    run: list[_Item] = []
    for item in items:
        if groups and item[0] == groups[-1][0]:
            groups[-1][2].append(item)
            run = []
            continue
        if run and not _same_side(run[0][0], item[0]):
            run = []
        run.append(item)
        if len(run) >= k:
            tally = Counter(member[0] for member in run)
            value = max(_LEVELS, key=lambda level: tally[level])  # 같으면 _LEVELS 순서 (합쇼체 먼저)
            groups.append((value, run[0][1], [member for member in run if member[0] == value]))
            run = []
    return groups


def _to_segments(
    groups: Sequence[_Group], last_status: Literal["confirmed", "candidate"]
) -> tuple[Segment, ...]:
    out: list[Segment] = []
    for i, (value, start, members) in enumerate(groups):
        is_last = i == len(groups) - 1
        out.append(
            Segment(
                value=value,
                start=start,
                end=None if is_last else groups[i + 1][1],
                count=len(members),
                message_ids=tuple(m[2] for m in members),
                status=last_status if is_last else "previous",
            )
        )
    return tuple(out)


def _address_segments(items: Sequence[_Item]) -> tuple[tuple[Segment, ...], Segment | None]:
    """(호칭 구간, 확정된 호칭 뒤에 한 번 나온 다른 호칭).

    - 같은 호칭을 **2회** 쓰면 확정 구간이 열린다 (설계 3.2). 연달아 쓰지 않아도 된다 — '팀장님' →
      '지현 팀장님' → '팀장님'은 '팀장님' 2회다. 횟수는 현재 호칭을 마지막으로 쓴 뒤부터 센다 (현재 호칭으로
      돌아오면 그 사이의 다른 호칭은 한 번 끼어든 것으로 보고 버린다).
    - 확정된 호칭이 있으면 그것이 현재 호칭이다. 그 뒤에 한 번 나온 다른 호칭은 구간에 넣지 않고 둘째
      값으로 따로 돌려준다 — 한 번의 관찰(또는 오탐)이 확정된 호칭을 '이전'으로 밀어내지 않게.
    - 확정된 호칭이 하나도 없으면 나온 순서대로 묶어 마지막 것만 후보로 둔다 (c3: '서연아' → '서연 파트장님').
    """
    if not items:
        return (), None
    groups: list[_Group] = []
    pending: dict[str, list[_Item]] = {}
    for item in items:
        if groups and item[0] == groups[-1][0]:
            groups[-1][2].append(item)
            pending = {}
            continue
        seen = pending.setdefault(item[0], [])
        seen.append(item)
        if len(seen) >= ADDRESS_CONFIRM_COUNT:
            groups.append((item[0], seen[0][1], seen))
            pending = {}
    if not groups:
        runs: list[_Group] = []
        for item in items:
            if runs and runs[-1][0] == item[0]:
                runs[-1][2].append(item)
            else:
                runs.append((item[0], item[1], [item]))
        return _to_segments(runs, "candidate"), None
    last = items[-1]
    candidate: Segment | None = None
    if last[0] != groups[-1][0]:
        members = pending[last[0]]
        candidate = Segment(
            value=last[0],
            start=members[0][1],
            end=None,
            count=len(members),
            message_ids=tuple(m[2] for m in members),
            status="candidate",
        )
    return _to_segments(groups, "confirmed"), candidate


def build_profile(
    messages: Sequence[tuple[str, datetime, str]],
    contact_alias: str,
    *,
    as_of: datetime | None = None,
    k: int = SEGMENT_K,
) -> RegisterProfile:
    """이 상대에게 보낸 내 메시지 `(message_id, sent_at, text)`에서 말투 프로필을 계산한다.

    입력 순서는 상관없다 (보낸 시각, id 순으로 정렬한다). `as_of`가 있으면 그보다 **앞선** 메시지만 보고,
    구간을 나누기 전에 걸러낸다 — 그 시점에 알 수 있었던 것만으로 계산하기 위해서다.
    `k`는 새 말투 구간을 여는 연속 메시지 수다 (민감도 보고용, 기본 2).
    """
    k = max(1, k)
    cutoff = _aware(as_of) if as_of is not None else None
    seen: set[str] = set()
    rows: list[tuple[datetime, str, str]] = []
    for message_id, sent_at, text in sorted(messages, key=lambda m: (_aware(m[1]), m[0], m[2])):
        if message_id in seen or (cutoff is not None and _aware(sent_at) >= cutoff):
            continue
        seen.add(message_id)
        rows.append((sent_at, message_id, text))
    observed = [observe(message_id, sent_at, text, contact_alias) for sent_at, message_id, text in rows]

    # 말투: 한 단계로 분류된 메시지만 구간을 만든다. unknown·mixed는 구간을 열지도 끊지도 않는다.
    classified: list[_Item] = [(o.label, o.sent_at, o.message_id) for o in observed if o.label in _LEVELS]
    speech = _to_segments(_segment(classified, k), "confirmed")
    if speech:
        # 현재 구간이 정말 '지금'의 것인가. 그 뒤로 반말과 존댓말이 한 메시지에 섞인 것만 이어졌다면
        # 구간은 닫히지 않은 채 옛것이 된다 — 최근 메시지에 이 구간의 것이 하나도 없으면 확정으로 내보내지
        # 않는다 (상태 블록·경고·제안이 예전 말투를 '최근 n건'이라고 말하지 않게).
        known = [o.message_id for o in observed if o.label != "unknown"]
        recent_ids = set(known[-RECENT_WINDOW:])
        if not recent_ids.intersection(speech[-1].message_ids):
            speech = (*speech[:-1], replace(speech[-1], status="candidate"))
    current_speech = speech[-1] if speech else None
    previous_speech = speech[-2] if len(speech) >= 2 else None

    # 두 값 상태: 마지막 변화 시점 이후(구간이 없으면 전체)의 최근 분류 메시지에 두 단계가 섞여 있는가.
    since = _aware(current_speech.start) if current_speech else None
    recent = [item for item in classified if since is None or _aware(item[1]) >= since][-TWO_VALUED_WINDOW:]
    tally = Counter(item[0] for item in recent)
    present = {level: tally[level] for level in _LEVELS if tally[level] >= TWO_VALUED_MIN_COUNT}
    two_valued = present if len(present) >= 2 else None

    address, candidate_address = _address_segments(
        [(term, o.sent_at, o.message_id) for o in observed for term in o.address_terms]
    )
    current_address = address[-1] if address else None
    previous_address = address[-2] if len(address) >= 2 else None

    return RegisterProfile(
        speech=speech,
        address=address,
        current_speech=current_speech,
        previous_speech=previous_speech,
        current_address=current_address,
        previous_address=previous_address,
        two_valued=two_valued,
        senior_hint=current_address is not None and is_senior_term(current_address.value),
        observations=len(observed),
        unknown=sum(1 for o in observed if o.label == "unknown"),
        candidate_address=candidate_address,
    )


def changed_segment(profile: RegisterProfile) -> Segment | None:
    """말투가 **바뀐** 구간: 앞에 다른 구간이 있고 확정된 현재 구간. 없으면 None.

    그래프에서 계기 사건을 찾을 시점(이 구간의 시작)을 정하는 조건이다. 제안 카드
    (`services/suggestion_service.py`)와 그래프 화면의 말투 간선(`services/register_view.py`)이 이 함수
    하나를 쓴다 — 조건을 한쪽에만 고치면 두 화면이 같은 상대에 대해 다른 구간·다른 계기를 말하게 된다.

    확정이 아닌 현재 구간은 옛것이다(최근 메시지에 없음) — 그 시점의 계기를 찾아 붙이지 않는다.
    """
    current = profile.current_speech
    if current is None or profile.previous_speech is None or current.status != "confirmed":
        return None
    return current
