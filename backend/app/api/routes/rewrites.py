from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, status

from app.api.deps import State, UserId
from app.api.schemas import FeedbackIn, RewriteCreate, RewriteOut, UsageOut
from app.domain.rewrite import Feedback, RewriteInput, RewriteRecord

router = APIRouter(prefix="/v1/rewrites", tags=["rewrites"])
# /v1/rewrites/usage는 /{request_id}와 겹치므로 별도 경로
usage_router = APIRouter(prefix="/v1/usage", tags=["usage"])


def _to_out(rec: RewriteRecord) -> RewriteOut:
    return RewriteOut(
        id=rec.id,
        contact_id=rec.contact_id,
        draft_version=rec.draft_version,
        condition=rec.condition,
        created_at=rec.created_at,
        result=rec.result,
    )


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
    user_id: UserId, state: State, contact_id: UUID | None = None, limit: int = Query(20, ge=1, le=100)
) -> list[RewriteOut]:
    return [_to_out(r) for r in await state.rewrite_service.history(user_id, contact_id, limit)]


@router.get("/{request_id}", response_model=RewriteOut)
async def get_rewrite(request_id: UUID, user_id: UserId, state: State) -> RewriteOut:
    return _to_out(await state.rewrite_service.get(user_id, request_id))


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
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        token_limit=u.token_limit,
    )
