"""내 데이터 API — 원문 파싱 미리보기, 데이터 요약, 내보내기, 관계별 기본 페르소나.

경로는 접두사 없이 전체를 적는다:
`/v1/imports/parse`, `/v1/data/summary`, `/v1/export`, `/v1/persona-defaults`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.deps import AppState, State, UserId
from app.api.schemas import (
    ContactSummaryOut,
    DataSummaryOut,
    DataTotalsOut,
    ImportChatOut,
    ImportParseIn,
    ImportPreviewOut,
    ImportSampleOut,
    ImportSpeakerOut,
)
from app.domain.persona import DEFAULT_PERSONAS, PersonaSettings
from app.importers import ParsedImport, parse_import

router = APIRouter(tags=["data"])

SAMPLE_SIZE = 20
EXPORT_REWRITE_LIMIT = 100_000  # 내보내기에서 상대 한 명당 읽는 첨삭 이력 상한


def _preview(parsed: ParsedImport, max_messages: int) -> ImportPreviewOut:
    times = [m.sent_at for m in parsed.messages]
    problems = list(parsed.problems)
    if len(parsed.messages) > max_messages:
        problems.append(
            f"메시지가 {len(parsed.messages):,}건입니다. 한 번에 {max_messages:,}건까지 가져올 수 있어 "
            "최근 메시지만 가져오도록 줄여야 합니다 (keep_recent)."
        )
    return ImportPreviewOut(
        format=parsed.format,
        variant=parsed.variant,
        title_hint=parsed.title_hint,
        messages_total=len(parsed.messages),
        date_from=min(times, default=None),
        date_to=max(times, default=None),
        speakers=[
            ImportSpeakerOut(name=name, count=count, suggested_role=parsed.suggested_roles.get(name))
            for name, count in parsed.speakers()
        ],
        sample=[
            ImportSampleOut(id=m.id, speaker=m.speaker, sent_at=m.sent_at, text=m.text)
            for m in parsed.messages[:SAMPLE_SIZE]
        ],
        skipped_lines=parsed.skipped_lines,
        system_lines=parsed.system_lines,
        masked_pii=parsed.masked_pii,
        problems=problems,
        max_messages=max_messages,
        chats=(
            [ImportChatOut(id=c.id, name=c.name, type=c.type, messages=c.messages) for c in parsed.chats]
            if parsed.chats is not None
            else None
        ),
    )


@router.post("/v1/imports/parse", response_model=ImportPreviewOut)
async def parse_import_preview(body: ImportParseIn, user_id: UserId, state: State) -> ImportPreviewOut:
    """내보내기 원문을 읽어 미리보기만 돌려준다 (저장하지 않는다).

    형식: 카카오톡 내보내기(.txt·.csv), 텔레그램 `result.json`, `날짜 시각 | 화자 | 내용` 붙여넣기,
    메시지 배열.
    못 읽은 줄은 `skipped_lines`·`problems`에 그대로 드러낸다. 한 건도 못 읽으면 422.
    저장은 같은 본문과 화자 지정으로 `POST /v1/conversations/{id}/imports/raw`.
    """
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
    return _preview(parsed, settings.max_import_messages)


@router.get("/v1/persona-defaults", response_model=dict[str, PersonaSettings])
async def persona_defaults(user_id: UserId) -> dict[str, PersonaSettings]:
    """관계 유형(7종)별 기본 페르소나. 상대 생성의 `use_default_persona`가 쓰는 값과 같다."""
    return DEFAULT_PERSONAS


@router.get("/v1/data/summary", response_model=DataSummaryOut)
async def data_summary(user_id: UserId, state: State) -> DataSummaryOut:
    """상대별로 쌓인 자료(대화방·메시지·그래프 수집·첨삭 이력) 요약. 상대 수와 무관하게 집계 쿼리 몇 번."""
    contacts = await state.contacts.list(user_id)
    versions = await state.contacts.latest_persona_versions(user_id)
    data = await state.conversations.contact_stats(user_id)
    rewrites = await state.rewrites.stats_by_contact(user_id)
    rows: list[ContactSummaryOut] = []
    for c in contacts:
        st = data.get(c.id)
        count, last = rewrites.get(c.id, (0, None))
        rows.append(
            ContactSummaryOut(
                contact_id=c.id,
                alias=c.alias,
                relationship_type=c.relationship_type,
                persona_version=versions.get(c.id),
                conversations=st.conversations if st else 0,
                messages=st.messages if st else 0,
                my_messages=st.my_messages if st else 0,
                graph_ingested=st.graph_ingested if st else 0,
                rewrites=count,
                last_rewrite_at=last,
            )
        )
    return DataSummaryOut(
        contacts=rows,
        totals=DataTotalsOut(
            contacts=len(rows),
            conversations=sum(r.conversations for r in rows),
            messages=sum(r.messages for r in rows),
            rewrites=sum(r.rewrites for r in rows),
        ),
        graph_enabled=state.graph_enabled,
        retention_days=state.settings.rewrite_retention_days,
    )


async def _export_user(state: AppState, user_id: str) -> dict[str, Any]:
    """계정 정보. 개발용 헤더 스텁 사용자는 users 행이 없어 이름이 없다."""
    info: dict[str, Any] = {"user_id": user_id, "username": None, "created_at": None}
    try:
        user = await state.auth.get_user(UUID(user_id))
    except Exception:  # noqa: BLE001 - UUID가 아니거나(스텁) 없는 사용자
        return info
    info.update(username=user.username, created_at=user.created_at.isoformat())
    return info


@router.get("/v1/export")
async def export_my_data(user_id: UserId, state: State) -> JSONResponse:
    """내 데이터 전체를 JSON 파일로 내려받는다 (상대·페르소나 버전·대화방·메시지·첨삭 이력·피드백).

    임베딩 벡터와 그래프(Neo4j)의 파생 데이터는 넣지 않는다 — 원문에서 다시 만들 수 있다.
    첨삭 이력은 저장된 결과 그대로 나간다. 요청의 초안은 따로 저장하지 않지만(요청 행에는 해시만),
    결과에 초안의 일부가 들어 있다: 보존 항목(초안에서 뽑은 날짜·수량 등)과, 추천에 실패해 원문을
    유지한 결과의 본문(초안 그대로)이다. 추천에 성공한 결과에는 초안 전문이 없다.
    """
    now = datetime.now(UTC)
    contacts_out: list[dict[str, Any]] = []
    for c in await state.contacts.list(user_id):
        conversations_out: list[dict[str, Any]] = []
        for conv in await state.conversations.list(user_id, c.id):
            messages = await state.conversations.list_messages(user_id, conv.id)
            conversations_out.append(
                {
                    "id": str(conv.id),
                    "title": conv.title,
                    "source": conv.source,
                    "source_key": conv.source_key,
                    "me_name": conv.me_name,
                    "created_at": conv.created_at.isoformat(),
                    "messages": [
                        {
                            "id": m.source_key,
                            "speaker_role": m.speaker_role,
                            "speaker_name": m.speaker_name,
                            "sent_at": m.sent_at.isoformat(),
                            "text": m.text,
                        }
                        for m in messages
                    ],
                }
            )
        personas = await state.contacts.list_personas(user_id, c.id, limit=1000)
        rewrites = await state.rewrites.list(user_id, c.id, EXPORT_REWRITE_LIMIT)
        contacts_out.append(
            {
                "id": str(c.id),
                "alias": c.alias,
                "relationship_type": c.relationship_type,
                "created_at": c.created_at.isoformat(),
                "persona_versions": [
                    {
                        "version": pv.version,
                        "settings": pv.settings.model_dump(mode="json"),
                        "created_at": pv.created_at.isoformat(),
                    }
                    for pv in personas
                ],
                "conversations": conversations_out,
                "rewrites": [
                    {
                        "id": str(r.id),
                        "draft_version": r.draft_version,
                        "condition": r.condition,
                        "channel": r.channel,
                        "created_at": r.created_at.isoformat(),
                        "result": r.result.model_dump(mode="json"),
                        "feedback": r.feedback.model_dump(mode="json", exclude={"request_id"})
                        if r.feedback
                        else None,
                    }
                    for r in rewrites
                ],
            }
        )
    body = {
        "exported_at": now.isoformat(),
        "user": await _export_user(state, user_id),
        "contacts": contacts_out,
    }
    local = now.astimezone(timezone(timedelta(hours=state.settings.usage_day_offset_hours)))
    filename = f"persona-ize-export-{local:%Y%m%d}.json"
    return JSONResponse(content=body, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
