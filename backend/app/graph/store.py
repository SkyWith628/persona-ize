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
class EventInfo:
    """Event 노드의 추출 속성 (schema.Event). 재정렬 가중치에 쓴다."""

    uuid: str
    event_type: str | None  # request·refusal·apology·schedule·promise·conflict·status·other
    importance: int | None  # 1~10, 모델이 안 채우면 None


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

    async def event_neighbors(self, group_id: str, node_uuid: str) -> set[str]:
        """이 노드와 엣지로 이어진 Event 노드들의 uuid (상대가 관여한 사건)."""
        ...

    async def event_info(self, group_id: str, uuids: set[str]) -> dict[str, EventInfo]:
        """uuid 중 Event 노드인 것들의 event_type·importance. Person 노드는 결과에 없다."""
        ...

    async def episode_refs(self, uuids: set[str]) -> dict[str, EpisodeRef]: ...

    async def remove_episodes(self, group_id: str, names: list[str]) -> int:
        """이름(`c1:m03`)으로 에피소드를 찾아 지운다.

        그 에피소드만이 근거인 엣지·노드도 함께. 지운 수를 돌려준다.
        """
        ...

    async def remove_group(self, group_id: str) -> int:
        """사용자 그래프 전체 삭제 (계정 삭제). 지운 노드 수."""
        ...

    async def ping(self) -> None:
        """연결 확인. 실패하면 예외."""
        ...

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

    async def event_neighbors(self, group_id: str, node_uuid: str) -> set[str]:
        result = await self._g.driver.execute_query(
            "MATCH (c:Entity {uuid: $uuid, group_id: $group_id})"
            "-[:RELATES_TO]-(e:Event {group_id: $group_id}) "
            "RETURN DISTINCT e.uuid AS uuid",
            uuid=node_uuid,
            group_id=group_id,
        )
        records = result[0] if isinstance(result, tuple) else result.records
        return {r["uuid"] for r in records}

    async def event_info(self, group_id: str, uuids: set[str]) -> dict[str, EventInfo]:
        if not uuids:
            return {}
        result = await self._g.driver.execute_query(
            "MATCH (e:Event {group_id: $group_id}) WHERE e.uuid IN $uuids "
            "RETURN e.uuid AS uuid, e.event_type AS event_type, e.importance AS importance",
            group_id=group_id,
            uuids=sorted(uuids),
        )
        records = result[0] if isinstance(result, tuple) else result.records
        out: dict[str, EventInfo] = {}
        for r in records:
            imp = r["importance"]
            out[r["uuid"]] = EventInfo(
                uuid=r["uuid"], event_type=r["event_type"], importance=int(imp) if imp is not None else None
            )
        return out

    async def episode_refs(self, uuids: set[str]) -> dict[str, EpisodeRef]:
        if not uuids:
            return {}
        from graphiti_core.nodes import EpisodicNode

        nodes = await EpisodicNode.get_by_uuids(self._g.driver, sorted(uuids))
        return {n.uuid: EpisodeRef(uuid=n.uuid, name=n.name, valid_at=n.valid_at) for n in nodes}

    async def remove_episodes(self, group_id: str, names: list[str]) -> int:
        if not names:
            return 0
        result = await self._g.driver.execute_query(
            "MATCH (e:Episodic {group_id: $group_id}) WHERE e.name IN $names RETURN e.uuid AS uuid",
            group_id=group_id,
            names=names,
        )
        records = result[0] if isinstance(result, tuple) else result.records
        uuids = sorted({r["uuid"] for r in records})
        if uuids:
            await self._remove_episode_nodes(group_id, uuids)
        return len(uuids)

    async def _remove_episode_nodes(self, group_id: str, gone: list[str]) -> None:
        """에피소드와, 그 에피소드만 근거인 사실(엣지)·개체(노드)를 지운다. 모델을 부르지 않는다.

        `graphiti.remove_episode`를 쓰지 않는다: 그 구현은 에피소드를 하나 지울 때마다 남는 개체의 요약을
        LLM으로 다시 만든다(2026-11-14 실측: 36건 삭제에 haiku 159회, 248초). 지우는 규칙은 그 구현과 같다 —
        다른 에피소드도 근거인 사실·개체는 남기고 근거 목록에서만 뺀다. 다른 점은 남는 개체의 요약을
        다시 만들지 않고 비운다는 것이다: 지운 대화의 내용이 요약에 남으면 안 되고, 이 앱은 개체 요약을
        읽지 않는다(검색은 사실 엣지를 쓴다). 요약은 다음 수집 때 graphiti가 다시 채운다.

        에피소드를 맨 나중에 지우므로 도중에 실패해도 같은 이름으로 다시 부르면 이어서 지운다.
        """
        run = self._g.driver.execute_query
        params = {"group_id": group_id, "gone": gone}
        # 1) 이 에피소드들이 언급한 개체: 다른 에피소드가 언급하지 않으면 지울 것, 언급하면 남길 것
        result = await run(
            "MATCH (e:Episodic {group_id: $group_id})-[:MENTIONS]->(n:Entity) WHERE e.uuid IN $gone "
            "WITH DISTINCT n "
            "OPTIONAL MATCH (o:Episodic)-[:MENTIONS]->(n) WHERE NOT o.uuid IN $gone "
            "WITH n, count(o) AS others "
            "RETURN n.uuid AS uuid, others",
            **params,
        )
        records = result[0] if isinstance(result, tuple) else result.records
        orphans = sorted(r["uuid"] for r in records if int(r["others"]) == 0)
        survivors = sorted(r["uuid"] for r in records if int(r["others"]) > 0)
        # 2) 사실: 근거 목록에서 빼고, 근거가 남지 않으면 지운다
        await run(
            "MATCH (:Entity)-[r:RELATES_TO {group_id: $group_id}]->(:Entity) "
            "WHERE any(x IN r.episodes WHERE x IN $gone) "
            "SET r.episodes = [x IN r.episodes WHERE NOT x IN $gone] "
            "WITH r WHERE size(r.episodes) = 0 "
            "DELETE r",
            **params,
        )
        # 3) 개체: 이 에피소드만 언급한 것은 지우고, 남는 것은 요약을 비운다
        if orphans:
            await run(
                "MATCH (n:Entity {group_id: $group_id}) WHERE n.uuid IN $uuids DETACH DELETE n",
                group_id=group_id,
                uuids=orphans,
            )
        if survivors:
            await run(
                "MATCH (n:Entity {group_id: $group_id}) WHERE n.uuid IN $uuids SET n.summary = ''",
                group_id=group_id,
                uuids=survivors,
            )
        # 4) 에피소드 (MENTIONS 엣지와 함께)
        await run("MATCH (e:Episodic {group_id: $group_id}) WHERE e.uuid IN $gone DETACH DELETE e", **params)

    async def remove_group(self, group_id: str) -> int:
        result = await self._g.driver.execute_query(
            "MATCH (n {group_id: $group_id}) DETACH DELETE n RETURN count(*) AS removed",
            group_id=group_id,
        )
        records = result[0] if isinstance(result, tuple) else result.records
        return int(records[0]["removed"]) if records else 0

    async def ping(self) -> None:
        await self._g.driver.execute_query("RETURN 1 AS ok")

    async def close(self) -> None:
        await self._g.close()
