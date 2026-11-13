"""대화방·가져오기·삭제 API (project-design §7).

삭제는 요청 안에서 끝내고 결과를 바로 돌려준다 (설계의 deletion-jobs는 데이터가 커지면). 그래프 삭제가
실패해도 DB는 지워지고 `graph_error`에 사유가 남는다.

가져오기 경로는 둘이다. 둘 다 `IMPORT_MASK_PII`면 본문의 개인정보 패턴을 가려서 저장한다.
- `POST /v1/conversations/{id}/imports` — 메시지 배열(JSON). 검증 실패가 하나라도 있으면 저장하지 않고 422.
- `POST /v1/conversations/{id}/imports/raw` — 내보내기 원문(카카오톡·텔레그램·붙여넣기)을 서버가 읽는다.
  미리보기는 `POST /v1/imports/parse`(routes/data.py).

사용자별 보관 한도(`MAX_CONVERSATIONS_PER_USER`, `MAX_MESSAGES_PER_USER`)를 넘는 요청은 아무것도 저장하지
않고 422 `quota_exceeded`다. 한도는 이 API 경로에서만 건다 (합성 적재·평가 스크립트는 서비스를 직접 부른다).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import State, UserId
from app.api.schemas import (
    ConversationCreate,
    ConversationOut,
    ConversationStatsOut,
    DeletionOut,
    ImportJobOut,
    ImportRawIn,
    MessageOut,
)
from app.domain.conversation import Conversation, ImportJob
from app.importers import parse_import
from app.services.import_service import ImportPayload

router = APIRouter(tags=["conversations"])
MESSAGES_LIMIT_MAX = 5000  # 메시지 목록의 limit 상한 (한 번에 가져올 수 있는 수와 같은 수준)


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
        user_id,
        body.contact_id,
        body.title,
        body.source,
        body.source_key,
        body.me_name,
        max_per_user=state.settings.max_conversations_per_user,
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
async def list_messages(
    conversation_id: UUID,
    user_id: UserId,
    state: State,
    limit: int | None = Query(
        None, ge=1, le=MESSAGES_LIMIT_MAX, description="가장 최근 N건만 (시각 순). 생략하면 전부"
    ),
) -> list[MessageOut]:
    """시각 순 메시지. `limit`을 주면 가장 최근 N건만 돌려준다 (생략하면 예전처럼 전부)."""
    return [
        MessageOut(
            id=m.id, source_key=m.source_key, speaker_role=m.speaker_role, sent_at=m.sent_at, text=m.text
        )
        for m in await state.conversations.list_messages(user_id, conversation_id, limit)
    ]


@router.get("/v1/conversations/{conversation_id}/stats", response_model=ConversationStatsOut)
async def conversation_stats(conversation_id: UUID, user_id: UserId, state: State) -> ConversationStatsOut:
    """가져오기 2단계 진행 표시용. 말투(묶음)는 가져오는 즉시, 사건 정리는 `graph_ingested / messages`.

    `graph_limited`: 그래프 수집 상한에 닿아 남은 메시지가 지금은 들어가지 않는다 (문체 검색은 그대로 된다).
    """
    stats = await state.conversations.conversation_stats(user_id, conversation_id)
    job = await state.conversations.latest_job(user_id, conversation_id)
    pending = stats.graph_pending if state.graph_enabled else 0
    limited = False
    if state.graph_enabled and pending > 0:
        limited = (job is not None and job.graph_status == "limited") or (
            state.graph_ingest is not None and await state.graph_ingest.is_limited(user_id)
        )
    return ConversationStatsOut(
        conversation_id=stats.conversation_id,
        messages=stats.messages,
        my_messages=stats.my_messages,
        episodes=stats.episodes,
        graph_enabled=state.graph_enabled,
        graph_ingested=stats.graph_ingested,
        graph_pending=pending,
        graph_limited=limited,
        first_message_at=stats.first_message_at,
        last_message_at=stats.last_message_at,
        latest_job=_job_out(job) if job is not None else None,
    )


@router.post(
    "/v1/conversations/{conversation_id}/imports",
    response_model=ImportJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_messages(
    conversation_id: UUID, body: ImportPayload, user_id: UserId, state: State
) -> ImportJobOut:
    """메시지를 저장하고 작업을 돌려준다. 묶음 생성이 `IMPORT_INDEX_WAIT_SECONDS` 안에 끝나면 `completed`,
    아니면 `processing` — 나머지는 뒤에서 끝나고 `GET /v1/imports/{id}`로 확인한다."""
    job = await state.import_service.import_messages(
        user_id,
        conversation_id,
        body,
        mask_pii=state.settings.import_mask_pii,
        max_user_messages=state.settings.max_messages_per_user,
    )
    return _job_out(job)


@router.post(
    "/v1/conversations/{conversation_id}/imports/raw",
    response_model=ImportJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_raw(conversation_id: UUID, body: ImportRawIn, user_id: UserId, state: State) -> ImportJobOut:
    """내보내기 원문을 읽어 화자 지정대로 저장한다 (미리보기와 같은 파서, 같은 메시지 id).

    `speaker_map`에 없는 화자가 있으면 422(이름 나열). `skip`은 그 화자의 메시지를 버리고, `keep_recent`는
    최근 N건만 남긴다. 결과가 0건이거나 `MAX_IMPORT_MESSAGES`를 넘으면 422. 아무것도 저장하지 않는다.
    """
    await state.conversations.get(user_id, conversation_id)  # 소유권: 큰 본문을 읽기 전에 404부터
    settings = state.settings
    parsed = await asyncio.to_thread(
        parse_import,
        body.text,
        body.format,
        body.tz_offset_minutes,
        chat_id=str(body.chat_id) if body.chat_id is not None else None,
        mask=settings.import_mask_pii,
        max_chars=settings.max_import_chars,
    )
    job = await state.import_service.import_parsed(
        user_id,
        conversation_id,
        parsed,
        body.speaker_map,
        keep_recent=body.keep_recent,
        max_user_messages=settings.max_messages_per_user,
    )
    return _job_out(job)


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
