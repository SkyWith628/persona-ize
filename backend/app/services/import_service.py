"""과거 대화 가져오기 (project-design §5.1).

업로드 → 크기·형식 검증 → 화자 지정 → 중복 제거 → 묶음 생성 → 임베딩 → 검색 준비.

- 검증에 실패한 메시지가 하나라도 있으면 아무것도 저장하지 않고 422로 어디가 틀렸는지 돌려준다.
  "건너뛰고 성공"은 없다.
- 같은 내용(해시)을 다시 올리면 기존 작업을 돌려준다. 다른 내용인데 메시지 id가 겹치면 겹친 것만 건너뛰고
  수를 보고한다.
- 묶음은 대화방 단위로 다시 만든다 (새 메시지가 앞 맥락이 될 수 있어서). 36개 수준에서는 재임베딩 비용이 작다.
- 그래프 수집은 사용자 단위 직렬(`GraphIngestService.ingest_user`) — 방 사이 시간 순서를 지키기 위해.
- 메시지 저장은 요청 안에서 끝내고, 묶음 생성·임베딩은 작업(`_index_job`)으로 돌린다. `index_wait`가
  None(스크립트·평가·테스트 기본)이면 끝날 때까지 기다려 예전과 같고, 초 단위 값(API)이면 그만큼만 기다린 뒤
  `processing` 작업을 돌려준다 — 나머지는 뒤에서 끝나고 `GET /v1/imports/{id}`로 확인한다. 서버가 도중에
  죽어 `processing`으로 남은 작업은 다음 시작 때 `resume_indexing`이 이어받는다.
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
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from app.ai.embeddings import Embedder
from app.ai.episodes import DEFAULT_WINDOW, build_episodes
from app.core.errors import InvalidInputError, NotFoundError, VersionConflictError
from app.domain.conversation import (
    Conversation,
    DeletionResult,
    ImportJob,
    NewMessage,
    SpeakerRole,
    exception_kind,
)
from app.domain.usage import QuotaExceededError
from app.importers import ParsedImport
from app.importers.base import MAX_YEAR, MIN_YEAR, has_unstorable_text, in_range, repair_unicode
from app.importers.pii import mask_pii as mask_text
from app.repositories.base import ContactRepo, ConversationRepo
from app.services.locks import KeyedLocks

if TYPE_CHECKING:
    from app.graph.ingest import GraphIngestService

log = logging.getLogger(__name__)

GraphMode = Literal["off", "sync", "background"]
RawSpeakerRole = Literal["me", "contact", "skip"]
"""원문 가져오기의 화자 지정. skip은 그 화자의 메시지를 버린다 (단체방에서 상대 한 명만 남길 때)."""

# 내 이름으로 쓰지 않는 표기 (카카오톡 안드로이드의 '회원님' 등). 그래프 에피소드의 화자 이름으로 부적절하다.
GENERIC_ME_NAMES = {"회원님", "me", "나"}
ME_NAME_MAX = 50  # conversations.me_name 열 길이
OFFLOAD_CHARS = 50_000  # 본문 합계가 이보다 크면 정규화·가림을 스레드에서 한다


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

    @model_validator(mode="before")
    @classmethod
    def _readable(cls, data: Any) -> Any:
        """저장할 수 없는 문자(NUL·깨진 유니코드)를 모델 검증보다 먼저 본다.

        본문(`text`)의 깨진 글자는 `?`로 바꿔 읽고(NUL은 `normalize_messages`가 뺀다), id·화자·시각 등
        그 밖의 값에 있으면 어느 항목인지 알려 주며 돌려보낸다. 그대로 두면 DB 오류(500)가 났다.
        """
        if not isinstance(data, dict):
            return data
        if has_unstorable_text({k: v for k, v in data.items() if k != "messages"}):
            raise ValueError("speaker_map·source에 저장할 수 없는 문자가 있습니다 (NUL 또는 깨진 유니코드).")
        messages = data.get("messages")
        if not isinstance(messages, list):
            return data
        repaired: list[Any] | None = None
        for idx, item in enumerate(messages):
            if not isinstance(item, dict):
                continue
            if has_unstorable_text([v for k, v in item.items() if k != "text"]):
                raise ValueError(
                    f"messages[{idx}]: id·speaker·sent_at에 저장할 수 없는 문자가 있습니다 "
                    "(NUL 또는 깨진 유니코드)."
                )
            text = item.get("text")
            if isinstance(text, str) and repair_unicode(text) is not text:
                repaired = repaired if repaired is not None else list(messages)
                repaired[idx] = {**item, "text": repair_unicode(text)}
        return data if repaired is None else {**data, "messages": repaired}


def normalize_messages(payload: ImportPayload) -> list[NewMessage]:
    """화자 지정·시간대·중복 id를 검사해 저장 형식으로 바꾼다. 문제는 모아서 한 번에 422."""
    problems: list[str] = []
    unknown_speakers: set[str] = set()
    seen: set[str] = set()
    out: list[NewMessage] = []
    for idx, m in enumerate(payload.messages):
        if has_unstorable_text(m.id):  # id는 열쇠라 고쳐 쓰지 않고 돌려보낸다
            problems.append(f"messages[{idx}]: 메시지 id에 저장할 수 없는 문자가 있습니다 (NUL 등)")
            continue
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
        if not in_range(m.sent_at):  # 0001년·9999년은 시간대 변환이 넘쳐 500이 났다
            problems.append(f"{where}: sent_at이 {MIN_YEAR}~{MAX_YEAR}년을 벗어납니다")
            continue
        # NUL과 짝 없는 대리 문자는 PostgreSQL text에 넣을 수 없다 — NUL은 빼고, 깨진 글자는 ?로 바꾼다
        # (원문 파서의 `clean_text`와 같은 처리). 멀쩡한 글은 그대로다.
        text = m.text.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8").strip()
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


def _prepare_messages(payload: ImportPayload, mask: bool) -> list[NewMessage]:
    """검증·정규화와 (API 경로면) 가림. 스레드에서 돈다."""
    messages = normalize_messages(payload)
    return mask_messages(messages) if mask else messages


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
        index_wait: float | None = None,
    ) -> None:
        self._contacts = contacts
        self._conversations = conversations
        self._embedder = embedder
        self._max_messages = max_messages
        self._window = window
        self._graph_ingest = graph_ingest
        # off: 그래프 안 씀 / sync: 요청 안에서 수집(스크립트·평가) / background: 응답 뒤 수집(API)
        self.graph_mode: GraphMode = graph_mode if graph_ingest is not None else "off"
        # None: 묶음 생성이 끝날 때까지 기다린다 / 초: 그만큼만 기다리고 나머지는 뒤에서 (API)
        self._index_wait = index_wait
        self._tasks: set[asyncio.Task[None]] = set()
        self._index_tasks: dict[UUID, asyncio.Task[ImportJob]] = {}  # 작업 id → 묶음 생성 중인 태스크
        self._conv_locks = KeyedLocks()  # 대화방 id → 저장·묶음 재생성·삭제를 한 번에 하나씩

    async def create_conversation(
        self,
        user_id: str,
        contact_id: UUID,
        title: str | None,
        source: str,
        source_key: str | None,
        me_name: str | None = None,
        *,
        max_per_user: int | None = None,
    ) -> Conversation:
        """대화방을 만든다. `max_per_user`(API 경로의 `MAX_CONVERSATIONS_PER_USER`)를 넘으면 422."""
        contact = await self._contacts.get(user_id, contact_id)  # 소유권
        if max_per_user is not None:
            owned, _ = await self._conversations.count_owned(user_id)
            if owned >= max_per_user:
                raise QuotaExceededError(
                    f"대화방은 계정당 {max_per_user:,}개까지 만들 수 있습니다 (지금 {owned:,}개). "
                    "쓰지 않는 대화방을 지운 뒤 다시 시도하세요."
                )
        key = (source_key or f"conv-{uuid4().hex[:8]}").strip()
        # '회원님'(안드로이드 내보내기의 본인 표기) 같은 말은 내 이름이 아니다. 그대로 두면 그래프 에피소드의
        # 화자가 '회원님'이 된다 — 비워 두면 첫 가져오기의 화자 지정이나 기본 이름('사용자')을 쓴다.
        name = (me_name or "").strip()
        return await self._conversations.create(
            user_id,
            contact.id,
            (title or f"{contact.alias}와의 대화").strip(),
            source,
            key,
            name if name and name not in GENERIC_ME_NAMES else None,
        )

    async def import_messages(
        self,
        user_id: str,
        conversation_id: UUID,
        payload: ImportPayload,
        *,
        mask_pii: bool = False,
        max_user_messages: int | None = None,
    ) -> ImportJob:
        """JSON 메시지 가져오기. `mask_pii`와 `max_user_messages`(계정당 보관 한도)는 API 경로만 준다.

        스크립트·평가는 둘 다 주지 않는다 (원문 그대로, 한도 없음).
        """
        conv = await self._conversations.get(user_id, conversation_id)  # 소유권
        if len(payload.messages) > self._max_messages:
            raise InvalidInputError(f"한 번에 {self._max_messages}개까지 가져올 수 있습니다.")
        # 정규화·가림은 CPU 작업이다(8MB 본문이면 수백 ms) — 큰 본문은 이벤트 루프 밖에서 돌려 다른 요청을
        # 막지 않는다. 작은 본문은 그 자리에서 한다 (스레드를 오가는 비용이 더 크다).
        if sum(len(m.text) for m in payload.messages) > OFFLOAD_CHARS:
            messages = await asyncio.to_thread(_prepare_messages, payload, mask_pii)
        else:
            messages = _prepare_messages(payload, mask_pii)
        return await self._save(user_id, conv, messages, max_user_messages=max_user_messages)

    async def import_parsed(
        self,
        user_id: str,
        conversation_id: UUID,
        parsed: ParsedImport,
        speaker_map: dict[str, RawSpeakerRole],
        *,
        keep_recent: int | None = None,
        max_user_messages: int | None = None,
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
        return await self._save(
            user_id,
            conv,
            messages,
            me_name_from=(parsed, speaker_map),
            max_user_messages=max_user_messages,
        )

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

    async def _save(
        self,
        user_id: str,
        conv: Conversation,
        messages: list[NewMessage],
        *,
        me_name_from: tuple[ParsedImport, dict[str, RawSpeakerRole]] | None = None,
        max_user_messages: int | None = None,
    ) -> ImportJob:
        """정규화된 메시지를 저장하고 묶음을 다시 만든다. 같은 내용(해시)이면 기존 작업을 돌려준다.

        저장과 묶음 다시 만들기는 각각 **대화방 단위로 한 번에 하나씩** 한다. 같은 방에 가져오기 두 건이
        겹치면 서로의 묶음 교체가 엇갈려 묶음이 두 벌 남았고, 같은 파일을 동시에 올리면 유일 제약에 걸려
        한쪽이 500이었다 (2026-11-11 검토). 그래프 수집은 잠금 밖에서 시작한다 (오래 걸린다).
        """
        digest = source_hash(messages)
        if max_user_messages is None:
            job, start_graph, task = await self._save_locked(user_id, conv, messages, digest, me_name_from)
        else:
            # 계정당 보관 한도: 세고 넣는 사이에 같은 사용자의 다른 가져오기가 끼어들지 못하게 한다.
            async with self._conv_locks.hold(("quota", user_id)):
                await self._check_message_quota(user_id, conv, messages, max_user_messages)
                job, start_graph, task = await self._save_locked(
                    user_id, conv, messages, digest, me_name_from
                )
        if task is not None:
            return await self._await_index(task, job)
        if start_graph:
            job = await self._start_graph_ingest(user_id, conv.id, job)
        return job

    async def _await_index(self, task: asyncio.Task[ImportJob], job: ImportJob) -> ImportJob:
        """묶음 생성 태스크를 기다린다. 기다리는 시간을 넘기면 `processing` 작업을 그대로 돌려준다.

        요청이 끊겨도(취소) 태스크는 계속 돈다 — 메시지는 이미 저장됐고 묶음만 남았다.
        """
        if self._index_wait is None:
            return await asyncio.shield(task)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self._index_wait)
        except TimeoutError:
            return job

    async def _save_locked(
        self,
        user_id: str,
        conv: Conversation,
        messages: list[NewMessage],
        digest: str,
        me_name_from: tuple[ParsedImport, dict[str, RawSpeakerRole]] | None,
    ) -> tuple[ImportJob, bool, asyncio.Task[ImportJob] | None]:
        async with self._conv_locks.hold(conv.id):
            if me_name_from is not None:  # 첫 가져오기인지는 잠금 안에서 봐야 한다
                conv = await self._adopt_me_name(user_id, conv, *me_name_from)
            return await self._store(user_id, conv, messages, digest)

    async def _check_message_quota(
        self, user_id: str, conv: Conversation, messages: list[NewMessage], limit: int
    ) -> None:
        """이번에 **새로** 들어갈 메시지까지 합쳐 계정당 한도를 넘으면 아무것도 저장하지 않고 422.

        이미 있는 메시지(같은 파일을 다시 올림)는 세지 않는다.
        """
        existing = await self._conversations.message_keys(user_id, conv.id)
        new = sum(1 for m in messages if m.source_key not in existing)
        if new == 0:
            return
        _, total = await self._conversations.count_owned(user_id)
        if total + new > limit:
            raise QuotaExceededError(
                f"가져온 메시지는 계정당 {limit:,}건까지 보관할 수 있습니다 "
                f"(지금 {total:,}건, 이번에 새로 {new:,}건). 오래된 대화방을 지우거나 "
                "keep_recent로 최근 메시지만 가져오세요."
            )

    async def _store(
        self, user_id: str, conv: Conversation, messages: list[NewMessage], digest: str
    ) -> tuple[ImportJob, bool, asyncio.Task[ImportJob] | None]:
        """(작업, 그래프 수집을 시작할 것인가, 묶음 생성 태스크). 대화방 잠금 안에서 부른다.

        메시지를 넣고 `processing` 작업을 남긴 뒤 묶음 생성 태스크를 띄운다 — 태스크는 이 잠금이 풀린 뒤
        같은 잠금을 다시 잡고 돈다. 태스크가 있으면 그래프 수집은 태스크가 끝에서 시작한다.
        """
        existing = await self._conversations.find_job(user_id, conv.id, digest)
        if existing is not None and existing.status == "completed":
            # 저장은 끝났다. 그래프 수집만 덜 됐으면(상한에 걸렸거나 실패) 같은 파일을 다시 올렸을 때
            # 남은 것을 이어서 넣는다 — 작업의 graph_episodes는 이번 실행에서 넣은 수가 된다.
            return existing, await self._should_retry_graph(user_id, existing), None
        resumed = existing is not None and existing.status == "processing"
        if resumed:
            running = self._index_tasks.get(existing.id)
            if running is not None and not running.done():
                return existing, False, running  # 같은 파일을 또 올렸다 — 돌고 있는 것을 같이 기다린다
        # 묶음 생성이 실패했던 작업(failed)이나 서버가 죽어 processing으로 남은 작업은 같은 내용을 다시
        # 올리면 같은 작업 id로 다시 시도한다.

        inserted, duplicates = await self._conversations.add_messages(user_id, conv.id, messages)
        if resumed:
            # 처음 올렸을 때 넣은 수를 잃지 않는다 (다시 올린 지금은 전부 중복으로 세어진다)
            inserted, duplicates = existing.imported + inserted, existing.duplicates
        job = ImportJob(
            id=existing.id if existing is not None else uuid4(),
            user_id=user_id,
            conversation_id=conv.id,
            source_hash=digest,
            status="processing",
            imported=inserted,
            duplicates=duplicates,
            # 묶음 생성 뒤에 이어질 그래프 수집을 미리 '대기'로 보여 준다 (화면이 '안 씀'으로 읽지 않게)
            graph_status="pending" if self.graph_mode != "off" and inserted > 0 else "skipped",
            created_at=datetime.now(UTC),
        )
        try:
            job = await self._conversations.save_job(job)
        except VersionConflictError:
            # 다른 서버 프로세스가 같은 내용을 방금 저장했다 (프로세스 안에서는 잠금이 막는다).
            # 메시지는 이미 들어가 있으니 그쪽 작업을 돌려준다.
            other = await self._conversations.find_job(user_id, conv.id, digest)
            if other is None:
                raise
            return other, False, None
        return job, False, self._start_index(user_id, conv, job)

    def _start_index(self, user_id: str, conv: Conversation, job: ImportJob) -> asyncio.Task[ImportJob]:
        task = asyncio.create_task(self._index_job(user_id, conv, job), name=f"import-index-{job.id}")
        self._index_tasks[job.id] = task

        def _done(t: asyncio.Task[ImportJob]) -> None:
            if self._index_tasks.get(job.id) is t:
                del self._index_tasks[job.id]
            if t.cancelled():
                return
            exc = t.exception()  # 기다리는 요청이 없어도 "never retrieved" 경고가 남지 않게 꺼낸다
            if exc is not None and not isinstance(exc, NotFoundError):
                log.warning("import index task failed job=%s: %s", job.id, exception_kind(exc))

        task.add_done_callback(_done)
        return task

    async def _index_job(self, user_id: str, conv: Conversation, job: ImportJob) -> ImportJob:
        """묶음을 다시 만들고 작업을 completed/failed로 닫는다. 끝나면 그래프 수집을 시작한다.

        묶음 생성은 대화방마다 한 번에 하나씩 돈다(`("index", 방)` 잠금) — 늦게 시작한 쪽이 더 새 메시지를
        읽으므로 차례만 지키면 마지막 묶음이 맞다. 저장·삭제가 쓰는 대화방 잠금은 임베딩 동안 쥐지 않는다
        (몇 분씩 걸릴 수 있어 그동안 같은 방의 다른 가져오기와 삭제가 멈추면 안 된다).
        그 사이 대화방이 지워졌으면 NotFoundError — 작업도 함께 지워졌으니 남길 것이 없다.
        """
        async with self._conv_locks.hold(("index", conv.id)):
            job = job.model_copy()
            try:
                job.episodes = await self.reindex(user_id, conv)
                job.status = "completed"
            except NotFoundError:
                raise  # 저장하는 사이에 대화방이 지워졌다 — 남길 작업도 없다 (404)
            except Exception as exc:  # noqa: BLE001 - 임베딩·저장 장애는 작업 실패로 기록하고 재시도 가능하게 둔다
                log.warning("import reindex failed conversation=%s: %s", conv.id, exception_kind(exc))
                job.status = "failed"
                job.error_code = "reindex_failed"
                job.error_message = exception_kind(exc)  # 종류만 — 문구에는 원문이 섞일 수 있다
            if job.status != "completed":
                job.graph_status = "skipped"  # 묶음이 없으면 그래프 수집도 시작하지 않는다
            async with self._conv_locks.hold(conv.id):
                await self._conversations.get(user_id, conv.id)  # 지워진 방에 작업을 되살리지 않는다
                job = await self._conversations.save_job(job)
        if job.graph_status == "pending":
            job = await self._start_graph_ingest(user_id, conv.id, job)
        return job

    async def resume_indexing(self) -> int:
        """서버가 도중에 죽어 `processing`으로 남은 작업의 묶음 생성을 다시 시작한다. 시작한 수.

        모델 호출이 없는 작업이다(로컬 임베딩). 서버 시작 때 한 번 부른다.
        """
        started = 0
        for job in await self._conversations.processing_jobs():
            running = self._index_tasks.get(job.id)
            if running is not None and not running.done():
                continue
            try:
                conv = await self._conversations.get(job.user_id, job.conversation_id)
            except NotFoundError:
                continue
            self._start_index(job.user_id, conv, job)
            started += 1
        return started

    async def _should_retry_graph(self, user_id: str, job: ImportJob) -> bool:
        """끝난 가져오기를 다시 올렸을 때 그래프 수집을 다시 시도할 것인가.

        상한에 걸렸던 작업은 지금 예산이 있을 때만(없으면 상태를 그대로 둔다), 실패했던 작업은 언제나.
        """
        if self.graph_mode == "off" or self._graph_ingest is None:
            return False
        if job.graph_status == "failed":
            return True
        return job.graph_status == "limited" and not await self._graph_ingest.is_limited(user_id)

    async def _start_graph_ingest(self, user_id: str, conversation_id: UUID, job: ImportJob) -> ImportJob:
        assert self._graph_ingest is not None
        if self.graph_mode == "sync":
            try:
                await self._graph_ingest.ingest_user(user_id, job.id)
            except Exception as exc:  # noqa: BLE001 - 작업 상태에 failed로 남아 있다
                log.warning("sync graph ingest failed: %s", exception_kind(exc))
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
            log.warning("background graph ingest failed job=%s: %s", job_id, exception_kind(exc))

    async def wait_graph_tasks(self) -> None:
        """테스트·종료용: 떠 있는 묶음 생성과 백그라운드 수집을 기다린다."""
        if self._index_tasks:  # 묶음 생성이 끝나야 그래프 수집 태스크가 생긴다
            await asyncio.gather(*list(self._index_tasks.values()), return_exceptions=True)
            await asyncio.sleep(0)  # 끝난 태스크의 정리 콜백이 돌 차례를 준다
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def delete_conversation(self, user_id: str, conversation_id: UUID) -> DeletionResult:
        """원문·묶음·가져오기 작업·그래프 에피소드를 지운다. 삭제 뒤 C/D 검색에 이 대화방이 나오지 않는다.

        그래프를 먼저 지운다 (메시지 목록이 필요하다). 그래프 삭제가 실패해도 DB는 지우고 사유를 돌려준다 —
        사용자의 삭제 요청은 원문 삭제가 우선이고, 그래프 잔여물은 재시도 대상으로 남긴다.
        """
        conv = await self._conversations.get(user_id, conversation_id)  # 소유권
        # 진행 중인 가져오기의 저장이 끝난 뒤에 지운다. 뒤에서 도는 묶음 생성은 기다리지 않는다 —
        # 지워진 방을 만나면 그쪽이 그만둔다.
        async with self._conv_locks.hold(conv.id):
            return await self._delete_conversation(user_id, conv)

    async def _delete_conversation(self, user_id: str, conv: Conversation) -> DeletionResult:
        messages = await self._conversations.list_messages(user_id, conv.id)
        graph_removed = 0
        graph_error: str | None = None
        if self._graph_ingest is not None:
            names = [f"{conv.source_key}:{m.source_key}" for m in messages if m.graph_ingested_at is not None]
            try:
                graph_removed = await self._graph_ingest.remove_episodes(user_id, names)
            except Exception as exc:  # noqa: BLE001 - DB 삭제는 계속한다
                graph_error = exception_kind(exc)
                log.warning("graph removal failed conversation=%s: %s", conv.id, graph_error)
        deleted_messages, deleted_episodes = await self._conversations.delete_conversation(user_id, conv.id)
        return DeletionResult(
            conversation_id=conv.id,
            messages=deleted_messages,
            episodes=deleted_episodes,
            graph_episodes=graph_removed,
            graph_error=graph_error,
        )

    async def reindex(self, user_id: str, conv: Conversation) -> int:
        """대화방의 묶음을 전부 다시 만든다. 임베딩 모델을 바꿨을 때도 이걸 돌린다.

        본문(`context_text`)이 그대로인 묶음은 저장돼 있는 벡터를 다시 쓴다 — **같은 임베딩 버전일 때만**.
        메시지 몇 건을 더 가져올 때마다 방 전체(수천 묶음)를 다시 임베딩하던 것을 새 묶음만 하게 된다.
        모델(버전)이 바뀌면 다시 쓸 것이 없으므로 전부 새로 계산한다.
        """
        all_messages = await self._conversations.list_messages(user_id, conv.id)
        drafts = build_episodes(all_messages, conv.source_key, self._window)
        version = self._embedder.version
        known = await self._conversations.episode_vectors(user_id, conv.id) if drafts else {}
        vectors_by_text = {
            d.context_text: known[f"{version}\n{d.context_text}"]
            for d in drafts
            if f"{version}\n{d.context_text}" in known
        }
        fresh = list(dict.fromkeys(d.context_text for d in drafts if d.context_text not in vectors_by_text))
        if fresh:
            vectors_by_text.update(zip(fresh, await self._embedder.embed_passages(fresh), strict=True))
        vectors = [vectors_by_text[d.context_text] for d in drafts]
        return await self._conversations.replace_episodes(
            user_id, conv.id, conv.contact_id, drafts, vectors, version
        )
