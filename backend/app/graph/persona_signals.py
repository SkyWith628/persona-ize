"""그래프의 관계 사실에서 페르소나 갱신 제안을 읽는다 (`/v1/contacts/{id}/persona/suggestion`의 그래프 근거).

왜 필요한가 (2026-11-16)
- D 조건에서 그래프 사실은 프롬프트에 들어가도 첨삭 문장을 거의 바꾸지 못했다: 프롬프트가 사실 맥락을
  내용에 쓰지 못하게 하고, 설정에 없는 호칭은 넣지 않기 때문이다. 문장을 바꾸는 것은 **설정**이다.
- 내 메시지의 겉모습으로 읽는 제안(`app.ai.style_suggest`)은 기간 전체를 한 덩어리로 본다. 동기가 상사가 되어
  반말에서 존댓말로 바뀐 대화는 "존댓말과 반말이 섞여 있음"이 된다. 그래프는 사실마다 시각이 있어
  **언제부터 바뀌었는지**를 말할 수 있다.

무엇을 읽는가
- 나 → 상대 방향의 Relationship 사실에 추출기가 채운 구조화 속성(`schema.Relationship`): `speech_style`
  (반말·존댓말·혼용), `address_term`, `hierarchy`. 사실 문장(fact)을 해석하지 않는다.
- 말투: 가장 최근에 확인된 말투와, 그 말투가 이어진 구간의 시작일. 존댓말이면 격식을 3 이상으로(상대가
  윗사람이면 4 이상), 반말이면 격식 3 이상을 1로 낮추자고 제안한다. '혼용'은 근거로 쓰지 않는다.
- 호칭: 가장 최근에 확인된 호칭과 그 호칭을 쓰기 시작한 날.
- 지금 설정과 같으면 말하지 않는다 (갱신 제안이므로). 지금 호칭이 더 구체적이면('지현 팀장님' ⊇ '팀장님')
  덮어쓰자고 하지 않는다.

믿을 만한가
- **무효화 표시(invalid_at)는 보지 않는다.** 로컬 그래프에서 무효화의 절반 가까이가 같은 대화 안에서 찍힌
  추출 부산물이었다 (`retriever.py` 설명). 시각 순서만 쓴다.
- **시각은 근거 메시지의 발화 시각(observed_at)이다. 사실의 valid_at이 아니다.** valid_at은 모델이 정한
  값이고, 로컬 그래프 105건 중 13건이 근거 메시지 시각보다 정확히 9시간 늦었다(한국 시간을 UTC로 적음,
  docs/knowledge-graph.md 10절). 그대로 쓰면 같은 날 사실의 순서가 뒤집히거나 "MM/DD부터"가 하루 밀린다.
  근거 에피소드의 시각은 수집 코드가 넣은 값이라 믿을 수 있다 — `trigger.py`·`retriever.py`의 관계 상태
  슬롯과 같은 기준이다. 저장소가 observed_at을 주지 못한 사실(근거 에피소드 없음)만 valid_at으로 물러난다.
- 속성은 모델이 추출한 값이라 틀릴 수 있고, 사실 한 건만으로도 제안한다(36건 규모에서는 호칭이 한 번만
  나온다). 그래서 **제안일 뿐**이다 — 저장하지 않으며, 근거에 날짜와 건수를 적어 사용자가 판단하게 한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from app.ai.style_suggest import Signal
from app.domain.conversation import local_time
from app.domain.persona import PersonaSettings
from app.graph.store import RelationshipFact

POLITE = "존댓말"
CASUAL = "반말"
TARGET_HIGHER = "target_higher"
FORMALITY_POLITE = 3  # '-요'체
FORMALITY_POLITE_TO_SENIOR = 4  # 윗사람에게 쓰는 존댓말
FORMALITY_CASUAL = 1
MAX_ADDRESS_TERM = 30  # PersonaSettings.address_term 길이 상한


def _day(moment: datetime) -> str:
    return local_time(moment).strftime("%m/%d")


def _when(fact: RelationshipFact) -> datetime | None:
    """이 사실이 확인된 시각: 근거 메시지 발화 시각, 없을 때만 모델이 적은 valid_at (모듈 설명 참고)."""
    return fact.observed_at if fact.observed_at is not None else fact.valid_at


def _current_run(
    facts: list[RelationshipFact], value_of: str
) -> tuple[RelationshipFact, RelationshipFact | None, int] | None:
    """(지금 값이 처음 확인된 사실, 그 직전의 다른 값 사실, 지금 값 구간의 사실 수). 값이 없으면 None.

    `facts`는 시각 순. 값이 빈 사실은 건너뛴다 — 호칭을 매번 부르지는 않는다.
    """
    having = [f for f in facts if getattr(f, value_of)]
    if not having:
        return None
    latest = getattr(having[-1], value_of)
    start, previous, count = having[-1], None, 0
    for f in reversed(having):
        if getattr(f, value_of) != latest:
            previous = f
            break
        start, count = f, count + 1
    return start, previous, count


def graph_persona_signals(
    facts: list[RelationshipFact], base: PersonaSettings, as_of: datetime | None = None
) -> list[Signal]:
    """나 → 상대 관계 사실에서 읽은 갱신 제안. `base`(지금 설정)와 같은 항목은 내지 않는다."""
    timed = [(when, f) for f in facts if (when := _when(f)) is not None and (as_of is None or when < as_of)]
    timed.sort(key=lambda pair: (pair[0], pair[1].uuid))
    timeline = [f for _, f in timed]
    at = {f.uuid: when for when, f in timed}  # 아래에서 "언제부터"를 적을 때 쓰는 시각
    signals: list[Signal] = []

    styled = [f for f in timeline if f.speech_style in (POLITE, CASUAL)]
    run = _current_run(styled, "speech_style")
    if run is not None:
        start, previous, count = run
        style = start.speech_style
        hierarchy = next((f.hierarchy for f in reversed(timeline) if f.hierarchy), None)
        senior = hierarchy == TARGET_HIGHER
        if style == POLITE:
            floor = FORMALITY_POLITE_TO_SENIOR if senior else FORMALITY_POLITE
            proposed = max(base.formality, floor)
        else:
            proposed = FORMALITY_CASUAL if base.formality >= FORMALITY_POLITE else base.formality
        if proposed != base.formality:
            reason = f"그래프: {_day(at[start.uuid])}부터 {style}"
            if previous is not None:
                reason += f" (그 전에는 {previous.speech_style})"
            if style == POLITE and senior:
                reason += ", 상대가 윗사람"
            reason += f" — 관계 사실 {count}건"
            signals.append(
                Signal("formality", proposed, reason, source="graph", since=local_time(at[start.uuid]).date())
            )

    named = [
        replace(f, address_term=f.address_term.strip())
        for f in timeline
        if f.address_term and 0 < len(f.address_term.strip()) <= MAX_ADDRESS_TERM
    ]
    run = _current_run(named, "address_term")
    if run is not None:
        start, previous, count = run
        term = start.address_term or ""
        current = (base.address_term or "").strip()
        # 지금 호칭이 같거나 더 구체적이면('지현 팀장님' ⊇ '팀장님') 건드리지 않는다
        if term and term not in current:
            reason = f"그래프: {_day(at[start.uuid])}부터 '{term}'(이)라고 부름"
            if previous is not None:
                reason += f" (그 전에는 '{previous.address_term}')"
            reason += f" — 관계 사실 {count}건"
            signals.append(
                Signal("address_term", term, reason, source="graph", since=local_time(at[start.uuid]).date())
            )
    return signals
