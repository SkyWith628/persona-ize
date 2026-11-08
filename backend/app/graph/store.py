"""그래프 접근 Protocol과 Graphiti 구현. 수집·검색 코드는 Graphiti 객체를 직접 만지지 않는다.

테스트는 `FakeGraphStore`(tests)를 꽂아 Neo4j·LLM 없이 수집 순서·필터 규칙을 확인한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from app.graph.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, EXTRACTION_INSTRUCTIONS

if TYPE_CHECKING:
    from graphiti_core import Graphiti


@dataclass(frozen=True)
class GraphEdgeHit:
    """검색 결과 엣지 하나 (사실 문장 + 시간 + 출처 에피소드)."""

    uuid: str
    name: str
    fact: str
    valid_at: datetime | None
    invalid_at: datetime | None
    episode_uuids: list[str] = field(default_factory=list)
    source_node_uuid: str = ""
    target_node_uuid: str = ""


@dataclass(frozen=True)
class EpisodeRef:
    uuid: str
    name: str  # `c1:m03` — 수집 때 넣은 이름
    valid_at: datetime | None  # 메시지 발화 시각(reference_time)


class GraphStore(Protocol):
    async def prepare(self) -> None:
        """인덱스·제약 생성. 여러 번 불러도 된다."""
        ...

    async def add_episode(
        self, *, name: str, body: str, reference_time: datetime, group_id: str, source_description: str
    ) -> None: ...

    async def search(
        self, query: str, group_id: str, center_node_uuid: str | None, limit: int
    ) -> list[GraphEdgeHit]: ...

    async def find_entity_uuid(self, group_id: str, name: str) -> str | None:
        """이름이 정확히 같은 엔티티 노드 하나 (상대 노드를 검색 중심으로 쓰기 위해)."""
        ...

    async def episode_refs(self, uuids: set[str]) -> dict[str, EpisodeRef]: ...

    async def close(self) -> None: ...


class GraphitiStore:
    def __init__(self, graphiti: Graphiti) -> None:
        self._g = graphiti

    async def prepare(self) -> None:
        await self._g.build_indices_and_constraints()

    async def add_episode(
        self, *, name: str, body: str, reference_time: datetime, group_id: str, source_description: str
    ) -> None:
        await self._g.add_episode(
            name=name,
            episode_body=body,
            source_description=source_description,
            # graphiti 프롬프트는 REFERENCE_TIME을 UTC로 전제한다.
            # +09:00 그대로면 KST/UTC가 섞인다 (스파이크 11-08).
            reference_time=reference_time.astimezone(UTC),
            group_id=group_id,
            entity_types=ENTITY_TYPES,
            edge_types=EDGE_TYPES,
            edge_type_map=EDGE_TYPE_MAP,
            custom_extraction_instructions=EXTRACTION_INSTRUCTIONS,
        )

    async def search(
        self, query: str, group_id: str, center_node_uuid: str | None, limit: int
    ) -> list[GraphEdgeHit]:
        edges = await self._g.search(
            query, center_node_uuid=center_node_uuid, group_ids=[group_id], num_results=limit
        )
        return [
            GraphEdgeHit(
                uuid=e.uuid,
                name=e.name,
                fact=e.fact,
                valid_at=e.valid_at,
                invalid_at=e.invalid_at,
                episode_uuids=list(e.episodes or []),
                source_node_uuid=e.source_node_uuid,
                target_node_uuid=e.target_node_uuid,
            )
            for e in edges
        ]

    async def find_entity_uuid(self, group_id: str, name: str) -> str | None:
        result = await self._g.driver.execute_query(
            "MATCH (n:Entity {group_id: $group_id}) WHERE n.name = $name RETURN n.uuid AS uuid LIMIT 1",
            group_id=group_id,
            name=name,
        )
        records = result[0] if isinstance(result, tuple) else result.records
        return records[0]["uuid"] if records else None

    async def episode_refs(self, uuids: set[str]) -> dict[str, EpisodeRef]:
        if not uuids:
            return {}
        from graphiti_core.nodes import EpisodicNode

        nodes = await EpisodicNode.get_by_uuids(self._g.driver, sorted(uuids))
        return {n.uuid: EpisodeRef(uuid=n.uuid, name=n.name, valid_at=n.valid_at) for n in nodes}

    async def close(self) -> None:
        await self._g.close()
