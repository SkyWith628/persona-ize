"""과거 대화 가져오기 (project-design §5.1).

업로드 → 크기·형식 검증 → 화자 지정 → 중복 제거 → 묶음 생성 → 임베딩 → 검색 준비.

- 검증에 실패한 메시지가 하나라도 있으면 아무것도 저장하지 않고 422로 어디가 틀렸는지 돌려준다.
  "건너뛰고 성공"은 없다.
- 같은 내용(해시)을 다시 올리면 기존 작업을 돌려준다. 다른 내용인데 메시지 id가 겹치면 겹친 것만 건너뛰고
  수를 보고한다.
- 묶음은 대화방 단위로 다시 만든다 (새 메시지가 앞 맥락이 될 수 있어서). 36개 수준에서는 재임베딩 비용이 작다.
- 지금은 요청 안에서 동기로 끝낸다. 응답 모양(202 + job)은 설계대로 두어 뒤에 비동기 작업으로 바꿀 수
  있게 한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.ai.embeddings import Embedder
from app.ai.episodes import DEFAULT_WINDOW, build_episodes
from app.core.errors import InvalidInputError
from app.domain.conversation import Conversation, ImportJob, NewMessage, SpeakerRole
from app.repositories.base import ContactRepo, ConversationRepo

if TYPE_CHECKING:
    from app.graph.ingest import GraphIngestService

log = logging.getLogger(__name__)

GraphMode = Literal["off", "sync", "background"]


class ImportMessage(BaseModel):
    """업로드 형식의 메시지 한 줄 (`data/synthetic/README.md`와 같은 모양)."""

    id: str = Field(min_length=1, max_length=64)
    speaker: str = Field(min_length=1, max_length=64, description="me / contact 또는 speaker_map의 이름")
    sent_at: datetime
    text: str = Field(max_length=4000)


class ImportPayload(BaseModel):
    source: str = Field("json", max_length=64)
    speaker_map: dict[str, SpeakerRole] = Field(
        default_factory=dict, description="화자 지정: 원본 이름 → me/contact (예: {'김민수': 'me'})"
    )
    messages: list[ImportMessage] = Field(min_length=1)


def normalize_messages(payload: ImportPayload) -> list[NewMessage]:
    """화자 지정·시간대·중복 id를 검사해 저장 형식으로 바꾼다. 문제는 모아서 한 번에 422."""
    problems: list[str] = []
    unknown_speakers: set[str] = set()
    seen: set[str] = set()
    out: list[NewMessage] = []
    for idx, m in enumerate(payload.messages):
        where = f"messages[{idx}] (id={m.id})"
        role: SpeakerRole | None
        if m.speaker in ("me", "contact"):
            role = m.speaker  # type: ignore[assignment]
        else:
            role = payload.speaker_map.get(m.speaker)
            if role is None:
                unknown_speakers.add(m.speaker)
                continue
        if m.sent_at.tzinfo is None:
            problems.append(f"{where}: sent_at에 시간대가 없습니다 (예: +09:00)")
            continue
        if not m.text.strip():
            problems.append(f"{where}: text가 비어 있습니다")
            continue
        if m.id in seen:
            problems.append(f"{where}: 메시지 id가 중복됩니다")
            continue
        seen.add(m.id)
        out.append(NewMessage(source_key=m.id, speaker_role=role, sent_at=m.sent_at, text=m.text.strip()))
    if unknown_speakers:
        names = ", ".join(sorted(unknown_speakers))
        problems.insert(0, f"화자 지정이 필요합니다. speaker_map에 me/contact를 지정하세요: {names}")
    if problems:
        raise InvalidInputError("가져오기 입력 오류: " + " / ".join(problems[:10]))
    return out


def source_hash(messages: list[NewMessage]) -> str:
    canonical = sorted(
        (m.sent_at.astimezone(UTC).isoformat(), m.source_key, m.speaker_role, m.text) for m in messages
    )
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode("utf-8")).hexdigest()


class ImportService:
    def __init__(
        self,
        contacts: ContactRepo,
        conversations: ConversationRepo,
        embedder: Embedder,
        *,
        max_messages: int = 5000,
        window: int = DEFAULT_WINDOW,
        graph_ingest: GraphIngestService | None = None,
        graph_mode: GraphMode = "off",
    ) -> None:
        self._contacts = contacts
        self._conversations = conversations
        self._embedder = embedder
        self._max_messages = max_messages
        self._window = window
        self._graph_ingest = graph_ingest
        # off: 그래프 안 씀 / sync: 요청 안에서 수집(스크립트·평가) / background: 응답 뒤 수집(API)
        self.graph_mode: GraphMode = graph_mode if graph_ingest is not None else "off"
        self._tasks: set[asyncio.Task[None]] = set()

    async def create_conversation(
        self,
        user_id: str,
        contact_id: UUID,
        title: str | None,
        source: str,
        source_key: str | None,
        me_name: str | None = None,
    ) -> Conversation:
        contact = await self._contacts.get(user_id, contact_id)  # 소유권
        key = (source_key or f"conv-{uuid4().hex[:8]}").strip()
        return await self._conversations.create(
            user_id, contact.id, (title or f"{contact.alias}와의 대화").strip(), source, key, me_name
        )

    async def import_messages(self, user_id: str, conversation_id: UUID, payload: ImportPayload) -> ImportJob:
        conv = await self._conversations.get(user_id, conversation_id)  # 소유권
        if len(payload.messages) > self._max_messages:
            raise InvalidInputError(f"한 번에 {self._max_messages}개까지 가져올 수 있습니다.")
        messages = normalize_messages(payload)
        digest = source_hash(messages)

        existing = await self._conversations.find_job(user_id, conv.id, digest)
        if existing is not None:
            return existing

        inserted, duplicates = await self._conversations.add_messages(user_id, conv.id, messages)
        job = ImportJob(
            id=uuid4(),
            user_id=user_id,
            conversation_id=conv.id,
            source_hash=digest,
            status="completed",
            imported=inserted,
            duplicates=duplicates,
            created_at=datetime.now(UTC),
        )
        try:
            job.episodes = await self.reindex(user_id, conv)
        except Exception as exc:  # noqa: BLE001 - 임베딩·저장 장애는 작업 실패로 기록하고 재시도 가능하게 둔다
            log.warning("import reindex failed conversation=%s: %s", conv.id, exc)
            job.status = "failed"
            job.error_code = "reindex_failed"
            job.error_message = str(exc)[:500]
        if self.graph_mode != "off" and job.status == "completed" and inserted > 0:
            job.graph_status = "pending"
        job = await self._conversations.save_job(job)
        if job.graph_status == "pending":
            job = await self._start_graph_ingest(user_id, conv.id, job)
        return job

    async def _start_graph_ingest(self, user_id: str, conversation_id: UUID, job: ImportJob) -> ImportJob:
        assert self._graph_ingest is not None
        if self.graph_mode == "sync":
            try:
                await self._graph_ingest.ingest_conversation(user_id, conversation_id, job.id)
            except Exception as exc:  # noqa: BLE001 - 작업 상태에 failed로 남아 있다
                log.warning("sync graph ingest failed: %s", exc)
            return await self._conversations.get_job(user_id, job.id)
        task = asyncio.create_task(self._graph_ingest_background(user_id, conversation_id, job.id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _graph_ingest_background(self, user_id: str, conversation_id: UUID, job_id: UUID) -> None:
        assert self._graph_ingest is not None
        try:
            await self._graph_ingest.ingest_conversation(user_id, conversation_id, job_id)
        except Exception as exc:  # noqa: BLE001 - 상태는 ingest가 failed로 기록했다
            log.warning("background graph ingest failed job=%s: %s", job_id, exc)

    async def wait_graph_tasks(self) -> None:
        """테스트·종료용: 떠 있는 백그라운드 수집을 기다린다."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def reindex(self, user_id: str, conv: Conversation) -> int:
        """대화방의 묶음을 전부 다시 만들고 임베딩한다. 임베딩 모델을 바꿨을 때도 이걸 돌린다."""
        all_messages = await self._conversations.list_messages(user_id, conv.id)
        drafts = build_episodes(all_messages, conv.source_key, self._window)
        vectors = await self._embedder.embed_passages([d.context_text for d in drafts])
        return await self._conversations.replace_episodes(
            user_id, conv.id, conv.contact_id, drafts, vectors, self._embedder.version
        )
