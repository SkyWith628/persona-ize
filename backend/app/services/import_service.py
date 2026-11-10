"""과거 대화 가져오기 (project-design §5.1).

업로드 → 크기·형식 검증 → 화자 지정 → 중복 제거 → 묶음 생성 → 임베딩 → 검색 준비.

- 검증에 실패한 메시지가 하나라도 있으면 아무것도 저장하지 않고 422로 어디가 틀렸는지 돌려준다.
  "건너뛰고 성공"은 없다.
- 같은 내용(해시)을 다시 올리면 기존 작업을 돌려준다. 다른 내용인데 메시지 id가 겹치면 겹친 것만 건너뛰고
  수를 보고한다.
- 묶음은 대화방 단위로 다시 만든다 (새 메시지가 앞 맥락이 될 수 있어서). 36개 수준에서는 재임베딩 비용이 작다.
- 그래프 수집은 사용자 단위 직렬(`GraphIngestService.ingest_user`) — 방 사이 시간 순서를 지키기 위해.
- 지금은 요청 안에서 동기로 끝낸다. 응답 모양(202 + job)은 설계대로 두어 뒤에 비동기 작업으로 바꿀 수
  있게 한다.
- 원문 가져오기(`import_parsed`): `app.importers`가 읽은 결과에 화자 지정(me/contact/skip)을 입혀 같은 경로로
  저장한다. 파서가 못 읽은 줄은 미리보기(`POST /v1/imports/parse`)의 `skipped_lines`·`problems`로 보여 주고,
  화자 지정 누락·결과 0건·상한 초과는 아무것도 저장하지 않고 422.
- 개인정보 가림(`mask_pii`)은 API 경로에서만 켠다. 이 서비스를 직접 부르는 평가·합성 적재 스크립트는 기본값
  (가림 없음)이라 결과가 바뀌지 않는다.
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
from app.domain.conversation import Conversation, DeletionResult, ImportJob, NewMessage, SpeakerRole
from app.importers import ParsedImport
from app.importers.pii import mask_pii as mask_text
from app.repositories.base import ContactRepo, ConversationRepo

if TYPE_CHECKING:
    from app.graph.ingest import GraphIngestService

log = logging.getLogger(__name__)

GraphMode = Literal["off", "sync", "background"]
RawSpeakerRole = Literal["me", "contact", "skip"]
"""원문 가져오기의 화자 지정. skip은 그 화자의 메시지를 버린다 (단체방에서 상대 한 명만 남길 때)."""

# 내 이름으로 쓰지 않는 표기 (카카오톡 안드로이드의 '회원님' 등). 그래프 에피소드의 화자 이름으로 부적절하다.
GENERIC_ME_NAMES = {"회원님", "me", "나"}
ME_NAME_MAX = 50  # conversations.me_name 열 길이


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
        text = m.text.replace("\x00", "").strip()  # NUL은 PostgreSQL text에 넣을 수 없다
        if not text:
            problems.append(f"{where}: text가 비어 있습니다")
            continue
        if m.id in seen:
            problems.append(f"{where}: 메시지 id가 중복됩니다")
            continue
        seen.add(m.id)
        out.append(NewMessage(source_key=m.id, speaker_role=role, sent_at=m.sent_at, text=text))
    if unknown_speakers:
        names = ", ".join(sorted(unknown_speakers))
        problems.insert(0, f"화자 지정이 필요합니다. speaker_map에 me/contact를 지정하세요: {names}")
    if problems:
        raise InvalidInputError("가져오기 입력 오류: " + " / ".join(problems[:10]))
    return out


ROLE_NAMES = ("me", "contact")


def mask_messages(messages: list[NewMessage]) -> list[NewMessage]:
    """본문의 개인정보 패턴을 가린 사본 (`app.importers.pii`). id·시각·화자는 그대로."""
    return [m.model_copy(update={"text": mask_text(m.text)[0]}) for m in messages]


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

    async def import_messages(
        self, user_id: str, conversation_id: UUID, payload: ImportPayload, *, mask_pii: bool = False
    ) -> ImportJob:
        """JSON 메시지 가져오기. `mask_pii`는 API 경로만 켠다 (스크립트·평가는 원문 그대로)."""
        conv = await self._conversations.get(user_id, conversation_id)  # 소유권
        if len(payload.messages) > self._max_messages:
            raise InvalidInputError(f"한 번에 {self._max_messages}개까지 가져올 수 있습니다.")
        messages = normalize_messages(payload)
        if mask_pii:
            messages = mask_messages(messages)
        return await self._save(user_id, conv, messages)

    async def import_parsed(
        self,
        user_id: str,
        conversation_id: UUID,
        parsed: ParsedImport,
        speaker_map: dict[str, RawSpeakerRole],
        *,
        keep_recent: int | None = None,
    ) -> ImportJob:
        """파서 결과(`app.importers.parse_import`)에 화자 지정을 입혀 저장한다.

        지도에 없는 화자가 하나라도 있으면 아무것도 저장하지 않고 422(이름 나열). `skip`은 그 화자의 메시지를
        버리고, `keep_recent`는 그 뒤 최근 N건만 남긴다. 결과가 0건이거나 상한을 넘으면 422.
        개인정보 가림은 파서 단계에서 끝나 있다 (미리보기와 저장의 메시지 id가 같도록).
        """
        conv = await self._conversations.get(user_id, conversation_id)  # 소유권
        if parsed.chats is not None:
            names = ", ".join(f"{c.name}(id={c.id}, {c.messages}건)" for c in parsed.chats[:10])
            raise InvalidInputError(f"대화가 여러 개인 내보내기입니다. chat_id로 하나를 골라 주세요: {names}")
        unknown = sorted(
            {
                m.speaker
                for m in parsed.messages
                if m.speaker not in speaker_map and m.speaker not in ROLE_NAMES
            }
        )
        if unknown:
            raise InvalidInputError(
                "화자 지정이 필요합니다. speaker_map에 me/contact/skip을 지정하세요: " + ", ".join(unknown)
            )
        messages: list[NewMessage] = []
        for m in parsed.messages:
            role = speaker_map.get(m.speaker, m.speaker)
            if role in ROLE_NAMES:  # skip은 버린다
                messages.append(
                    NewMessage(source_key=m.id, speaker_role=role, sent_at=m.sent_at, text=m.text)  # type: ignore[arg-type]
                )
        if keep_recent is not None:
            messages = sorted(messages, key=lambda x: x.sent_at)[-keep_recent:]
        if not messages:
            raise InvalidInputError(
                "가져올 메시지가 없습니다. 모든 화자를 건너뛰도록(skip) 지정했는지 확인하세요."
            )
        if len(messages) > self._max_messages:
            raise InvalidInputError(
                f"한 번에 {self._max_messages}개까지 가져올 수 있습니다 (지금 {len(messages)}개). "
                "keep_recent로 최근 메시지만 가져오세요."
            )
        conv = await self._adopt_me_name(user_id, conv, parsed, speaker_map)
        return await self._save(user_id, conv, messages)

    async def _adopt_me_name(
        self, user_id: str, conv: Conversation, parsed: ParsedImport, speaker_map: dict[str, RawSpeakerRole]
    ) -> Conversation:
        """대화방에 내 이름이 없고 첫 가져오기면, 'me'로 지정된 화자 이름을 내 이름으로 쓴다.

        그래프 에피소드 본문이 '사용자' 대신 실제 이름으로 만들어진다. 이미 메시지가 있는 방은 건드리지 않는다
        (앞서 '사용자'로 들어간 에피소드와 이름이 갈리면 그래프에 내가 두 사람으로 생긴다).
        """
        if conv.me_name:
            return conv
        names = [
            name
            for name, _ in parsed.speakers()
            if speaker_map.get(name) == "me" and name not in GENERIC_ME_NAMES and len(name) <= ME_NAME_MAX
        ]
        if len(names) != 1:
            return conv
        if (await self._conversations.conversation_stats(user_id, conv.id)).messages:
            return conv
        return await self._conversations.set_me_name(user_id, conv.id, names[0])

    async def _save(self, user_id: str, conv: Conversation, messages: list[NewMessage]) -> ImportJob:
        """정규화된 메시지를 저장하고 묶음을 다시 만든다. 같은 내용(해시)이면 기존 작업을 돌려준다."""
        digest = source_hash(messages)

        existing = await self._conversations.find_job(user_id, conv.id, digest)
        if existing is not None and existing.status == "completed":
            return existing
        # 묶음 생성이 실패했던 작업(failed)은 같은 내용을 다시 올리면 같은 작업 id로 다시 시도한다.

        inserted, duplicates = await self._conversations.add_messages(user_id, conv.id, messages)
        job = ImportJob(
            id=existing.id if existing is not None else uuid4(),
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
                await self._graph_ingest.ingest_user(user_id, job.id)
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
            await self._graph_ingest.ingest_user(user_id, job_id)
        except Exception as exc:  # noqa: BLE001 - 상태는 ingest가 failed로 기록했다
            log.warning("background graph ingest failed job=%s: %s", job_id, exc)

    async def wait_graph_tasks(self) -> None:
        """테스트·종료용: 떠 있는 백그라운드 수집을 기다린다."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def delete_conversation(self, user_id: str, conversation_id: UUID) -> DeletionResult:
        """원문·묶음·가져오기 작업·그래프 에피소드를 지운다. 삭제 뒤 C/D 검색에 이 대화방이 나오지 않는다.

        그래프를 먼저 지운다 (메시지 목록이 필요하다). 그래프 삭제가 실패해도 DB는 지우고 사유를 돌려준다 —
        사용자의 삭제 요청은 원문 삭제가 우선이고, 그래프 잔여물은 재시도 대상으로 남긴다.
        """
        conv = await self._conversations.get(user_id, conversation_id)  # 소유권
        messages = await self._conversations.list_messages(user_id, conv.id)
        graph_removed = 0
        graph_error: str | None = None
        if self._graph_ingest is not None:
            names = [f"{conv.source_key}:{m.source_key}" for m in messages if m.graph_ingested_at is not None]
            try:
                graph_removed = await self._graph_ingest.remove_episodes(user_id, names)
            except Exception as exc:  # noqa: BLE001 - DB 삭제는 계속한다
                log.warning("graph removal failed conversation=%s: %s", conv.id, exc)
                graph_error = str(exc)[:500]
        deleted_messages, deleted_episodes = await self._conversations.delete_conversation(user_id, conv.id)
        return DeletionResult(
            conversation_id=conv.id,
            messages=deleted_messages,
            episodes=deleted_episodes,
            graph_episodes=graph_removed,
            graph_error=graph_error,
        )

    async def reindex(self, user_id: str, conv: Conversation) -> int:
        """대화방의 묶음을 전부 다시 만들고 임베딩한다. 임베딩 모델을 바꿨을 때도 이걸 돌린다."""
        all_messages = await self._conversations.list_messages(user_id, conv.id)
        drafts = build_episodes(all_messages, conv.source_key, self._window)
        vectors = await self._embedder.embed_passages([d.context_text for d in drafts])
        return await self._conversations.replace_episodes(
            user_id, conv.id, conv.contact_id, drafts, vectors, self._embedder.version
        )
