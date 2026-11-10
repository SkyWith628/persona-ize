from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, status

from app.ai.style_suggest import MAX_MESSAGES, suggest_persona
from app.api.deps import AppState, State, UserId
from app.api.schemas import (
    ContactCreate,
    ContactDeletionOut,
    ContactOut,
    ContactUpdate,
    PersonaOut,
    PersonaPut,
    PersonaSignalOut,
    PersonaSuggestionOut,
)
from app.core.errors import InvalidInputError, NotFoundError
from app.domain.contact import Contact
from app.domain.persona import PersonaVersion, default_persona

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
    """
    contact = await state.contacts.get(user_id, contact_id)
    mine: list[tuple[datetime, str]] = []
    for conv in await state.conversations.list(user_id, contact.id):
        mine.extend(
            (m.sent_at, m.text)
            for m in await state.conversations.list_messages(user_id, conv.id)
            if m.speaker_role == "me"
        )
    mine.sort(key=lambda pair: pair[0])
    texts = [text for _, text in mine][-MAX_MESSAGES:]  # 최근 것부터 MAX_MESSAGES건만 본다
    current = await state.contacts.latest_persona(user_id, contact.id)
    base = current.settings if current else default_persona(contact.relationship_type)
    settings, signals = suggest_persona(texts, base, contact.alias)
    return PersonaSuggestionOut(
        based_on_messages=len(texts),
        settings=settings,
        signals=[PersonaSignalOut(field=s.field, value=s.value, reason=s.reason) for s in signals],
    )


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
