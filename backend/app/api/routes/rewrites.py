from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, status

from app.api.deps import State, UserId
from app.api.schemas import FeedbackIn, RewriteCreate, RewriteOut
from app.domain.rewrite import Feedback, RewriteInput, RewriteRecord

router = APIRouter(prefix="/v1/rewrites", tags=["rewrites"])


def _to_out(rec: RewriteRecord) -> RewriteOut:
    return RewriteOut(
        id=rec.id,
        contact_id=rec.contact_id,
        draft_version=rec.draft_version,
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


@router.get("/{request_id}", response_model=RewriteOut)
async def get_rewrite(request_id: UUID, user_id: UserId, state: State) -> RewriteOut:
    return _to_out(await state.rewrite_service.get(user_id, request_id))


@router.post("/{request_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def post_feedback(request_id: UUID, body: FeedbackIn, user_id: UserId, state: State) -> None:
    await state.rewrite_service.feedback(user_id, Feedback(request_id=request_id, **body.model_dump()))
