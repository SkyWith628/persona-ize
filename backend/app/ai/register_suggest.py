"""말투 프로필에서 설정 제안과 제안 카드를 만든다 (docs/graph-redesign-2026-11-16.md 5.1).

`app.ai.register.build_profile`이 내 메시지에서 계산한 프로필(말투 구간·호칭 구간)을 받아
- `register_signals`: 지금 설정과 다른 항목만 `Signal(source="register")`로,
- `build_card`: 화면에 보여 줄 제안 카드(이전 말투 → 지금 말투, 호칭, 물어볼지 여부)로 바꾼다.

순수 함수다. 모델을 부르지 않고 저장하지도 않는다. 그래프에서 찾는 계기·범위 단서는 서비스
(`app.services.suggestion_service`)가 카드에 붙인다 — 이 모듈은 그래프를 모른다.

지키는 것
- 말투 단계는 기존 격식 1~5에 대응시킨다 (반말 → 1, 해요체 → 3, 합쇼체 → 4). 새 설정 항목은 없다.
- 존댓말 쪽에서는 이미 더 격식 있는 설정을 낮추자고 하지 않는다 (해요체가 보여도 격식 4·5는 그대로).
  반말이 보이는데 설정이 존댓말이면 1로 낮추자고 한다 (`graph.persona_signals`와 같은 규칙).
- 두 단계가 섞여 있으면(`two_valued`) 말투 값을 정해 주지 않는다 — 카드가 묻는다.
- 호칭은 메시지에 나온 문자열 그대로다. 지금 호칭이 같거나 더 구체적이면 건드리지 않는다.
  한 번만 나온 호칭은 근거에 "1회 관찰"이라고 적는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.ai.register import LEVEL_KO, RegisterProfile, Segment, formality_for, level_of_formality
from app.ai.style_suggest import Signal
from app.domain.conversation import local_time
from app.domain.persona import PersonaSettings

MAX_ADDRESS_TERM = 30  # PersonaSettings.address_term 길이 상한
FEW_AFTER_CHANGE = 2  # 말투가 바뀐 뒤 관찰이 이 건수 이하면 값을 정해 주지 않고 묻는다 (설계 5.1, 판단)
CASUAL_LEVEL = "hae"


@dataclass
class SuggestionCard:
    """제안 카드 (설계 5.1). API가 그대로 내보낸다.

    `trigger`·`scope_hints`는 그래프에서 온다 — `build_card`는 비워 두고 서비스가 채운다.
    """

    contact_alias: str
    speech_previous: dict[str, Any] | None  # {"level": "hae", "count": 3, "message_ids": [...]}
    speech_current: dict[str, Any] | None  # 위 + "since": "2026-10-21"
    two_valued: dict[str, int] | None  # 최근 메시지에 두 단계가 섞여 있으면 {단계: 건수}
    address: dict[str, Any] | None  # {"term", "count", "since", "status", "message_ids"}
    trigger: dict[str, Any] | None = None  # 그래프: {"event", "at", "message_ids"}
    scope_hints: list[dict[str, Any]] = field(default_factory=list)  # 그래프: [{"text", "message_ids"}]
    ask: bool = False  # True면 값을 정해 주지 않고 사용자에게 묻는다
    ask_reason: str | None = None
    proposed: dict[str, Any] = field(default_factory=lambda: {"formality": None, "address_term": None})


def _day(moment: datetime) -> str:
    return local_time(moment).strftime("%m/%d")


def _iso_day(moment: datetime) -> str:
    return local_time(moment).date().isoformat()


def _level_ko(level: str) -> str:
    return LEVEL_KO.get(level, level)


def _proposed_formality(profile: RegisterProfile, base: PersonaSettings) -> int | None:
    """지금 말투 구간이 뜻하는 격식. 제안할 것이 없으면(설정과 같거나 근거가 없으면) None."""
    current = profile.current_speech
    if current is None or current.status != "confirmed" or profile.two_valued:
        # 구간이 없거나, 최근 메시지에 그 구간의 것이 없거나(옛 구간), 두 말투가 섞여 있다 — 정하지 않는다
        return None
    target = formality_for(current.value, profile.senior_hint)
    if target is None:
        return None
    if current.value == CASUAL_LEVEL:
        # 설정이 이미 반말 쪽(1~2)이면 그대로 둔다. 존댓말 설정일 때만 낮추자고 한다.
        proposed = base.formality if level_of_formality(base.formality) == CASUAL_LEVEL else target
    else:
        proposed = max(base.formality, target)  # 이미 더 격식 있는 설정은 낮추지 않는다
    return proposed if proposed != base.formality else None


def _proposed_address(profile: RegisterProfile, base: PersonaSettings) -> Segment | None:
    """제안할 호칭 구간. 지금 호칭이 같거나 더 구체적이면('지현 팀장님' ⊇ '팀장님') None."""
    # 확정된 호칭 뒤에 한 번 나온 다른 호칭이 있으면 그것이 가장 최근에 부른 말이다 ("1회 관찰"로 제안한다).
    current = profile.candidate_address or profile.current_address
    if current is None:
        return None
    term = current.value.strip()
    if not term or len(term) > MAX_ADDRESS_TERM or term in (base.address_term or ""):
        return None
    return current


def _address_before(profile: RegisterProfile, address: Segment) -> Segment | None:
    """제안하는 호칭 바로 전에 쓰던 호칭. 후보를 제안할 때는 확정된 현재 호칭이 '그 전'이다."""
    if profile.candidate_address is not None and address is profile.candidate_address:
        return profile.current_address
    return profile.previous_address


def register_signals(profile: RegisterProfile, base: PersonaSettings) -> list[Signal]:
    """말투 프로필에서 읽은 갱신 제안. `base`(지금 설정)와 같은 항목은 내지 않는다."""
    signals: list[Signal] = []

    formality = _proposed_formality(profile, base)
    current = profile.current_speech
    if formality is not None and current is not None:
        reason = f"내 메시지: {_day(current.start)}부터 {_level_ko(current.value)} {current.count}건"
        previous = profile.previous_speech
        if previous is not None:
            reason += f" (그 전에는 {_level_ko(previous.value)} {previous.count}건)"
        if profile.senior_hint and current.value != CASUAL_LEVEL:
            reason += ", 호칭으로 보아 상대가 윗사람"
        signals.append(
            Signal("formality", formality, reason, source="register", since=local_time(current.start).date())
        )

    address = _proposed_address(profile, base)
    if address is not None:
        term = address.value.strip()
        seen = "1회 관찰" if address.status == "candidate" else f"{address.count}회"
        reason = f"내 메시지: {_day(address.start)}부터 '{term}'(이)라고 부름 — {seen}"
        before = _address_before(profile, address)
        if before is not None:
            reason += f" (그 전에는 '{before.value}')"
        signals.append(
            Signal("address_term", term, reason, source="register", since=local_time(address.start).date())
        )
    return signals


def _speech_dict(segment: Segment, *, since: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "level": segment.value,
        "count": segment.count,
        "message_ids": list(segment.message_ids),
    }
    if since:
        out["since"] = _iso_day(segment.start)
    return out


def _counts_ko(counts: dict[str, int]) -> str:
    return " / ".join(f"{_level_ko(level)} {n}건" for level, n in counts.items())


def build_card(profile: RegisterProfile, base: PersonaSettings, contact_alias: str) -> SuggestionCard:
    """프로필을 제안 카드로. 그래프 항목(`trigger`, `scope_hints`)은 비워 둔다.

    `ask`는 값을 정해 주지 않고 물어야 하는 경우다: 두 말투가 섞여 있거나, 말투가 바뀐 뒤 관찰이
    `FEW_AFTER_CHANGE`건 이하일 때. 범위 단서가 있을 때도 묻는데, 그것은 그래프를 본 서비스가
    `with_graph_evidence`로 더한다.
    """
    current, previous = profile.current_speech, profile.previous_speech
    if current is not None and current.status != "confirmed":
        # 최근 메시지에 이 구간의 것이 하나도 없다 — '지금 말투'로 보여 주지 않는다
        current, previous = None, None
    address = _proposed_address(profile, base)
    ask_reason: str | None = None
    if profile.two_valued:
        ask_reason = f"최근 메시지에 두 말투가 섞여 있습니다 ({_counts_ko(profile.two_valued)})."
    elif current is not None and previous is not None and current.count <= FEW_AFTER_CHANGE:
        ask_reason = f"말투가 바뀐 뒤의 메시지가 {current.count}건뿐입니다."
    return SuggestionCard(
        contact_alias=contact_alias,
        speech_previous=_speech_dict(previous, since=False) if previous is not None else None,
        speech_current=_speech_dict(current, since=True) if current is not None else None,
        two_valued=dict(profile.two_valued) if profile.two_valued else None,
        address=(
            {
                "term": address.value.strip(),
                "count": address.count,
                "since": _iso_day(address.start),
                "status": address.status,
                "message_ids": list(address.message_ids),
            }
            if address is not None
            else None
        ),
        ask=ask_reason is not None,
        ask_reason=ask_reason,
        proposed={
            "formality": _proposed_formality(profile, base),
            "address_term": address.value.strip() if address is not None else None,
        },
    )


SCOPE_ASK_REASON = "특정 자리에서만 쓰는 말투일 수 있습니다."


def with_graph_evidence(
    card: SuggestionCard, trigger: dict[str, Any] | None, scope_hints: list[dict[str, Any]]
) -> SuggestionCard:
    """그래프에서 찾은 계기와 범위 단서를 카드에 붙인다. 범위 단서가 있으면 묻는 카드가 된다.

    두 말투가 섞여 있다는 이유가 이미 있으면 그 이유를 남긴다 (더 직접적인 근거다).
    """
    card.trigger = trigger
    card.scope_hints = list(scope_hints)
    if scope_hints:
        card.ask = True
        if card.two_valued is None:
            card.ask_reason = SCOPE_ASK_REASON
    return card


def has_content(card: SuggestionCard) -> bool:
    """보여 줄 것이 있는 카드인가 — 제안할 값이 있거나, 두 말투가 섞여 있어 물어야 하는 경우."""
    return (
        card.proposed.get("formality") is not None
        or card.proposed.get("address_term") is not None
        or card.two_valued is not None
    )
