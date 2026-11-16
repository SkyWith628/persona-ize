"""말투가 바뀐 계기와 적용 범위 단서를 그래프에서 찾는다 (docs/graph-redesign-2026-11-16.md 3.3).

규칙 프로필(`app.ai.register`)은 "10/21부터 합쇼체"까지만 안다. 왜 바뀌었는지(파트장 발령)와 어디까지
적용되는지("사적으로는 편하게 하자")는 메시지 겉모습에 없고 그래프에 있다.

무엇을 읽는가
- 계기: 상대 노드 이웃의 `Event` 노드 중 **그 사건을 말한 에피소드(메시지)의 시각**이 변화 시점 T의
  앞뒤 `TRIGGER_WINDOW` 안인 것. T **이전**에서 가장 가까운 것을 고르고, 없으면 T와 그 이후에서 가장 가까운
  것. T는 새 말투로 쓴 첫 메시지의 시각이라, 그 메시지가 말한 사건(회의록 공유)은 계기가 아니라 결과다.
- 범위 단서: 상대에 닿은 `Relationship` 사실 중 근거 에피소드가 같은 기간에 있는 것의 사실 문장과
  출처 메시지 id.

믿을 만한가
- 기존 이웃 조회(`GraphStore.neighborhood`)와 에피소드 조회(`episode_refs`)만 쓴다. 모델 호출이 없다.
- **무효화 표시(invalid_at)는 보지 않는다** — 추출 부산물이 많았다 (`retriever.py` 설명). 사실 시각
  (valid_at)도 쓰지 않는다: 모델이 정한 값이라 흔들린다. 메시지 발화 시각만 쓴다.
- 결과는 사용자에게 보여 주는 제안의 근거일 뿐이다. 첨삭 프롬프트에 내용으로 들어가지 않는다.
- 그래프가 죽었거나 값이 이상하면 계기도 단서도 없는 것으로 한다. 예외를 올리지 않는다.
- ±7일은 판단이다 (설계 8절 미해결 질문).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.conversation import exception_kind
from app.graph.store import EpisodeRef, GraphEdgeHit, GraphStore

log = logging.getLogger(__name__)

TRIGGER_WINDOW = timedelta(days=7)  # 변화 시점 앞뒤로 계기·단서를 찾는 기간
MAX_SCOPE_HINTS = 4  # 카드에 보여 줄 범위 단서 수 (변화 시점에 가까운 것부터)
RELATIONSHIP_EDGE = "Relationship"  # schema.EDGE_TYPES의 이름
EVENT_KIND = "event"  # GraphNodeInfo.kind


@dataclass(frozen=True)
class Trigger:
    """말투가 바뀐 계기로 보이는 사건."""

    event: str  # Event 노드 이름 (예: '파트장 발령')
    at: datetime | None  # 그 사건을 처음 말한 메시지의 시각
    message_ids: tuple[str, ...]  # 출처 메시지 (`c3:m05`), 시간 순


@dataclass(frozen=True)
class ScopeHint:
    """그 기간의 관계 사실 하나 — 말투의 적용 범위를 짐작하게 하는 단서."""

    text: str  # 사실 문장 (그래프 추출기가 쓴 문장)
    message_ids: tuple[str, ...]


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _in_window(
    edge: GraphEdgeHit,
    refs: dict[str, EpisodeRef],
    changed_at: datetime,
    window: timedelta,
    as_of: datetime | None,
) -> list[tuple[datetime, str]]:
    """이 엣지의 근거 에피소드 중 기간 안에 있는 것들의 (발화 시각, 메시지 id), 시간 순."""
    out: list[tuple[datetime, str]] = []
    for uuid in edge.episode_uuids:
        ref = refs.get(uuid)
        if ref is None or ref.valid_at is None:
            continue
        at = _aware(ref.valid_at)
        if as_of is not None and at >= as_of:
            continue  # 평가: 기준 시점 이후의 메시지는 못 본 것으로 한다
        if abs(at - changed_at) <= window:
            out.append((at, ref.name))
    return sorted(out)


async def find_trigger(
    store: GraphStore,
    user_id: str,
    contact_uuid: str,
    changed_at: datetime,
    *,
    as_of: datetime | None = None,
    window: timedelta = TRIGGER_WINDOW,
) -> tuple[Trigger | None, list[ScopeHint]]:
    """변화 시점 `changed_at` 앞뒤의 계기 사건과 범위 단서. 못 찾거나 그래프가 실패하면 (None, []).

    `contact_uuid`는 상대 엔티티 노드(`find_entity_uuid`로 찾은 것), `as_of`는 평가용 상한이다.
    """
    try:
        changed_at = _aware(changed_at)
        as_of = _aware(as_of) if as_of is not None else None
        hood = await store.neighborhood(user_id, contact_uuid)
        refs = await store.episode_refs({u for e in hood.edges for u in e.episode_uuids})
        events = {n.uuid: n.name for n in hood.nodes if n.kind == EVENT_KIND and n.uuid != contact_uuid}

        # 사건마다: 그 사건에 닿은 엣지들의 근거 메시지 가운데 기간 안의 것
        mentions: dict[str, set[tuple[datetime, str]]] = {}
        hints: list[tuple[timedelta, datetime, ScopeHint]] = []
        for edge in hood.edges:
            ends = (edge.source_node_uuid, edge.target_node_uuid)
            seen = _in_window(edge, refs, changed_at, window, as_of)
            if not seen:
                continue
            for end in ends:
                if end in events:
                    mentions.setdefault(end, set()).update(seen)
            if edge.name == RELATIONSHIP_EDGE and contact_uuid in ends and edge.fact.strip():
                first = seen[0][0]
                hint = ScopeHint(text=edge.fact.strip(), message_ids=tuple(name for _, name in seen))
                hints.append((abs(first - changed_at), first, hint))

        trigger: Trigger | None = None
        if mentions:
            # 사건의 시각 = 기간 안에서 그 사건을 처음 말한 메시지. T보다 앞선 것 먼저, 그 안에서는 가까운 순.
            def rank(item: tuple[str, set[tuple[datetime, str]]]) -> tuple[bool, timedelta, str]:
                first = min(at for at, _ in item[1])
                return (first >= changed_at, abs(first - changed_at), item[0])

            uuid, seen_set = min(mentions.items(), key=rank)
            ordered = sorted(seen_set)
            trigger = Trigger(
                event=events[uuid], at=ordered[0][0], message_ids=tuple(dict.fromkeys(n for _, n in ordered))
            )

        hints.sort(key=lambda h: (h[0], h[1], h[2].text))
        picked: dict[str, tuple[datetime, ScopeHint]] = {}
        for _, first, hint in hints:
            if hint.text not in picked and len(picked) < MAX_SCOPE_HINTS:
                picked[hint.text] = (first, hint)  # 같은 문장은 한 번만
        scope = [hint for _, hint in sorted(picked.values(), key=lambda p: (p[0], p[1].text))]  # 시간 순
        return trigger, scope
    except Exception as exc:  # noqa: BLE001 - 제안의 부가 근거다. 그래프 장애로 제안을 막지 않는다
        log.warning("graph trigger lookup failed, continuing without it: %s", exception_kind(exc))
        return None, []
