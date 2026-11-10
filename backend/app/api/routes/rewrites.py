from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, status

from app.api.deps import State, UserId
from app.api.schemas import (
    EvidenceItemOut,
    EvidenceMessageOut,
    EvidenceOut,
    FeedbackIn,
    FeedbackOut,
    RewriteCreate,
    RewriteOut,
    RewritesDeletedOut,
    UsageOut,
)
from app.core.errors import InvalidInputError
from app.domain.rewrite import Feedback, RewriteInput, RewriteRecord

router = APIRouter(prefix="/v1/rewrites", tags=["rewrites"])
# /v1/rewrites/usage는 /{request_id}와 겹치므로 별도 경로
usage_router = APIRouter(prefix="/v1/usage", tags=["usage"])

# 쿼리 문자열에서 '+'가 공백으로 풀린 시간대(…T03:00:00 00:00)를 되살린다.
_SPACE_OFFSET = re.compile(r" (\d{2}:?\d{2})$")


def _to_out(rec: RewriteRecord) -> RewriteOut:
    fb = rec.feedback
    return RewriteOut(
        id=rec.id,
        contact_id=rec.contact_id,
        draft_version=rec.draft_version,
        condition=rec.condition,
        channel=rec.channel,
        created_at=rec.created_at,
        result=rec.result,
        feedback=FeedbackOut(
            accepted=fb.accepted,
            edited_text=fb.edited_text,
            reason=fb.reason,
            reason_code=fb.reason_code,
            consent_to_reuse=fb.consent_to_reuse,
            created_at=fb.created_at,
        )
        if fb is not None
        else None,
    )


def _parse_before(raw: str | None) -> datetime | None:
    """`before` 쿼리(ISO 시각). 시간대가 없으면 UTC로 본다. 못 읽으면 422."""
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if "T" in value or len(value) > 19:  # 날짜와 시각 사이의 공백은 건드리지 않는다
        value = _SPACE_OFFSET.sub(r"+\1", value)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInputError(
            "before는 ISO 시각이어야 합니다 (예: 2026-11-11T03:00:00+00:00). "
            "목록의 created_at을 그대로 넣으세요."
        ) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@router.post("", response_model=RewriteOut, status_code=status.HTTP_201_CREATED)
async def create_rewrite(
    body: RewriteCreate,
    user_id: UserId,
    state: State,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RewriteOut:
    inp = RewriteInput(user_id=user_id, **body.model_dump())
    rec = await state.rewrite_service.rewrite(inp, idempotency_key)
    return _to_out(rec)


@router.get("", response_model=list[RewriteOut], summary="첨삭 이력 (최근 순)")
async def list_rewrites(
    user_id: UserId,
    state: State,
    contact_id: UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
    before: str | None = Query(
        None, description="이 시각보다 앞의 이력만 (페이지 넘김: 앞 페이지 마지막 항목의 created_at)"
    ),
) -> list[RewriteOut]:
    rows = await state.rewrite_service.history(user_id, contact_id, limit, _parse_before(before))
    return [_to_out(r) for r in rows]


@router.delete("", response_model=RewritesDeletedOut, summary="한 상대의 첨삭 이력 전부 삭제")
async def delete_rewrites(user_id: UserId, state: State, contact_id: UUID) -> RewritesDeletedOut:
    """`contact_id`는 필수다 — 전체 이력을 한 번에 지우는 경로는 두지 않는다."""
    return RewritesDeletedOut(deleted=await state.rewrite_service.delete_by_contact(user_id, contact_id))


@router.get("/{request_id}", response_model=RewriteOut)
async def get_rewrite(request_id: UUID, user_id: UserId, state: State) -> RewriteOut:
    return _to_out(await state.rewrite_service.get(user_id, request_id))


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rewrite(request_id: UUID, user_id: UserId, state: State) -> None:
    """첨삭 이력 한 건(결과·피드백 포함) 삭제."""
    await state.rewrite_service.delete(user_id, request_id)


@router.get("/{request_id}/evidence", response_model=EvidenceOut)
async def get_evidence(request_id: UUID, user_id: UserId, state: State) -> EvidenceOut:
    """참고한 과거 표현의 원문. 결과에는 참조만 있고, 여기서 **지금 남아 있는** 메시지를 읽어 온다.

    대화방을 지웠으면 그 근거는 나오지 않고 `missing`에 수만 남는다.
    """
    ev = await state.rewrite_service.evidence(user_id, request_id)
    return EvidenceOut(
        items=[
            EvidenceItemOut(
                id=item.id,
                source_message_ids=item.source_message_ids,
                messages=[
                    EvidenceMessageOut(speaker_role=m.speaker_role, sent_at=m.sent_at, text=m.text)
                    for m in item.messages
                ],
            )
            for item in ev.items
        ],
        missing=ev.missing,
    )


@router.post("/{request_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def post_feedback(request_id: UUID, body: FeedbackIn, user_id: UserId, state: State) -> None:
    await state.rewrite_service.feedback(user_id, Feedback(request_id=request_id, **body.model_dump()))


@usage_router.get("", response_model=UsageOut, summary="오늘(KST) 사용량과 한도")
async def get_usage(user_id: UserId, state: State) -> UsageOut:
    u = await state.rewrite_service.usage(user_id)
    return UsageOut(
        day=u.day,
        rewrites=u.rewrites,
        rewrite_limit=u.rewrite_limit,
        remaining_rewrites=u.remaining_rewrites,
        graph_messages_ingested=u.graph_messages_ingested,
        graph_message_cap=u.graph_message_cap,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        token_limit=u.token_limit,
    )
