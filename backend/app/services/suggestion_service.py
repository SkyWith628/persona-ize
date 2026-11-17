"""상대별 말투 설정 제안 (`GET /v1/contacts/{id}/persona/suggestion`, docs/graph-redesign-2026-11-16.md 5.1).

근거를 내는 곳이 셋이고, `sources`로 고른다 (평가가 출처별 정확도를 따로 재려고 나눠 부른다).
- `messages`: 내 메시지 전체를 한 덩어리로 본 겉모습 추정 (`ai.style_suggest`). 변화 시점을 모른다.
- `register`: 문장 단위 말투 분류로 만든 프로필 (`ai.register`) — "10/21부터 합쇼체 2건, 그 전에는 반말 3건".
- `graph`: 그래프 관계 사실의 추출 속성 (`graph.persona_signals`).
응답의 근거 순서는 messages → register → graph이고, 같은 항목이면 뒤에 온 근거가 제안 설정에 반영된다.

제안 카드(`SuggestionCard`)는 말투 프로필이 만들고, 그래프가 켜져 있으면 변화 시점 앞뒤의 계기 사건과
범위 단서를 붙인다 (`graph.trigger`). 카드가 `ask`이면 말투 프로필의 값은 제안 설정에 넣지 않는다 —
값을 정해 주지 않고 사용자에게 묻는다. 근거 목록에는 그대로 남는다.

지키는 것: 모델을 부르지 않는다. 아무것도 저장하지 않는다(승인은 `PUT /persona`). 그래프나 프로필 계산이
실패하면 그 근거 없이 나머지만 돌려준다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app.ai.register import SEGMENT_K, RegisterProfile, build_profile, changed_segment
from app.ai.register_source import own_messages as _own_messages
from app.ai.register_source import recent_before
from app.ai.register_suggest import (
    SuggestionCard,
    build_card,
    has_content,
    register_signals,
    with_graph_evidence,
)
from app.ai.style_suggest import MAX_MESSAGES, Signal, suggest_persona
from app.domain.contact import Contact
from app.domain.conversation import Conversation, exception_kind, local_time
from app.domain.persona import PersonaSettings, default_persona
from app.graph.episodes import DEFAULT_ME_NAME
from app.graph.persona_signals import graph_persona_signals
from app.graph.trigger import find_trigger

if TYPE_CHECKING:
    from app.api.deps import AppState

__all__ = ["ALL_SOURCES", "Suggestion", "SuggestionCard", "own_messages", "suggest_persona_for_contact"]

log = logging.getLogger(__name__)

ALL_SOURCES: frozenset[str] = frozenset({"messages", "register", "graph"})
# 말투 프로필로 제안하는 데 필요한 내 메시지 수의 기본값. 문턱이 없다 (설계 3.2: "확정과 표시 가능의 기준은
# 같다", 건수 문턱을 두지 않고 건수를 항상 표시한다). 근거가 얇다는 것은 구간 규칙(연속 2건)과 카드의
# 건수 표시("2건", "1회 관찰")가 드러낸다. 총 건수 문턱을 두면 반말 2건 → 합쇼체 2건처럼 구간 규칙은
# 충족하지만 메시지가 적은 상대의 변화를 통째로 놓친다. 인자는 평가에서 문턱의 영향을 볼 때만 올린다.
REGISTER_MIN_MESSAGES = 0
# 카드가 물을 때 제안 설정에 넣지 않는 항목 (말투 프로필이 정하는 값)
ASKED_FIELDS = ("formality", "address_term")


@dataclass
class Suggestion:
    based_on_messages: int  # 본 내 메시지 수 (최근 `MAX_MESSAGES`건까지)
    settings: PersonaSettings | None  # 근거가 하나도 없으면 None
    signals: list[Signal]
    card: SuggestionCard | None


async def _own(
    state: AppState, user_id: str, contact: Contact
) -> tuple[list[Conversation], list[tuple[str, datetime, str]]]:
    convs = await state.conversations.list(user_id, contact.id)
    return convs, await _own_messages(state.conversations, user_id, contact.id, convs=convs)


async def own_messages(state: AppState, user_id: str, contact: Contact) -> list[tuple[str, datetime, str]]:
    """이 상대의 모든 대화방에서 내가 보낸 메시지: (메시지 id, 보낸 시각, 본문). 순서는 정해져 있지 않다.

    메시지 id는 그래프 에피소드 이름과 같은 `<대화방 source_key>:<메시지 source_key>`(`c3:m08`)다.
    """
    return (await _own(state, user_id, contact))[1]


async def _center_uuid(state: AppState, user_id: str, contact: Contact) -> str | None:
    """그래프의 상대 노드. 그래프가 없거나 못 찾거나 조회가 실패하면 None."""
    if state.graph_store is None:
        return None
    try:
        return await state.graph_store.find_entity_uuid(user_id, contact.name_in_graph)
    except Exception as exc:  # noqa: BLE001 - 제안은 부가 기능이다. 그래프 장애로 화면을 막지 않는다
        log.warning("graph persona signals failed, continuing without them: %s", exception_kind(exc))
        return None


async def _graph_signals(
    state: AppState,
    user_id: str,
    center: str | None,
    me_names: list[str],
    base: PersonaSettings,
    as_of: datetime | None,
) -> list[Signal]:
    """그래프의 나 → 상대 관계 사실에서 읽은 갱신 제안. 그래프가 없거나 노드를 못 찾으면 빈 목록."""
    if state.graph_store is None or center is None:
        return []
    try:
        me = None
        for name in dict.fromkeys(me_names):  # 대화방마다 내 표시 이름이 다를 수 있다 — 처음 찾은 노드
            me = await state.graph_store.find_entity_uuid(user_id, name)
            if me is not None:
                break
        if me is None or me == center:
            return []
        facts = await state.graph_store.relationship_facts(user_id, me, center)
    except Exception as exc:  # noqa: BLE001 - 제안은 부가 기능이다. 그래프 장애로 화면을 막지 않는다
        log.warning("graph persona signals failed, continuing without them: %s", exception_kind(exc))
        return []
    return graph_persona_signals(facts, base, as_of)


def _profile(
    mine: list[tuple[str, datetime, str]], alias: str, as_of: datetime | None, k: int = SEGMENT_K
) -> RegisterProfile | None:
    """말투 프로필. 계산이 실패하면 None — 제안은 프로필 없이 예전처럼 나간다."""
    try:
        return build_profile(mine, alias, as_of=as_of, k=k)
    except Exception as exc:  # noqa: BLE001 - 규칙 계산의 버그로 제안 화면 전체를 막지 않는다
        log.warning("register profile failed, continuing without it: %s", exception_kind(exc))
        return None


async def suggest_persona_for_contact(
    state: AppState,
    user_id: str,
    contact: Contact,
    *,
    as_of: datetime | None = None,
    sources: frozenset[str] = ALL_SOURCES,
    base: PersonaSettings | None = None,
    register_min_messages: int = REGISTER_MIN_MESSAGES,
    k: int = SEGMENT_K,
) -> Suggestion:
    """이 상대에게 쓸 말투 설정 제안과 근거, 제안 카드.

    - `as_of`: 평가용. 이 시각 **이전**의 메시지·그래프 사실만 본다.
    - `sources`: 근거를 낼 곳 (모듈 설명). 카드는 `register`가 있을 때만 만든다. 카드의 계기·범위 단서는
      `graph`를 빼도 그래프가 켜져 있고 말투가 바뀐 시점이 있으면 찾는다 (근거를 보여 줄 뿐이다).
    - `base`: 추정하지 않는 항목의 값이자 "지금 설정". 없으면 저장된 페르소나, 그것도 없으면 관계 유형 기본값.
    - `register_min_messages`: 내 메시지가 이보다 적으면 말투 프로필로는 제안하지 않는다. 기본 0(문턱 없음,
      구간 규칙 K만 남는다). 평가에서 총 건수 문턱의 영향을 볼 때만 올린다.
    - `k`: 새 말투 구간을 여는 연속 메시지 수 (`register.build_profile`). 평가의 K=1/2/3 민감도 보고용이다.

    말투 프로필과 그래프 근거는 둘 다 **메시지 겉모습 추정까지 반영한 설정**과 비교한다 — 겉모습 추정이 이미
    말한 값을 다른 출처 이름으로 되풀이하지 않는다.
    """
    convs, mine = await _own(state, user_id, contact)
    recent = recent_before(mine, as_of, MAX_MESSAGES)  # as_of 이전, 최근 것부터 MAX_MESSAGES건만 본다
    texts = [text for _, _, text in recent]
    if base is None:
        saved = await state.contacts.latest_persona(user_id, contact.id)
        base = saved.settings if saved else default_persona(contact.relationship_type)

    settings: PersonaSettings | None = None
    signals: list[Signal] = []
    if "messages" in sources:
        # 메시지 500건에 정규식을 여러 번 돌린다 — 이벤트 루프 밖에서 계산한다.
        settings, signals = await asyncio.to_thread(suggest_persona, texts, base, contact.alias)
    working = settings or base

    center = await _center_uuid(state, user_id, contact)

    card: SuggestionCard | None = None
    from_register: list[Signal] = []
    if "register" in sources and len(recent) >= register_min_messages:
        profile = await asyncio.to_thread(_profile, recent, contact.alias, as_of, k)
        if profile is not None:
            from_register = register_signals(profile, working)
            card = build_card(profile, working, contact.alias)
            # 계기를 찾을 구간 — 그래프 화면의 말투 간선(`register_view`)과 같은 함수를 쓴다
            changed = changed_segment(profile)
            if changed is not None and state.graph_store is not None and center is not None:
                trigger, hints = await find_trigger(
                    state.graph_store, user_id, center, changed.start, as_of=as_of
                )
                card = with_graph_evidence(
                    card,
                    {
                        "event": trigger.event,
                        "at": local_time(trigger.at).date().isoformat() if trigger.at else None,
                        "message_ids": list(trigger.message_ids),
                    }
                    if trigger is not None
                    else None,
                    [{"text": h.text, "message_ids": list(h.message_ids)} for h in hints],
                )
            if not has_content(card):
                card = None

    from_graph: list[Signal] = []
    if "graph" in sources:
        me_names = [conv.me_name or DEFAULT_ME_NAME for conv in convs] or [DEFAULT_ME_NAME]
        from_graph = await _graph_signals(state, user_id, center, me_names, working, as_of)

    # 뒤에 온 근거가 이긴다. 카드가 물을 때는 말투 프로필의 값을 넣지 않는다 (근거 목록에는 남는다).
    # 그래프 근거(`persona_signals`)는 카드가 물어도 예전처럼 반영한다 — 그래프를 켠 사용자의 응답이
    # 말투 프로필 도입 전과 달라지지 않게.
    asking = card is not None and card.ask
    if asking and settings is not None:
        # 묻는 항목은 겉모습 추정의 값도 넣지 않고 지금 설정(base)으로 둔다. 겉모습 추정은 기간 전체를 한
        # 덩어리로 봐서, 반말에서 합쇼체로 바뀐 상대를 "섞여 있음 → 격식 2"로 읽는다 — 묻는 카드(제안 격식 4)
        # 옆에 반대쪽 값을 설정으로 내밀게 된다 (2026-11-16 합성 c3 확인). 근거 목록에는 그대로 남는다.
        # 말투 프로필이 겉모습 추정과 **다른 값**을 말한 항목만이다 — 둘이 같은 말을 하는 항목은 그대로 둔다.
        disputed = {s.field for s in from_register if s.field in ASKED_FIELDS}
        settings = settings.model_copy(update={f: getattr(base, f) for f in disputed})
        working = settings
    updates = {s.field: s.value for s in from_register if not (asking and s.field in ASKED_FIELDS)}
    updates.update({s.field: s.value for s in from_graph})
    if updates:
        settings = working.model_copy(update=updates)
    return Suggestion(
        based_on_messages=len(texts),
        settings=settings,
        signals=[*signals, *from_register, *from_graph],
        card=card,
    )
