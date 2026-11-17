"""상대 주변 그래프 화면에 보여 줄 "말투 간선" (docs/graph-redesign-2026-11-16.md 3.2).

설계는 말투 프로필을 `(나)-[:REGISTER]->(상대)` 간선처럼 **보여 주기만** 한다. Neo4j에는 아무것도 쓰지
않는다 — 프로필은 PostgreSQL 메시지에서 읽을 때 규칙으로 계산하는 값이라(`ai.register.build_profile`),
저장해 두면 메시지가 지워지거나 화자가 고쳐졌을 때 어긋난다.

제안(`services/suggestion_service.py`)과 **같은 입력·같은 규칙**을 쓴다: 내 메시지는
`ai.register_source.own_messages`, 상한은 `recent_before`의 기본값(최근 500건), 계기 사건은
`graph.trigger.find_trigger`를 "확정된 현재 구간이고 그 앞에 다른 구간이 있을 때"만 — 이 조건은 두 곳이
`ai.register.changed_segment` 하나를 부른다. 그래야 같은 상대에 대해 제안 카드와 이 화면이 다른 구간·다른
계기를 말하지 않는다.

지키는 것: 모델을 부르지 않는다. 아무것도 저장하지 않는다. 그래프가 꺼져 있어도 구간은 나온다(계기만
없다). 계기 조회가 실패하면 계기 없이 구간만 돌려준다(`find_trigger`가 예외를 올리지 않는다).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from app.ai.register import Segment, build_profile, changed_segment
from app.ai.register_source import own_messages, recent_before
from app.domain.contact import Contact
from app.domain.conversation import local_time
from app.graph.store import GraphNeighborhood
from app.graph.trigger import find_trigger

if TYPE_CHECKING:
    from app.api.deps import AppState

MAX_SEGMENT_MESSAGE_IDS = 5  # 구간마다 보여 줄 근거 메시지 수 (그 구간의 최근 것부터)


@dataclass(frozen=True)
class RegisterTriggerView:
    event: str  # 그래프의 사건 이름 (예: '파트장 발령')
    at: date | None  # 그 사건을 처음 말한 메시지의 날짜 (한국 시간)


@dataclass(frozen=True)
class RegisterSegmentView:
    level: str  # hapsyo / haeyo / hae (`ai.register`의 값 그대로)
    start: date  # 한국 시간
    end: date | None  # 다음 구간이 시작한 날. 현재 구간이면 None
    count: int
    status: str  # 마지막 구간은 confirmed 또는 candidate, 그 앞은 previous
    message_ids: tuple[str, ...]
    trigger: RegisterTriggerView | None = None


@dataclass(frozen=True)
class RegisterAddressView:
    term: str
    count: int
    status: str  # confirmed(2회 이상) / candidate(1회)


@dataclass(frozen=True)
class RegisterView:
    segments: tuple[RegisterSegmentView, ...]  # 시간 순
    address: RegisterAddressView | None
    based_on_messages: int


def _day(moment: datetime) -> date:
    return local_time(moment).date()


def _segment_view(seg: Segment, trigger: RegisterTriggerView | None = None) -> RegisterSegmentView:
    return RegisterSegmentView(
        level=seg.value,
        start=_day(seg.start),
        end=_day(seg.end) if seg.end is not None else None,
        count=seg.count,
        status=seg.status,
        message_ids=tuple(seg.message_ids[-MAX_SEGMENT_MESSAGE_IDS:]),
        trigger=trigger,
    )


async def register_view(
    state: AppState,
    user_id: str,
    contact: Contact,
    center: str | None,
    hood: GraphNeighborhood | None = None,
) -> RegisterView | None:
    """이 상대에게 쓰는 내 말투의 구간과 현재 호칭. 내 메시지가 없으면 None.

    `center`는 그래프의 상대 노드(없거나 그래프가 꺼져 있으면 None) — 있을 때만 계기 사건을 찾는다.
    `hood`는 호출 쪽이 그 노드로 이미 읽은 이웃이다. 주면 계기 조회가 이웃을 다시 읽지 않는다.
    프로필 계산의 예외는 그대로 올린다: 호출 쪽(그래프 조회)이 잡아 말투 없이 예전 응답을 돌려준다.
    """
    mine = recent_before(await own_messages(state.conversations, user_id, contact.id), None)
    if not mine:
        return None
    # 메시지 수백 건에 정규식을 여러 번 돌린다 — 이벤트 루프 밖에서 계산한다.
    profile = await asyncio.to_thread(build_profile, mine, contact.alias)

    trigger: RegisterTriggerView | None = None
    changed = changed_segment(profile)
    if changed is not None and state.graph_store is not None and center is not None:
        found, _hints = await find_trigger(state.graph_store, user_id, center, changed.start, hood=hood)
        if found is not None:
            trigger = RegisterTriggerView(event=found.event, at=_day(found.at) if found.at else None)

    segments = tuple(_segment_view(seg, trigger if seg is changed else None) for seg in profile.speech)
    current = profile.current_address
    address = (
        RegisterAddressView(term=current.value, count=current.count, status=current.status)
        if current is not None
        else None
    )
    return RegisterView(segments=segments, address=address, based_on_messages=profile.observations)
