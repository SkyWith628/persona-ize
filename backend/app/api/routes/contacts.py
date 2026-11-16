from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import AppState, State, UserId
from app.api.schemas import (
    ContactCreate,
    ContactDeletionOut,
    ContactGraphOut,
    ContactOut,
    ContactUpdate,
    GraphEdgeOut,
    GraphNodeOut,
    PersonaOut,
    PersonaPut,
    PersonaSignalOut,
    PersonaSuggestionOut,
    SuggestionCardOut,
)
from app.core.errors import InvalidInputError, NotFoundError
from app.domain.contact import Contact
from app.domain.persona import PersonaVersion, default_persona
from app.domain.usage import QuotaExceededError
from app.services.suggestion_service import ALL_SOURCES, suggest_persona_for_contact

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])


def _out(c: Contact, persona_version: int | None) -> ContactOut:
    return ContactOut(
        id=c.id,
        alias=c.alias,
        relationship_type=c.relationship_type,
        persona_version=persona_version,
        created_at=c.created_at,
    )


async def _to_out(state: AppState, user_id: str, c: Contact) -> ContactOut:
    pv = await state.contacts.latest_persona(user_id, c.id)
    return _out(c, pv.version if pv else None)


def _persona_out(pv: PersonaVersion) -> PersonaOut:
    return PersonaOut(
        contact_id=pv.contact_id, version=pv.version, settings=pv.settings, created_at=pv.created_at
    )


@router.post("", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
async def create_contact(body: ContactCreate, user_id: UserId, state: State) -> ContactOut:
    alias = body.alias.strip()
    if not alias:
        raise InvalidInputError("상대 이름(별칭)이 비어 있습니다.")
    limit = state.settings.max_contacts_per_user
    owned = await state.contacts.count(user_id)
    if owned >= limit:
        raise QuotaExceededError(
            f"상대는 계정당 {limit:,}명까지 만들 수 있습니다 (지금 {owned:,}명). "
            "쓰지 않는 상대를 지운 뒤 다시 시도하세요."
        )
    c = await state.contacts.create(user_id, alias, body.relationship_type)
    if body.use_default_persona:
        pv = await state.contacts.save_persona(user_id, c.id, default_persona(c.relationship_type), 0)
        return _out(c, pv.version)
    return _out(c, None)


@router.get("", response_model=list[ContactOut])
async def list_contacts(user_id: UserId, state: State) -> list[ContactOut]:
    versions = await state.contacts.latest_persona_versions(user_id)  # 상대마다 따로 묻지 않는다
    return [_out(c, versions.get(c.id)) for c in await state.contacts.list(user_id)]


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(contact_id: UUID, user_id: UserId, state: State) -> ContactOut:
    return await _to_out(state, user_id, await state.contacts.get(user_id, contact_id))


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(contact_id: UUID, body: ContactUpdate, user_id: UserId, state: State) -> ContactOut:
    """별칭·관계 유형 수정. 페르소나는 그대로다 (관계를 바꿔도 저장된 설정을 덮어쓰지 않는다).

    이미 그래프에 들어간 에피소드에는 옛 별칭이 남는다 — 별칭을 바꾸면 D 조건의 상대 중심 검색이
    느슨해질 수 있다.
    """
    alias = body.alias.strip() if body.alias is not None else None
    if alias is not None and not alias:
        raise InvalidInputError("상대 이름(별칭)이 비어 있습니다.")
    c = await state.contacts.update(
        user_id, contact_id, alias=alias, relationship_type=body.relationship_type
    )
    return await _to_out(state, user_id, c)


@router.put("/{contact_id}/persona", response_model=PersonaOut)
async def put_persona(contact_id: UUID, body: PersonaPut, user_id: UserId, state: State) -> PersonaOut:
    pv = await state.contacts.save_persona(user_id, contact_id, body.settings, body.expected_version)
    return _persona_out(pv)


@router.get("/{contact_id}/persona", response_model=PersonaOut)
async def get_persona(contact_id: UUID, user_id: UserId, state: State) -> PersonaOut:
    pv = await state.contacts.latest_persona(user_id, contact_id)
    if pv is None:
        raise NotFoundError("저장된 페르소나가 없습니다.")
    return _persona_out(pv)


@router.get("/{contact_id}/persona/versions", response_model=list[PersonaOut])
async def list_persona_versions(contact_id: UUID, user_id: UserId, state: State) -> list[PersonaOut]:
    """페르소나 버전 목록 (최신 먼저, 최대 50). 저장된 적이 없으면 빈 목록."""
    return [_persona_out(pv) for pv in await state.contacts.list_personas(user_id, contact_id, limit=50)]


@router.get("/{contact_id}/persona/suggestion", response_model=PersonaSuggestionOut)
async def suggest_persona_settings(contact_id: UUID, user_id: UserId, state: State) -> PersonaSuggestionOut:
    """이 상대에게 보낸 내 과거 메시지에서 문체 수치를 규칙으로 추정한다 (모델 호출 없음, 저장하지 않음).

    추정하지 않는 항목은 저장된 페르소나(없으면 관계 유형 기본값)의 값이다. 성격은 추정하지 않는다.
    내 메시지가 5건보다 적으면 메시지로는 추정하지 않는다: `based_on_messages`에 본 메시지 수가 나온다.

    그래프가 켜져 있으면 **관계 사실의 시간 순 변화**에서 읽은 제안을 뒤에 붙인다 (`source: "graph"`,
    `since`: 그 값이 처음 확인된 날). 내 메시지 전체를 한 덩어리로 보는 추정과 달리 "10/17부터 존댓말",
    "10/21부터 '파트장님'"처럼 바뀐 시점을 말한다. 같은 항목이면 그래프 쪽이 `settings`에 반영된다(뒤에 온
    근거가 이긴다). 그래프 조회가 실패하면 메시지 근거만 돌려준다.

    말투 프로필(`source: "register"`)은 내 메시지를 문장 단위로 분류해 말투가 **언제 바뀌었는지**를 말한다
    ("10/21부터 합쇼체 2건, 그 전에는 반말 3건"). 근거 순서는 messages → register → graph. 제안할 것이 있으면
    `card`에 제안 카드가 온다 — 이전·지금 말투와 근거 메시지, 호칭, 그래프에서 찾은 계기 사건과 범위 단서.
    `card.ask`가 참이면 말투 프로필의 값은 `settings`에 넣지 않는다 (값을 정해 주지 않고 묻는다).
    말투 프로필에는 메시지 수 문턱이 없다 — 구간 규칙(같은 쪽 말투 연속 2건, 호칭 2회)만 있고 건수를
    근거에 적는다. 그래서 내 메시지가 5건보다 적어도 register 근거와 카드는 올 수 있다.
    계산은 `services/suggestion_service.py`가 한다.
    """
    contact = await state.contacts.get(user_id, contact_id)
    # 말투 프로필 근거는 설정으로 끌 수 있다 (`REGISTER_SUGGESTIONS=false` → 예전 응답 그대로)
    sources = ALL_SOURCES if state.settings.register_suggestions else ALL_SOURCES - {"register"}
    suggestion = await suggest_persona_for_contact(state, user_id, contact, sources=sources)
    out = PersonaSuggestionOut(
        based_on_messages=suggestion.based_on_messages,
        settings=suggestion.settings,
        signals=[
            PersonaSignalOut(field=s.field, value=s.value, reason=s.reason, source=s.source, since=s.since)  # type: ignore[arg-type]
            for s in suggestion.signals
        ],
    )
    if suggestion.card is not None:
        out.card = SuggestionCardOut.model_validate(asdict(suggestion.card))
    return out


@router.get("/{contact_id}/graph", response_model=ContactGraphOut, summary="상대 주변 맥락 그래프")
async def get_contact_graph(contact_id: UUID, user_id: UserId, state: State) -> ContactGraphOut:
    """상대 노드와 그 상대가 관여한 사건에 닿은 사실들. 유효한 사실 먼저, 그 안에서는 최근 순."""
    contact = await state.contacts.get(user_id, contact_id)
    if state.graph_store is None:
        return ContactGraphOut(enabled=False)
    # 검색기와 같은 이름으로 찾는다 — 상대 이름을 바꿔도 그래프에는 처음 수집한 이름이 남는다 (0009).
    center = await state.graph_store.find_entity_uuid(user_id, contact.name_in_graph)
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
