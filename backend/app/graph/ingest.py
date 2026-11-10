"""그래프 수집: 아직 안 들어간 메시지를 발화 시각 순으로 `add_episode` 한다.

- group_id = user_id. 사용자 그래프는 서로 섞이지 않는다.
- Graphiti는 넣는 순서대로 이전 사실을 무효화하므로 시간 순이 필수다. 그래서 수집은 **사용자 단위로 직렬**이다
  (`ingest_user`: 사용자별 asyncio.Lock, 미수집 메시지를 대화방 가리지 않고 전체 시각 순으로). 가져오기 직후
  백그라운드 수집도 이 경로를 탄다 — 다른 방의 가져오기가 겹치면 뒤 작업이 잠금을 기다렸다가 남은 것을 넣는다
  (11-09 백로그 A3; 그 전엔 대화방별 태스크라 방 사이 순서가 어긋날 수 있었다).
- 메시지마다 `graph_ingested_at`을 찍어 재실행이 멱등하다. 에피소드 하나가 haiku로 10~15초 걸린다.
- 실패하면 그 메시지부터 다시 시도할 수 있게 앞선 메시지의 표시는 남긴다.
- 수집 도중 대화방(또는 상대·계정)이 지워지면 그 방의 남은 메시지는 넣지 않고, 모델을 부르는 사이에
  지워졌으면 방금 넣은 에피소드를 되돌린다 — 지운 대화가 그래프에 남지 않게.

비용 상한 (메시지 1건 = 에피소드 1건 = 모델 호출 여러 번)
- `user_message_cap`: 사용자 한 명의 메시지 중 그래프에 **들어가 있는** 수의 상한
  (대화를 지우면 그만큼 다시 여유가 생긴다).
- `daily_global_cap`: 하루(사용량 날짜, 기본 KST) 동안 전체 사용자가 넣는 수의 상한. DB의 오늘 수집분과 이
  프로세스가 센 값 중 큰 쪽을 쓴다 — 수집 뒤 대화를 지워도 오늘 쓴 예산이 되살아나지 않는다
  (프로세스가 떠 있는 동안).
- 상한은 **API 경로**(`ingest_user`: 가져오기 뒤 수집, 서버 시작 이어받기)에만 건다. 운영자가 직접 돌리는
  스크립트의 `ingest_pending`·`ingest_conversation`은 기본이 상한 없음이라 합성 적재·평가 결과가 설정에 따라
  달라지지 않는다.
- 상한에 닿으면 시각 순으로 예산만큼만 넣고 멈춘다. 작업은 `limited`(graph_error에 `user_cap`/`daily_cap`),
  남은 메시지는 미수집으로 둔다. 문체 검색(C)은 상한과 무관하다.
- `resume_all`은 예산이 0인 사용자를 건너뛴다(모델 호출 없음). 날이 바뀌어 하루 예산이 생기면 이어서 넣는다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta, timezone
from uuid import UUID

from app.core.errors import NotFoundError
from app.domain.conversation import Conversation, GraphStatus, Message
from app.graph.episodes import build_graph_episodes
from app.graph.store import GraphStore
from app.repositories.base import ContactRepo, ConversationRepo

log = logging.getLogger(__name__)

USER_CAP = "user_cap"
DAILY_CAP = "daily_cap"


class GraphIngestService:
    def __init__(
        self,
        store: GraphStore,
        conversations: ConversationRepo,
        contacts: ContactRepo,
        *,
        user_message_cap: int = 0,
        daily_global_cap: int = 0,
        day_offset_hours: int = 9,
    ) -> None:
        self._store = store
        self._conversations = conversations
        self._contacts = contacts
        self._prepared = False
        self._locks: dict[str, asyncio.Lock] = {}  # user_id → 수집 잠금 (프로세스 안에서만 유효)
        self.user_message_cap = user_message_cap  # 0 = 무제한
        self.daily_global_cap = daily_global_cap  # 0 = 무제한
        self._day_tz = timezone(timedelta(hours=day_offset_hours))
        self._global_day: date | None = None  # 아래 카운터가 세는 날
        self._global_used = 0  # 오늘 이 프로세스가 넣은(예약 포함) 수와 DB 값 중 큰 쪽

    @property
    def capped(self) -> bool:
        return bool(self.user_message_cap or self.daily_global_cap)

    def _today(self, now: datetime | None = None) -> tuple[date, datetime]:
        """(오늘 날짜, 오늘이 시작한 시각 UTC)."""
        local = (now or datetime.now(UTC)).astimezone(self._day_tz)
        start = datetime.combine(local.date(), time.min, tzinfo=self._day_tz)
        return local.date(), start.astimezone(UTC)

    async def _sync_global(self) -> None:
        """오늘의 전체 수집 수를 DB와 맞춘다 (날이 바뀌면 0부터)."""
        today, since = self._today()
        if self._global_day != today:
            self._global_day, self._global_used = today, 0
        in_db = await self._conversations.count_graph_ingested(None, since)
        self._global_used = max(self._global_used, in_db)

    def _reserve_global(self) -> bool:
        """하루 전체 예산에서 1건을 잡는다. 남은 게 없으면 False."""
        if not self.daily_global_cap:
            return True
        today, _ = self._today()
        if self._global_day != today:
            self._global_day, self._global_used = today, 0
        if self._global_used >= self.daily_global_cap:
            return False
        self._global_used += 1
        return True

    def _release_global(self) -> None:
        if self.daily_global_cap and self._global_used > 0:
            self._global_used -= 1

    async def remaining(self, user_id: str) -> tuple[int | None, str | None]:
        """(지금 더 넣을 수 있는 메시지 수, 먼저 닿는 상한 `user_cap`/`daily_cap`).

        상한이 없으면 (None, None).
        """
        left: int | None = None
        reason: str | None = None
        if self.daily_global_cap:
            await self._sync_global()
            left = max(self.daily_global_cap - self._global_used, 0)
            reason = DAILY_CAP
        if self.user_message_cap:
            used = await self._conversations.count_graph_ingested(user_id)
            user_left = max(self.user_message_cap - used, 0)
            if left is None or user_left <= left:
                left, reason = user_left, USER_CAP
        return left, reason

    async def is_limited(self, user_id: str) -> bool:
        """이 사용자는 지금 상한 때문에 더 넣을 수 없는가."""
        if not self.capped:
            return False
        left, _ = await self.remaining(user_id)
        return left == 0

    async def ingest_user(
        self, user_id: str, job_id: UUID | None = None, *, enforce_caps: bool = True
    ) -> int:
        """이 사용자의 미수집 메시지를 전체 시각 순으로 넣는다. 같은 사용자의 수집은 한 번에 하나만.

        잠금을 기다리는 동안 다른 작업이 먼저 넣은 메시지는 pending에서 빠지므로 중복 수집은 없다.
        job_id를 주면 그 작업의 graph_* 상태를 이번 실행 결과로 갱신한다
        (다른 방의 메시지가 섞여 들어갈 수 있다). 상한(`GRAPH_INGEST_*_CAP`)을 따른다.
        """
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await self._job(user_id, job_id, "pending", 0, None)
        async with lock:
            done = await self.ingest_pending(user_id, None, job_id, enforce_caps=enforce_caps)
            if done:
                # 이번 실행이 다른 작업(예: 어제 상한에 걸린 작업)의 남은 메시지까지 넣었을 수 있다.
                await self._conversations.close_stale_graph_jobs(user_id)
            return done

    async def ingest_conversation(
        self, user_id: str, conversation_id: UUID, job_id: UUID | None = None
    ) -> int:
        return await self.ingest_pending(user_id, [conversation_id], job_id)

    async def ingest_pending(
        self,
        user_id: str,
        conversation_ids: list[UUID] | None = None,
        job_id: UUID | None = None,
        *,
        enforce_caps: bool = False,
    ) -> int:
        """대화방들의 미수집 메시지를 시각 순으로 넣는다. 넣은 수를 돌려준다.

        `enforce_caps`가 참이면 상한 안에서만 넣고, 남기면 작업을 `limited`로 표시한다.
        """
        convs = (
            [await self._conversations.get(user_id, cid) for cid in conversation_ids]
            if conversation_ids is not None
            else await self._conversations.list(user_id)
        )
        pending: list[tuple[Message, Conversation, str]] = []  # (message, conversation, episode body)
        for conv in convs:
            contact = await self._contacts.get(user_id, conv.contact_id)
            messages = await self._conversations.list_messages(user_id, conv.id)
            episodes = {
                e.message_id: e
                for e in build_graph_episodes(messages, conv.source_key, conv.me_name, contact.alias)
            }
            pending.extend((m, conv, episodes[m.id].body) for m in messages if m.graph_ingested_at is None)
        pending.sort(key=lambda item: item[0].sent_at)

        await self._job(user_id, job_id, "running", 0, None)
        if not pending:
            await self._job(user_id, job_id, "completed", 0, None)
            return 0

        enforce = enforce_caps and self.capped
        budget: int | None = None
        limit_reason: str | None = None  # 예산을 다 쓰면 멈추는 이유 (먼저 닿는 상한)
        if enforce:
            budget, limit_reason = await self.remaining(user_id)
            if budget == 0:
                log.info("graph ingest skipped user=%s: %s", user_id, limit_reason)
                await self._job(user_id, job_id, "limited", 0, limit_reason)
                return 0

        if not self._prepared:
            await self._store.prepare()
            self._prepared = True

        done = 0
        stopped: str | None = None
        gone: set[UUID] = set()  # 수집 도중 지워진 대화방 — 남은 메시지를 그래프에 넣지 않는다
        try:
            for message, conv, body in pending:
                if conv.id in gone:
                    continue
                if not await self._exists(user_id, conv.id):
                    gone.add(conv.id)
                    continue
                if enforce:
                    if budget is not None and done >= budget:
                        stopped = limit_reason or DAILY_CAP
                        break
                    if not self._reserve_global():  # 다른 사용자의 수집이 그 사이 하루 예산을 썼다
                        stopped = DAILY_CAP
                        break
                name = f"{conv.source_key}:{message.source_key}"
                try:
                    await self._store.add_episode(
                        name=name,
                        body=body,
                        reference_time=message.sent_at,
                        group_id=user_id,
                        source_description=f"과거 대화 ({conv.title})",
                    )
                except Exception:
                    if enforce:
                        self._release_global()
                    raise
                if not await self._exists(user_id, conv.id):
                    # 모델을 부르는 사이(10~15초)에 대화방이 지워졌다. 삭제는 이미 끝났으므로 방금 넣은
                    # 에피소드를 여기서 되돌린다 — 지운 대화가 그래프에 남으면 안 된다.
                    gone.add(conv.id)
                    await self._store.remove_episodes(user_id, [name])
                    continue
                await self._conversations.mark_graph_ingested(user_id, [message.id], datetime.now(UTC))
                done += 1
                await self._job(user_id, job_id, "running", done, None)
        except Exception as exc:
            log.warning("graph ingest failed user=%s after %d episodes: %s", user_id, done, exc)
            await self._job(user_id, job_id, "failed", done, str(exc)[:500])
            raise
        if gone:
            log.info("graph ingest skipped %d deleted conversation(s) user=%s", len(gone), user_id)
        if stopped is not None:
            log.info(
                "graph ingest limited user=%s reason=%s done=%d left=%d",
                user_id,
                stopped,
                done,
                len(pending) - done,
            )
            await self._job(user_id, job_id, "limited", done, stopped)
            return done
        await self._job(user_id, job_id, "completed", done, None)
        return done

    async def _exists(self, user_id: str, conversation_id: UUID) -> bool:
        try:
            await self._conversations.get(user_id, conversation_id)
        except NotFoundError:
            return False
        return True

    async def _job(
        self, user_id: str, job_id: UUID | None, status: GraphStatus, episodes: int, error: str | None
    ) -> None:
        """작업의 그래프 상태 갱신. 그 사이 대화방(과 작업)이 지워졌으면 조용히 넘어간다.

        수집은 사용자 단위라 다른 대화방의 메시지가 남아 있을 수 있다 — 작업이 없어졌다고 멈추지 않는다.
        """
        if job_id is None:
            return
        try:
            await self._conversations.update_job_graph(user_id, job_id, status, episodes, error)
        except NotFoundError:
            log.info("graph ingest: job %s no longer exists (conversation deleted)", job_id)

    async def remove_episodes(self, user_id: str, names: list[str]) -> int:
        """대화방 삭제 때 그 메시지들의 에피소드를 그래프에서 지운다."""
        return await self._store.remove_episodes(user_id, names)

    async def reset(self, user_id: str, conversation_id: UUID | None = None) -> tuple[int, int]:
        """그래프를 지우고 메시지를 다시 수집 대상으로 되돌린다. (그래프에서 지운 수, 되돌린 메시지 수).

        대화방을 주면 그 방의 에피소드만 지운다(다른 방 사실은 남는다). 안 주면 사용자 group 전체를
        DETACH DELETE 한다 — 스키마·프롬프트를 바꿔 다시 뽑을 때. 실제 재수집은 `ingest_user`로.
        """
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:  # 진행 중인 수집과 겹치지 않게
            if conversation_id is None:
                removed = await self._store.remove_group(user_id)
            else:
                conv = await self._conversations.get(user_id, conversation_id)
                messages = await self._conversations.list_messages(user_id, conv.id)
                names = [
                    f"{conv.source_key}:{m.source_key}" for m in messages if m.graph_ingested_at is not None
                ]
                removed = await self._store.remove_episodes(user_id, names)
            reset = await self._conversations.reset_graph_ingested(user_id, conversation_id)
        return removed, reset

    async def resume_all(self) -> dict[str, int]:
        """서버 시작 시: 미수집 메시지가 남은 사용자마다 이어서 넣고, 끝난 작업의 상태를 정리한다.

        프로세스가 죽어 pending/running인 채 남은 작업을 살린다. 사용자 단위로 순서대로(잠금 안에서) 돈다.
        상한 때문에 지금 넣을 수 없는 사용자는 건너뛴다 — 재시작할 때마다 다시 돌지 않는다.
        """
        done: dict[str, int] = {}
        for user_id in await self._conversations.users_with_pending_graph():
            if await self.is_limited(user_id):
                continue
            try:
                done[user_id] = await self.ingest_user(user_id)
            except Exception as exc:  # noqa: BLE001 - 한 사용자 실패가 다른 사용자를 막지 않는다
                log.warning("graph resume failed user=%s: %s", user_id, exc)
                continue
            await self._conversations.close_stale_graph_jobs(user_id)
        return done
