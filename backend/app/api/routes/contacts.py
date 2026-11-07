from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import State, UserId
from app.api.schemas import ContactCreate, ContactOut, PersonaOut, PersonaPut

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])


def _to_out(state, user_id: str, c) -> ContactOut:  # noqa: ANN001
    pv = state.contacts.latest_persona(user_id, c.id)
    return ContactOut(
        id=c.id,
        alias=c.alias,
        relationship_type=c.relationship_type,
        persona_version=pv.version if pv else None,
        created_at=c.created_at,
    )


@router.post("", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
def create_contact(body: ContactCreate, user_id: UserId, state: State) -> ContactOut:
    c = state.contacts.create(user_id, body.alias.strip(), body.relationship_type)
    return _to_out(state, user_id, c)


@router.get("", response_model=list[ContactOut])
def list_contacts(user_id: UserId, state: State) -> list[ContactOut]:
    return [_to_out(state, user_id, c) for c in state.contacts.list(user_id)]


@router.get("/{contact_id}", response_model=ContactOut)
def get_contact(contact_id: UUID, user_id: UserId, state: State) -> ContactOut:
    return _to_out(state, user_id, state.contacts.get(user_id, contact_id))


@router.put("/{contact_id}/persona", response_model=PersonaOut)
def put_persona(contact_id: UUID, body: PersonaPut, user_id: UserId, state: State) -> PersonaOut:
    pv = state.contacts.save_persona(user_id, contact_id, body.settings, body.expected_version)
    return PersonaOut(
        contact_id=pv.contact_id, version=pv.version, settings=pv.settings, created_at=pv.created_at
    )


@router.get("/{contact_id}/persona", response_model=PersonaOut)
def get_persona(contact_id: UUID, user_id: UserId, state: State) -> PersonaOut:
    pv = state.contacts.latest_persona(user_id, contact_id)
    if pv is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("저장된 페르소나가 없습니다.")
    return PersonaOut(
        contact_id=pv.contact_id, version=pv.version, settings=pv.settings, created_at=pv.created_at
    )
