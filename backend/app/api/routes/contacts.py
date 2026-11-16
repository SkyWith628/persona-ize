from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import AppState, State, UserId
from app.api.schemas import (
    ContactCreate,
    ContactDeletionOut,
    ContactGraphOut,
    ContactOut,
    GraphEdgeOut,
    GraphNodeOut,
    PersonaOut,
    PersonaPut,
)
from app.core.errors import NotFoundError
from app.domain.contact import Contact

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])


async def _to_out(state: AppState, user_id: str, c: Contact) -> ContactOut:
    pv = await state.contacts.latest_persona(user_id, c.id)
    return ContactOut(
        id=c.id,
        alias=c.alias,
        relationship_type=c.relationship_type,
        persona_version=pv.version if pv else None,
        created_at=c.created_at,
    )


@router.post("", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
async def create_contact(body: ContactCreate, user_id: UserId, state: State) -> ContactOut:
    c = await state.contacts.create(user_id, body.alias.strip(), body.relationship_type)
    return await _to_out(state, user_id, c)


@router.get("", response_model=list[ContactOut])
async def list_contacts(user_id: UserId, state: State) -> list[ContactOut]:
    return [await _to_out(state, user_id, c) for c in await state.contacts.list(user_id)]


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(contact_id: UUID, user_id: UserId, state: State) -> ContactOut:
    return await _to_out(state, user_id, await state.contacts.get(user_id, contact_id))


@router.put("/{contact_id}/persona", response_model=PersonaOut)
async def put_persona(contact_id: UUID, body: PersonaPut, user_id: UserId, state: State) -> PersonaOut:
    pv = await state.contacts.save_persona(user_id, contact_id, body.settings, body.expected_version)
    return PersonaOut(
        contact_id=pv.contact_id, version=pv.version, settings=pv.settings, created_at=pv.created_at
    )


@router.get("/{contact_id}/persona", response_model=PersonaOut)
async def get_persona(contact_id: UUID, user_id: UserId, state: State) -> PersonaOut:
    pv = await state.contacts.latest_persona(user_id, contact_id)
    if pv is None:
        raise NotFoundError("저장된 페르소나가 없습니다.")
    return PersonaOut(
        contact_id=pv.contact_id, version=pv.version, settings=pv.settings, created_at=pv.created_at
    )


@router.get("/{contact_id}/graph", response_model=ContactGraphOut, summary="상대 주변 맥락 그래프")
async def get_contact_graph(contact_id: UUID, user_id: UserId, state: State) -> ContactGraphOut:
    """상대 노드와 그 상대가 관여한 사건에 닿은 사실들. 유효한 사실 먼저, 그 안에서는 최근 순."""
    contact = await state.contacts.get(user_id, contact_id)
    if state.graph_store is None:
        return ContactGraphOut(enabled=False)
    center = await state.graph_store.find_entity_uuid(user_id, contact.alias)
    if center is None:
        return ContactGraphOut(enabled=True)
    hood = await state.graph_store.neighborhood(user_id, center)
    now = datetime.now(UTC)
    oldest = datetime.min.replace(tzinfo=UTC)
    edges = [
        GraphEdgeOut(
            id=e.uuid,
            source=e.source_node_uuid,
            target=e.target_node_uuid,
            relation=e.name,
            fact=e.fact,
            valid_at=e.valid_at,
            invalid_at=e.invalid_at,
            current=e.invalid_at is None or e.invalid_at > now,
        )
        for e in hood.edges
    ]
    edges.sort(key=lambda e: (e.current, e.valid_at or oldest, e.id), reverse=True)
    nodes = [
        GraphNodeOut(
            id=n.uuid,
            name=n.name,
            kind=n.kind if n.kind in ("person", "event") else "other",  # type: ignore[arg-type]
            summary=n.summary,
            event_type=n.event_type,
            importance=n.importance,
            is_contact=n.uuid == center,
        )
        for n in sorted(hood.nodes, key=lambda n: (n.uuid != center, n.kind, n.name))
    ]
    return ContactGraphOut(enabled=True, center_id=center, nodes=nodes, edges=edges)


@router.delete("/{contact_id}", response_model=ContactDeletionOut)
async def delete_contact(contact_id: UUID, user_id: UserId, state: State) -> ContactDeletionOut:
    """상대와 그 상대의 대화방·묶음·그래프 에피소드·페르소나·첨삭 이력을 모두 지운다."""
    d = await state.deletion_service.delete_contact(user_id, contact_id)
    return ContactDeletionOut(
        contact_id=d.contact_id,
        conversations=d.conversations,
        messages=d.messages,
        episodes=d.episodes,
        graph_episodes=d.graph_episodes,
        rewrites=d.rewrites,
        graph_errors=d.graph_errors,
    )
