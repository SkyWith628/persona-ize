"""대화방·가져오기·삭제 API (project-design §7).

삭제는 요청 안에서 끝내고 결과를 바로 돌려준다 (설계의 deletion-jobs는 데이터가 커지면). 그래프 삭제가
실패해도 DB는 지워지고 `graph_error`에 사유가 남는다.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import State, UserId
from app.api.schemas import ConversationCreate, ConversationOut, DeletionOut, ImportJobOut, MessageOut
from app.domain.conversation import Conversation, ImportJob
from app.services.import_service import ImportPayload

router = APIRouter(tags=["conversations"])


def _conv_out(c: Conversation) -> ConversationOut:
    return ConversationOut(
        id=c.id,
        contact_id=c.contact_id,
        title=c.title,
        source=c.source,
        source_key=c.source_key,
        me_name=c.me_name,
        created_at=c.created_at,
    )


def _job_out(j: ImportJob) -> ImportJobOut:
    return ImportJobOut(
        id=j.id,
        conversation_id=j.conversation_id,
        status=j.status,
        imported=j.imported,
        duplicates=j.duplicates,
        episodes=j.episodes,
        error_code=j.error_code,
        error_message=j.error_message,
        graph_status=j.graph_status,
        graph_episodes=j.graph_episodes,
        graph_error=j.graph_error,
        created_at=j.created_at,
    )


@router.post("/v1/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(body: ConversationCreate, user_id: UserId, state: State) -> ConversationOut:
    conv = await state.import_service.create_conversation(
        user_id, body.contact_id, body.title, body.source, body.source_key, body.me_name
    )
    return _conv_out(conv)


@router.get("/v1/conversations", response_model=list[ConversationOut])
async def list_conversations(
    user_id: UserId, state: State, contact_id: UUID | None = None
) -> list[ConversationOut]:
    return [_conv_out(c) for c in await state.conversations.list(user_id, contact_id)]


@router.get("/v1/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(conversation_id: UUID, user_id: UserId, state: State) -> ConversationOut:
    return _conv_out(await state.conversations.get(user_id, conversation_id))


@router.get("/v1/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(conversation_id: UUID, user_id: UserId, state: State) -> list[MessageOut]:
    return [
        MessageOut(
            id=m.id, source_key=m.source_key, speaker_role=m.speaker_role, sent_at=m.sent_at, text=m.text
        )
        for m in await state.conversations.list_messages(user_id, conversation_id)
    ]


@router.post(
    "/v1/conversations/{conversation_id}/imports",
    response_model=ImportJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_messages(
    conversation_id: UUID, body: ImportPayload, user_id: UserId, state: State
) -> ImportJobOut:
    """지금은 요청 안에서 처리를 끝내고 완료된 작업을 돌려준다 (202는 설계의 비동기 전환을 위해 유지)."""
    return _job_out(await state.import_service.import_messages(user_id, conversation_id, body))


@router.get("/v1/imports/{job_id}", response_model=ImportJobOut)
async def get_import(job_id: UUID, user_id: UserId, state: State) -> ImportJobOut:
    return _job_out(await state.conversations.get_job(user_id, job_id))


@router.delete("/v1/conversations/{conversation_id}", response_model=DeletionOut)
async def delete_conversation(conversation_id: UUID, user_id: UserId, state: State) -> DeletionOut:
    """원문·묶음·가져오기 작업·그래프 에피소드를 지운다. 이후 C/D 검색에 이 대화방은 나오지 않는다."""
    d = await state.import_service.delete_conversation(user_id, conversation_id)
    return DeletionOut(
        conversation_id=d.conversation_id,
        messages=d.messages,
        episodes=d.episodes,
        graph_episodes=d.graph_episodes,
        graph_error=d.graph_error,
    )
