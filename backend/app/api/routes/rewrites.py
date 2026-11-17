from __future__ import annotations

import asyncio
import logging
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
    RewriteBatchCreate,
    RewriteBatchErrorOut,
    RewriteBatchItemOut,
    RewriteBatchOut,
    RewriteCreate,
    RewriteOut,
    RewritesDeletedOut,
    UsageOut,
)
from app.core.errors import DomainError, InvalidInputError
from app.core.http_errors import INTERNAL_ERROR_MESSAGE
from app.domain.conversation import exception_kind
from app.domain.rewrite import IDEMPOTENCY_KEY_MAX, Feedback, RewriteInput, RewriteRecord

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/rewrites", tags=["rewrites"])
# /v1/rewrites/usage는 /{request_id}와 겹치므로 별도 경로
usage_router = APIRouter(prefix="/v1/usage", tags=["usage"])

# 쿼리 문자열에서 '+'가 공백으로 풀린 시간대(…T03:00:00 00:00)를 되살린다.
_SPACE_OFFSET = re.compile(r" (\d{2}:?\d{2})$")
EDITED_TEXT_FACTOR = 2  # 피드백의 고친 문장 길이 한도 = 초안 한도(MAX_DRAFT_CHARS) × 이 값
# 묶음 요청의 멱등 키는 상대마다 `<키>:<contact_id>`로 파생한다 — 붙는 길이(':' + UUID 36자)만큼 짧아야 한다.
BATCH_KEY_SUFFIX_CHARS = 37


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
        # UTC로 맞춰 둔다. 0001년·9999년 근처는 시간대 변환이 넘친다 — 저장소까지 가면 500이었다.
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise InvalidInputError(
            "before는 ISO 시각이어야 합니다 (예: 2026-11-11T03:00:00+00:00). "
            "목록의 created_at을 그대로 넣으세요."
        ) from exc


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


@router.post(
    "/batch",
    response_model=RewriteBatchOut,
    summary="같은 초안을 여러 상대에게 (상대마다 그 상대의 말투로)",
)
async def create_rewrites_batch(
    body: RewriteBatchCreate,
    user_id: UserId,
    state: State,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RewriteBatchOut:
    """상대마다 단건 첨삭(`RewriteService.rewrite`)을 그대로 한 번씩 부른다 — 새 첨삭 로직은 없다.

    - 그래서 상대 1명 = 요청 1건이다: 사용량·한도·이력 저장·멱등 처리가 단건과 똑같이 적용된다.
    - 한 상대가 실패해도(없는 상대, 한도 초과, 저장된 설정 없음 등) 나머지는 계속한다. 실패한 항목만
      `error`를 채우고 응답은 200이다. 본문 자체가 틀린 경우(빈 목록·5명 초과·중복, 그리고 모든 상대에
      똑같이 걸리는 입력 오류 — 빈·너무 긴 초안, 긴 상황 설명, 못 쓰는 키)만 422이고 아무것도 처리하지 않는다.
    - `Idempotency-Key`는 상대마다 `<키>:<contact_id>`로 파생한다. 같은 키로 다시 보내면 이미 끝난 상대는
      저장된 결과를 돌려받고(모델을 다시 부르지 않는다) 실패했던 상대만 다시 시도한다.
    - 상대들은 동시에 처리한다. 단건 경로가 원래 동시 요청을 견디게 돼 있다: 한도 확인·예약은
      `_admit`의 잠금 하나로 직렬화되고(진행 중 요청까지 센다), 멱등 잠금은 파생 키마다 따로이며,
      저장소는 호출마다 자기 세션을 연다. 묶음 요청은 같은 사용자가 단건 요청을 동시에 보낸 것과 같다.
      (한도에 거의 닿은 사용자는 "진행 중인 요청까지 합치면 한도" 오류를 일부 항목에서 볼 수 있다 —
      동시에 보낸 단건 요청과 같은 동작이다.)
    """
    if idempotency_key and len(idempotency_key) > IDEMPOTENCY_KEY_MAX - BATCH_KEY_SUFFIX_CHARS:
        # 그대로 두면 파생 키가 길이 한도를 넘어 모든 항목이 같은 오류로 실패한다 — 요청 전체를 돌려보낸다.
        raise InvalidInputError(
            f"묶음 요청의 Idempotency-Key는 {IDEMPOTENCY_KEY_MAX - BATCH_KEY_SUFFIX_CHARS}자 이내여야 합니다."
        )
    shared = body.model_dump(exclude={"contact_ids"})
    # 상대와 무관한 입력 오류(빈·너무 긴 초안, 긴 상황 설명, 저장 못 할 문자, 보이지 않는 문자가 든 키)는
    # 단건에서 422다. 상대마다 돌리면 같은 오류가 상대 수만큼 200으로 나가므로 나누기 전에 한 번 검사한다.
    # 검사는 contact_id를 보지 않는다 — 첫 상대로 만든 입력과 원래 키로 충분하다(파생 키 길이는 위에서 봤다).
    state.rewrite_service.validate_input(
        RewriteInput(user_id=user_id, contact_id=body.contact_ids[0], **shared), idempotency_key
    )

    async def one(contact_id: UUID) -> RewriteBatchItemOut:
        key = f"{idempotency_key}:{contact_id}" if idempotency_key else None
        try:
            inp = RewriteInput(user_id=user_id, contact_id=contact_id, **shared)
            rec = await state.rewrite_service.rewrite(inp, key)
        except DomainError as exc:
            return RewriteBatchItemOut(
                contact_id=contact_id, error=RewriteBatchErrorOut(code=exc.code, message=exc.message)
            )
        except Exception as exc:  # noqa: BLE001 - 한 상대의 장애로 다른 상대의 결과까지 버리지 않는다
            # 예외 문구에는 대화 원문이 섞일 수 있다 — 종류만 남기고, 응답에는 단건 500과 같은 문구를 쓴다.
            log.error("batch rewrite item failed: %s", exception_kind(exc))
            return RewriteBatchItemOut(
                contact_id=contact_id,
                error=RewriteBatchErrorOut(code="internal_error", message=INTERNAL_ERROR_MESSAGE),
            )
        return RewriteBatchItemOut(contact_id=contact_id, rewrite=_to_out(rec))

    # gather는 넘긴 순서대로 결과를 돌려준다 — items의 순서가 요청의 contact_ids 순서와 같다.
    return RewriteBatchOut(items=list(await asyncio.gather(*(one(cid) for cid in body.contact_ids))))


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

    대화방을 지웠으면 그 근거는 나오지 않고 `missing`에 수만 남는다. 출처 참조를 남기기 전에 만든
    옛 결과는 `refs_recorded: false`다 (그때는 `items`가 비고 `missing`은 0).
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
        refs_recorded=ev.refs_recorded,
    )


@router.post("/{request_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def post_feedback(request_id: UUID, body: FeedbackIn, user_id: UserId, state: State) -> None:
    # 고친 문장은 결과를 손본 글이다 — 초안 한도의 두 배까지 받는다 (예전에는 길이 제한이 없었다).
    limit = state.settings.max_draft_chars * EDITED_TEXT_FACTOR
    if body.edited_text is not None and len(body.edited_text) > limit:
        raise InvalidInputError(f"고친 문장은 {limit:,}자 이내여야 합니다.")
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
