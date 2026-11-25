"""그래프 수집: 아직 안 들어간 메시지를 발화 시각 순으로 `add_episode` 한다.

- group_id = user_id. 사용자 그래프는 서로 섞이지 않는다.
- Graphiti는 넣는 순서대로 이전 사실을 무효화하므로 시간 순이 필수다. 그래서 수집은 **사용자 단위로 직렬**이다
  (`ingest_user`: 사용자별 잠금, 미수집 메시지를 대화방 가리지 않고 전체 시각 순으로). 가져오기 직후
  백그라운드 수집도 이 경로를 탄다 — 다른 방의 가져오기가 겹치면 뒤 작업이 잠금을 기다렸다가 남은 것을 넣는다
  (11-09 백로그 A3; 그 전엔 대화방별 태스크라 방 사이 순서가 어긋날 수 있었다).
- 메시지마다 `graph_ingested_at`을 찍어 재실행이 멱등하다. 에피소드 하나가 haiku로 10~15초 걸린다.
- 실패하면 그 메시지부터 다시 시도할 수 있게 앞선 메시지의 표시는 남긴다.
- 수집 도중 대화방(또는 상대·계정)이 지워지면 그 방의 남은 메시지는 넣지 않고, 모델을 부르는 사이에
  지워졌으면 방금 넣은 에피소드를 되돌린다 — 지운 대화가 그래프에 남지 않게.

비용 상한 (메시지 1건 = 에피소드 1건 = 모델 호출 여러 번) — 2026-11-11 검토 뒤 다시 짰다
- 세는 단위: 메시지 1건이 1, 본문 1,000자마다 1을 더한다 (`domain.conversation.graph_units`).
  4,000자 메시지는 5로 센다 — 에피소드의 모델 비용이 본문 길이를 따라가기 때문이다.
- **지워지지 않는 카운터**: API 경로로 넣을 때마다 `usage_counters.graph_episodes`(사용자·일)에 더한다.
  대화를 지워도 줄지 않는다. 예전에는 "지금 그래프에 들어 있는 수"만 봐서 가져오기→삭제를 되풀이하면
  상한이 무력했다.
- `user_message_cap`: 사용자가 오늘 더 넣을 수 있는 양 = 상한 − max(지금 그래프에 든 양, 오늘 넣은 양).
  대화를 지워도 오늘은 여유가 되살아나지 않는다 (내일은 지금 든 양 기준으로 다시 생긴다).
- `daily_global_cap`: 하루(사용량 날짜, 기본 KST) 동안 전체 사용자가 넣는 양 = 오늘 카운터의 합계.
  계정을 지우면 그 사용자의 카운터 행도 지워지므로, 이 프로세스가 센 값과 견줘 큰 쪽을 쓴다.
- 예산이 모자라면 **가장 최근 메시지부터** 예산만큼 골라, 고른 것을 시각 순으로 넣는다 (사건 맥락은 최근
  것이 쓸모 있다 — 예전에는 가장 오래된 것부터 채웠다). 나중에 예산이 생겨 더 오래된 메시지를 넣게 되면
  그 실행은 앞 실행보다 과거의 메시지를 넣는다. Graphiti는 사실의 선후를 발화 시각(valid_at)으로 따지지만,
  넣는 순서가 뒤바뀐 구간에서는 무효화 판단이 덜 정확할 수 있다.
- 상한과 카운터는 **API 경로**(`ingest_user`: 가져오기 뒤 수집, 재시작·주기 이어받기)에만 건다. 운영자가
  직접 돌리는 경로(`ingest_pending`·`ingest_conversation`, `ingest_user(enforce_caps=False)` —
  합성 적재, 평가, `scripts/graph_reset.py`)는 상한도 카운터도 건드리지 않는다.
- 상한에 닿으면 작업은 `limited`(graph_error에 `user_cap`/`daily_cap`), 남은 메시지는 미수집으로 둔다.
  같은 사용자의 다른 작업이 `pending`/`running`으로 남아 있으면(프로세스가 죽었던 경우) 함께 `limited`로
  정리한다 — 화면이 끝없이 "정리 중"으로 보이지 않게. 문체 검색(C)은 상한과 무관하다.
- `resume_all`은 예산이 0인 사용자를 건너뛴다(모델 호출 없음). 날이 바뀌어 예산이 생기면 이어서 넣는다.
- 한계: 잠금과 프로세스 집계는 서버 프로세스 하나 안에서만 유효하다.

거듭 실패하는 메시지 (2026-11-13) — `max_attempts`(`GRAPH_EPISODE_MAX_ATTEMPTS`, 기본 3, 0이면 끔)
- 예전에는 에피소드 하나가 실패하면 그 사용자의 수집이 거기서 멈추고, 재시작·주기 이어받기·다음 가져오기
  때마다 같은 메시지를 다시 시도했다 — 모델이 늘 실패하는 메시지 하나가 뒤의 메시지를 영영 막고 비용을 썼다.
- 이제 실패를 메시지마다 센다(`messages.graph_attempts`). 같은 실행 안에서 바로 다시 시도하고, 횟수를 다
  쓰면 그 메시지를 **보류**(`graph_held_at`)하고 다음 메시지로 넘어간다. 보류된 메시지는 미수집으로 세지
  않으므로 다시 돌지 않는다. 운영자가 관리자 화면의 "다시 시도"(`release_held` → `start_resume`, 보류만 풂)나
  `scripts/graph_reset.py`(그래프를 지우고 다시 만든다)로 풀 수 있다.
- **일시 장애는 세지 않는다**(`is_transient`: 연결·시간 초과·요청 한도·인증·5xx·Neo4j 접속). 그때는 예전처럼
  실행을 멈추고 작업을 `failed`로 남긴다 — 장애 동안 메시지가 줄줄이 보류되면 안 된다.
- 보류가 연달아 `GIVE_UP_STREAK`건 나오면 메시지 탓이 아니라 설정·모델 쪽 문제로 보고 실행을 멈춘다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from uuid import UUID

from app.core.errors import NotFoundError
from app.domain.conversation import Conversation, GraphStatus, Message, exception_kind, graph_units
from app.domain.titles import ORG_TITLES, strip_honorific
from app.graph.episodes import DEFAULT_ME_NAME, build_graph_episodes
from app.graph.store import GraphStore
from app.repositories.base import ContactRepo, ConversationRepo
from app.services.locks import KeyedLocks

log = logging.getLogger(__name__)

USER_CAP = "user_cap"
DAILY_CAP = "daily_cap"
_BUDGET = "budget"  # 예산 확인과 예약을 한 번에 하나씩 하는 잠금의 키

GIVE_UP_STREAK = 2  # 연달아 이만큼 보류되면 메시지 탓이 아니다 — 실행을 멈춘다
YIELD_WAIT_SECONDS = (
    45.0  # 화자 역할 고치기가 도는 수집이 비켜 주길 기다리는 시간 (에피소드 하나 + 재시도 여유)
)

HONORIFICS = ("님", "씨")  # 이름 대조에서 붙이거나 떼어 보는 경칭
GRAPH_NAME_MAX = 50  # contacts.graph_name 열 길이

Pending = tuple[Message, Conversation, str]  # (메시지, 대화방, 에피소드 본문)

# 메시지 내용과 무관한 실패 — 실패 횟수에 넣지 않는다. 클래스 이름으로 본다 (anthropic·graphiti·neo4j가
# 설치돼 있지 않아도 import되게).
_TRANSIENT_NAMES = frozenset(
    {
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "OverloadedError",
        "ServiceUnavailableError",
        "AuthenticationError",
        "PermissionDeniedError",
        "ServiceUnavailable",
        "SessionExpired",
        "TransientError",
        "DriverError",
    }
)
_TRANSIENT_STATUS = frozenset({401, 403, 408, 409, 429})


def is_transient(exc: BaseException) -> bool:
    """이 실패는 메시지 탓이 아닌가 (연결·시간 초과·요청 한도·인증·서버 오류)."""
    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status >= 500 or status in _TRANSIENT_STATUS:
            return True
    return any(cls.__name__ in _TRANSIENT_NAMES for cls in type(exc).__mro__)


@dataclass(frozen=True)
class NameCheck:
    """상대 하나의 이름 대조 결과 (`GraphIngestService.check_names`)."""

    contact_id: UUID
    alias: str
    name: str  # 지금 그래프에서 찾는 이름 (`Contact.name_in_graph`)
    found: bool  # 이 이름과 정확히 같은 노드가 있다
    candidates: tuple[str, ...] = ()  # 없을 때: 경칭·공백만 다른 Person 노드 이름

    @property
    def match(self) -> str | None:
        """맞춰 쓸 노드 이름 — 같은 노드가 없고 후보가 정확히 하나일 때만."""
        return self.candidates[0] if not self.found and len(self.candidates) == 1 else None


def same_person_name(a: str, b: str) -> bool:
    """두 이름이 경칭('님'·'씨') 하나를 붙이거나 뗀 차이, 또는 공백 차이뿐인가.

    이름 뒤에 직함만 더 붙은 것도 같다고 본다('최서연 팀장님' / '최서연') — 상대 이름은 직함까지 적고
    대화의 화자 이름은 이름뿐인 경우가 흔해, 추출 모델이 노드를 이름만으로 만든다(2026-11-24).
    일부러 좁다: 성만 같거나('김상무' / '김철수 상무') 직함이 다른 이름('최서연 팀장' / '최서연 부장'),
    한 글자짜리 이름('김' / '김상무')은 같다고 보지 않는다.
    """
    x, y = "".join(a.split()), "".join(b.split())
    if not x or not y:
        return False
    if x == y or any(x + h == y or y + h == x for h in HONORIFICS):
        return True
    short, long = sorted((x, y), key=len)
    if len(short) < 2 or not long.startswith(short):
        return False
    return strip_honorific(long[len(short) :]) in ORG_TITLES


def pick_recent(pending: list[Pending], budget: int) -> list[Pending]:
    """시각 순 목록에서 예산에 드는 **가장 최근** 구간을 고른다 (결과도 시각 순).

    최근 것부터 거슬러 올라가다 예산을 넘기는 메시지를 만나면 멈춘다 — 고른 구간은 끊기지 않는다.
    """
    used = 0
    start = len(pending)
    while start > 0:
        units = graph_units(pending[start - 1][0].text)
        if used + units > budget:
            break
        used += units
        start -= 1
    return pending[start:]


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
        max_attempts: int = 0,
    ) -> None:
        self._store = store
        self.max_attempts = max_attempts  # 메시지 하나의 실패 한도. 0 = 세지 않는다 (실패하면 멈춘다)
        self._conversations = conversations
        self._contacts = contacts
        self._prepared = False
        self._locks = KeyedLocks()  # ("user", user_id) → 수집 잠금 / _BUDGET → 예산 확인·예약
        self.user_message_cap = user_message_cap  # 0 = 무제한
        self.daily_global_cap = daily_global_cap  # 0 = 무제한
        self._day_tz = timezone(timedelta(hours=day_offset_hours))
        # 오늘 이 프로세스가 API 경로로 넣은 양 — DB 카운터가 (계정 삭제로) 줄어도 남는다.
        self._tally_day: date | None = None
        self._tally = 0
        self._stopping = False  # 서버가 내려가는 중 — 넣던 에피소드까지만 끝내고 멈춘다
        # 사용자 id → 비켜 달라는 요청 수. 있으면 그 사용자의 수집은 에피소드 사이에서 멈춘다 (화자 고치기)
        self._yield: dict[str, int] = {}
        self.yield_wait = YIELD_WAIT_SECONDS
        self._tasks: set[asyncio.Task[dict[str, int]]] = set()  # 보류를 푼 뒤의 백그라운드 수집

    @property
    def capped(self) -> bool:
        return bool(self.user_message_cap or self.daily_global_cap)

    def _today(self, now: datetime | None = None) -> tuple[date, datetime]:
        """(오늘 날짜, 오늘이 시작한 시각 UTC)."""
        local = (now or datetime.now(UTC)).astimezone(self._day_tz)
        start = datetime.combine(local.date(), time.min, tzinfo=self._day_tz)
        return local.date(), start.astimezone(UTC)

    def request_stop(self) -> None:
        """서버 종료: 돌고 있는 수집이 지금 넣는 에피소드까지만 끝내고 멈추게 한다 (새 수집도 바로 끝난다).

        남은 메시지는 미수집으로 남고 작업은 `pending`이 된다 — 다음 시작 때 `resume_all`이 이어받는다.
        """
        self._stopping = True

    @property
    def background_tasks(self) -> set[asyncio.Task]:
        """`start_resume`으로 띄운 수집 태스크 (종료 때 기다리는 쪽이 본다)."""
        return self._tasks

    def _process_tally(self, day: date, add: int = 0) -> int:
        if self._tally_day != day:
            self._tally_day, self._tally = day, 0
        self._tally = max(self._tally + add, 0)
        return self._tally

    async def used_by(self, user_id: str) -> int:
        """이 사용자가 상한에서 쓴 양: max(지금 그래프에 든 양, 오늘 넣은 양)."""
        today, _ = self._today()
        in_graph = await self._conversations.graph_units_in_graph(user_id)
        return max(in_graph, await self._conversations.graph_usage(today, user_id))

    async def remaining(self, user_id: str) -> tuple[int | None, str | None]:
        """(지금 더 넣을 수 있는 양, 먼저 닿는 상한 `user_cap`/`daily_cap`).

        상한이 없으면 (None, None). 양의 단위는 `graph_units`(보통 메시지 1건 = 1).
        """
        left: int | None = None
        reason: str | None = None
        if self.daily_global_cap:
            today, _ = self._today()
            used = max(await self._conversations.graph_usage(today), self._process_tally(today))
            left = max(self.daily_global_cap - used, 0)
            reason = DAILY_CAP
        if self.user_message_cap:
            user_left = max(self.user_message_cap - await self.used_by(user_id), 0)
            if left is None or user_left <= left:
                left, reason = user_left, USER_CAP
        return left, reason

    async def is_limited(self, user_id: str) -> bool:
        """이 사용자는 지금 상한 때문에 더 넣을 수 없는가."""
        if not self.capped:
            return False
        left, _ = await self.remaining(user_id)
        return left == 0

    async def _reserve(self, user_id: str, units: int) -> tuple[str | None, date]:
        """예산에서 units를 잡고 카운터에 더한다. 모자라면 잡지 않고 (이유, 날짜)를 돌려준다.

        확인과 예약 사이에 다른 사용자의 수집이 끼어들지 못하게 프로세스 안에서 한 번에 하나씩 한다.
        """
        async with self._locks.hold(_BUDGET):
            today, _ = self._today()
            left, reason = await self.remaining(user_id)
            if left is not None and units > left:
                return reason or DAILY_CAP, today
            await self._conversations.add_graph_usage(user_id, today, units)
            self._process_tally(today, units)
            return None, today

    async def _release(self, user_id: str, day: date, units: int) -> None:
        """에피소드를 넣지 못했다 — 잡아 둔 예산을 돌려놓는다."""
        await self._conversations.add_graph_usage(user_id, day, -units)
        self._process_tally(day, -units)

    async def ingest_user(
        self, user_id: str, job_id: UUID | None = None, *, enforce_caps: bool = True
    ) -> int:
        """이 사용자의 미수집 메시지를 전체 시각 순으로 넣는다. 같은 사용자의 수집은 한 번에 하나만.

        잠금을 기다리는 동안 다른 작업이 먼저 넣은 메시지는 pending에서 빠지므로 중복 수집은 없다.
        job_id를 주면 그 작업의 graph_* 상태를 이번 실행 결과로 갱신한다
        (다른 방의 메시지가 섞여 들어갈 수 있다). 상한(`GRAPH_INGEST_*_CAP`)을 따르고 카운터에 센다 —
        운영자가 직접 다시 넣을 때만 `enforce_caps=False`(상한 없음, 카운터도 그대로).
        """
        key = ("user", user_id)
        if self._locks.busy(key):
            await self._job(user_id, job_id, "pending", 0, None)
        async with self._locks.hold(key):
            done, stopped = await self._run(user_id, None, job_id, metered=enforce_caps)
            if done:
                await self._reconcile_quietly(user_id)
            if done:
                # 이번 실행이 다른 작업(예: 어제 상한에 걸린 작업)의 남은 메시지까지 넣었을 수 있다.
                await self._conversations.close_stale_graph_jobs(user_id)
            if stopped is not None:
                # 상한 때문에 남겼다. pending/running으로 남은 다른 작업(죽은 프로세스의 흔적)도 정리한다.
                # 지금 잠금을 기다리는 작업은 자기 차례에 상태를 다시 쓴다.
                await self._conversations.limit_stale_graph_jobs(user_id, stopped)
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

        `enforce_caps`가 참이면 상한 안에서만 넣고(카운터에도 센다), 남기면 작업을 `limited`로 표시한다.
        기본(운영자 경로)은 상한도 카운터도 없다.
        """
        done, _ = await self._run(user_id, conversation_ids, job_id, metered=enforce_caps)
        if done:
            await self._reconcile_quietly(user_id)
        return done

    async def _reconcile_quietly(self, user_id: str) -> None:
        """수집 뒤 이름 대조. 실패해도 수집 결과는 그대로다 (다음 수집 뒤에 다시 한다)."""
        try:
            await self.reconcile_names(user_id)
        except Exception as exc:  # noqa: BLE001 - 대조 실패가 끝난 수집을 실패로 만들지 않는다
            log.warning("graph name check failed user=%s: %s", user_id, exception_kind(exc))

    async def reconcile_names(self, user_id: str) -> dict[UUID, str]:
        """상대마다 그래프 이름(`name_in_graph`)과 실제 노드 이름을 대조한다. {고친 상대 id: 새 이름}.

        추출 모델이 에피소드 본문의 이름을 다르게 적을 때가 있다('김상무님' → 노드 '김상무', 2026-11-19).
        그러면 이름이 정확히 같은 노드를 찾는 조회(`find_entity_uuid`)가 비어 화면과 사실 맥락이 빈다.
        - 이름이 정확히 같은 노드가 있으면 그대로 둔다.
        - 없으면 그 상대의 대화방 에피소드가 언급한 Person 노드 중 경칭·공백만 다른 이름
          (`same_person_name`)을 찾아, **정확히 하나**일 때만 `contacts.graph_name`을 그 이름으로 맞춘다.
          이후 에피소드 본문도 그 이름을 쓴다.
        - 후보가 없거나 둘 이상이면 바꾸지 않고 경고만 남긴다. 모델을 부르지 않는다(읽기 조회뿐).
        조회 쪽은 느슨하게 하지 않는다 — 다른 사람과 섞일 수 있어, 대조는 수집 뒤 여기서만 한다.
        """
        fixed: dict[UUID, str] = {}
        for check in await self.check_names(user_id):
            if check.found:
                continue
            if check.match is None:
                log.warning(
                    "graph name mismatch user=%s contact=%s name=%r candidates=%r (그대로 둔다)",
                    user_id,
                    check.contact_id,
                    check.name,
                    list(check.candidates),
                )
                continue
            await self._contacts.set_graph_name(user_id, check.contact_id, check.match)
            fixed[check.contact_id] = check.match
            log.info(
                "graph name matched user=%s contact=%s %r -> %r",
                user_id,
                check.contact_id,
                check.name,
                check.match,
            )
        return fixed

    async def check_names(self, user_id: str) -> list[NameCheck]:
        """그래프에 메시지가 들어간 상대마다 이름 대조 결과를 돌려준다. 읽기만 한다 (아무것도 안 바꾼다)."""
        episodes: dict[UUID, list[str]] = {}  # 상대 id → 그래프에 들어간 에피소드 이름
        me_names: dict[UUID, set[str]] = {}
        for conv in await self._conversations.list(user_id):
            messages = await self._conversations.list_messages(user_id, conv.id)
            names = [f"{conv.source_key}:{m.source_key}" for m in messages if m.graph_ingested_at is not None]
            if names:
                episodes.setdefault(conv.contact_id, []).extend(names)
                me_names.setdefault(conv.contact_id, set()).add((conv.me_name or DEFAULT_ME_NAME).strip())
        out: list[NameCheck] = []
        for contact_id, names in episodes.items():
            contact = await self._contacts.get(user_id, contact_id)
            target = contact.name_in_graph
            if await self._store.find_entity_uuid(user_id, target) is not None:
                out.append(NameCheck(contact_id, contact.alias, target, found=True))
                continue
            found = await self._store.person_names(user_id, names)
            candidates = tuple(
                n
                for n in found
                if n not in me_names[contact_id] and len(n) <= GRAPH_NAME_MAX and same_person_name(n, target)
            )
            out.append(NameCheck(contact_id, contact.alias, target, found=False, candidates=candidates))
        return out

    async def _run(
        self, user_id: str, conversation_ids: list[UUID] | None, job_id: UUID | None, *, metered: bool
    ) -> tuple[int, str | None]:
        """(넣은 수, 상한 때문에 남겼으면 그 이유). `metered`면 상한을 따르고 카운터에 센다."""
        convs = (
            [await self._conversations.get(user_id, cid) for cid in conversation_ids]
            if conversation_ids is not None
            else await self._conversations.list(user_id)
        )
        pending: list[Pending] = []
        for conv in convs:
            contact = await self._contacts.get(user_id, conv.contact_id)
            messages = await self._conversations.list_messages(user_id, conv.id)
            if contact.graph_name is None and any(m.graph_ingested_at is None for m in messages):
                # 이 상대를 처음 그래프에 넣는다 — 지금 이름을 그래프 이름으로 남긴다. 나중에 별칭을
                # 바꿔도 에피소드 본문과 검색은 이 이름을 쓴다 (노드가 둘로 갈리지 않게).
                contact = await self._contacts.remember_graph_name(user_id, contact.id, contact.alias)
            episodes = {
                e.message_id: e
                for e in build_graph_episodes(messages, conv.source_key, conv.me_name, contact.name_in_graph)
            }
            pending.extend((m, conv, episodes[m.id].body) for m in messages if m.graph_pending)
        pending.sort(key=lambda item: item[0].sent_at)

        await self._job(user_id, job_id, "running", 0, None)
        if not pending:
            await self._job(user_id, job_id, "completed", 0, None)
            return 0, None

        chosen = pending
        stopped: str | None = None  # 상한 때문에 남긴 이유
        if metered and self.capped:
            budget, limit_reason = await self.remaining(user_id)
            if budget is not None:
                chosen = pick_recent(pending, budget)
                if len(chosen) < len(pending):
                    stopped = limit_reason or DAILY_CAP
            if not chosen:
                log.info("graph ingest skipped user=%s: %s", user_id, stopped)
                await self._job(user_id, job_id, "limited", 0, stopped)
                return 0, stopped

        if not self._prepared:
            await self._store.prepare()
            self._prepared = True

        done = 0
        held = 0  # 이번 실행에서 보류한 메시지 수
        streak = 0  # 연달아 보류한 수
        gone: set[UUID] = set()  # 수집 도중 지워진 대화방 — 남은 메시지를 그래프에 넣지 않는다
        interrupted = False
        try:
            for message, conv, body in chosen:
                if self._stopping or self._yield.get(user_id):
                    # 서버가 내려가거나, 이 사용자의 대화방을 고치려는 요청이 기다린다 — 여기서 멈춘다
                    interrupted = True
                    break
                if conv.id in gone:
                    continue
                if not await self._exists(user_id, conv.id):
                    gone.add(conv.id)
                    continue
                units = graph_units(message.text)
                name = f"{conv.source_key}:{message.source_key}"
                attempts = message.graph_attempts
                added = False
                out_of_budget = False
                while True:
                    reserved_on: date | None = None
                    if metered:
                        refused, reserved_on = await self._reserve(user_id, units)
                        if refused is not None:  # 다른 사용자의 수집이 그 사이 하루 예산을 썼다
                            stopped = refused
                            out_of_budget = True
                            break
                    try:
                        await self._store.add_episode(
                            name=name,
                            body=body,
                            reference_time=message.sent_at,
                            group_id=user_id,
                            source_description=f"과거 대화 ({conv.title})",
                        )
                        added = True
                        break
                    except asyncio.CancelledError:
                        # 종료 대기 시간을 넘겨 끊겼다 — 잡아 둔 예산을 돌려놓는다 (메시지는 미수집으로 남음)
                        if reserved_on is not None:
                            await self._release(user_id, reserved_on, units)
                        raise
                    except Exception as exc:
                        if reserved_on is not None:
                            await self._release(user_id, reserved_on, units)
                        if not self.max_attempts or is_transient(exc):
                            raise  # 장애이거나 횟수를 세지 않는 구성 — 여기서 멈추고 다음 실행에 맡긴다
                        attempts += 1
                        hold = attempts >= self.max_attempts
                        await self._conversations.record_graph_failure(
                            user_id, message.id, hold, datetime.now(UTC)
                        )
                        if not hold:
                            continue  # 모델 출력이 어긋난 것일 수 있다 — 바로 다시 시도한다
                        held += 1
                        streak += 1
                        log.warning(
                            "graph ingest gave up episode=%s user=%s after %d attempts: %s",
                            name,
                            user_id,
                            attempts,
                            exception_kind(exc),
                        )
                        if streak >= GIVE_UP_STREAK:
                            raise  # 메시지마다 실패한다 — 메시지 탓이 아니다
                        break
                if out_of_budget:
                    break
                if not added:
                    continue  # 보류했다 — 다음 메시지로
                streak = 0
                if not await self._exists(user_id, conv.id):
                    # 모델을 부르는 사이(10~15초)에 대화방이 지워졌다. 삭제는 이미 끝났으므로 방금 넣은
                    # 에피소드를 여기서 되돌린다 — 지운 대화가 그래프에 남으면 안 된다.
                    # 잡아 둔 예산은 돌려놓지 않는다 (모델은 이미 불렸다).
                    gone.add(conv.id)
                    await self._store.remove_episodes(user_id, [name])
                    continue
                await self._conversations.mark_graph_ingested(user_id, [message.id], datetime.now(UTC))
                done += 1
                await self._job(user_id, job_id, "running", done, None)
        except Exception as exc:
            kind = exception_kind(exc)  # 예외 문구에는 에피소드 본문(대화 원문)이 섞일 수 있다
            log.warning("graph ingest failed user=%s after %d episodes: %s", user_id, done, kind)
            await self._job(user_id, job_id, "failed", done, kind)
            raise
        if interrupted:
            why = "shutdown" if self._stopping else "a pending edit"
            left = len(chosen) - done
            log.info("graph ingest stopped for %s user=%s done=%d left=%d", why, user_id, done, left)
            await self._job(user_id, job_id, "pending", done, None)
            return done, None
        if gone:
            log.info("graph ingest skipped %d deleted conversation(s) user=%s", len(gone), user_id)
        if held:
            log.warning("graph ingest held %d message(s) user=%s (관리자 화면에서 다시 시도)", held, user_id)
        if stopped is not None:
            log.info(
                "graph ingest limited user=%s reason=%s done=%d left=%d",
                user_id,
                stopped,
                done,
                len(pending) - done,
            )
            await self._job(user_id, job_id, "limited", done, stopped)
            return done, stopped
        await self._job(user_id, job_id, "completed", done, None)
        return done, None

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
        DETACH DELETE 한다 — 스키마·프롬프트를 바꿔 다시 뽑을 때. 실제 재수집은
        `ingest_user(user_id, enforce_caps=False)`로 (운영자 작업이라 상한을 따르지 않는다).
        """
        async with self._locks.hold(("user", user_id)):  # 진행 중인 수집과 겹치지 않게
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

    async def release_held(
        self, user_id: str | None = None, conversation_id: UUID | None = None
    ) -> dict[str, int]:
        """보류된 메시지를 풀어 다시 수집 대상으로 만든다. {사용자 id: 푼 건수}.

        인자가 없으면 모든 사용자, `user_id`·`conversation_id`를 주면 그 범위만 푼다.

        `reset`과 달리 그래프와 이미 들어간 메시지는 그대로 둔다. 풀기만 하고 수집은 하지 않는다 —
        이어서 `start_resume`을 부르거나, 다음 재시작·주기 이어받기(`resume_all`)가 넣는다.
        지금 돌고 있는 수집은 시작할 때 고른 목록만 넣으므로 잠금을 쥐지 않아도 겹치지 않는다.
        """
        return await self._conversations.release_held_graph(user_id, conversation_id)

    async def resume_users(self, user_ids: Iterable[str]) -> dict[str, int]:
        """이 사용자들의 미수집 메시지를 순서대로 이어서 넣는다 (API 경로 — 상한을 따르고 카운터에 센다).

        `resume_all`과 같은 규칙이되, 지금 수집 중인 사용자도 건너뛰지 않고 잠금을 기다렸다가 넣는다
        (도는 실행은 방금 풀린 메시지를 모른다). 예산이 0이면 모델을 부르지 않고 넘어간다 — 풀린
        메시지는 미수집으로 남아 예산이 생기면 `resume_all`이 넣는다.
        """
        done: dict[str, int] = {}
        for user_id in user_ids:
            if await self.is_limited(user_id):
                continue
            try:
                done[user_id] = await self.ingest_user(user_id)
            except Exception as exc:  # noqa: BLE001 - 한 사용자 실패가 다른 사용자를 막지 않는다
                log.warning("graph retry failed user=%s: %s", user_id, exception_kind(exc))
        if done:
            log.info("graph retry: %s", done)
        return done

    def start_resume(self, user_ids: Iterable[str]) -> bool:
        """`resume_users`를 백그라운드 태스크로 띄운다 (요청은 기다리지 않는다). 띄웠으면 True."""
        users = sorted(set(user_ids))
        if not users:
            return False
        task = asyncio.create_task(self.resume_users(users), name="graph-retry-held")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def wait_tasks(self) -> None:
        """테스트·종료용: `start_resume`으로 띄운 수집을 기다린다."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            # 이미 끝난 태스크만 있으면 gather가 양보 없이 돌아온다 — 정리 콜백(discard)이 돌 차례를 준다
            await asyncio.sleep(0)

    async def reset_conversation(
        self,
        user_id: str,
        conversation_id: UUID,
        then: Callable[[], Awaitable[None]],
        *,
        message_ids: set[UUID] | None = None,
    ) -> tuple[int, int] | None:
        """대화방 하나를 그래프에서 지우고 `then`을 실행한다. 이 사용자의 수집이 돌고 있으면 비켜 달라고 한다.

        `message_ids`를 주면 그 메시지의 에피소드만 지우고 그 메시지만 다시 수집 대상으로 돌린다 — 화자 한
        명의 역할만 바뀌었을 때 방 전체를 다시 넣지 않는다(에피소드마다 모델 비용이 든다).

        화자 역할을 고칠 때 쓴다: 에피소드 삭제 → 수집 표시 비우기 → `then`(역할 바꾸기)을 수집 잠금
        안에서 차례로 해, 그 사이에 시작한 수집이 옛 역할의 본문을 넣지 못하게 한다.

        수집이 돌고 있으면 끝나기(몇 분~몇 시간)를 기다리지 않는다: 비켜 달라는 표시를 남기면 도는 수집이
        넣던 에피소드까지만 끝내고 멈추고(그 작업은 `pending`), 잠금을 넘겨받는다 — 보통 10초 안팎이다.
        `yield_wait`초 안에 잠금을 못 잡으면 아무것도 하지 않고 None. 돌려주는 값은 `reset`과 같다.
        멈춘 수집의 남은 메시지는 부른 쪽이 다시 시작하는 수집(`ingest_user`)이 함께 넣는다 — 여기서
        실패해 부른 쪽이 수집을 시작하지 못하면 멈춘 수집을 뒤에서 다시 띄운다.
        """
        key = ("user", user_id)
        interrupted = self._locks.busy(key)
        acquired = finished = False
        self._yield[user_id] = self._yield.get(user_id, 0) + 1
        try:
            async with asyncio.timeout(self.yield_wait) as limit:
                async with self._locks.hold(key):
                    acquired = True
                    limit.reschedule(None)  # 잠금을 잡았다 — 여기서부터는 시간 제한이 없다
                    conv = await self._conversations.get(user_id, conversation_id)
                    messages = await self._conversations.list_messages(user_id, conv.id)
                    if message_ids is not None:
                        messages = [m for m in messages if m.id in message_ids]
                    names = [
                        f"{conv.source_key}:{m.source_key}"
                        for m in messages
                        if m.graph_ingested_at is not None
                    ]
                    removed = await self._store.remove_episodes(user_id, names) if names else 0
                    reset = await self._conversations.reset_graph_ingested(
                        user_id, conv.id, None if message_ids is None else [m.id for m in messages]
                    )
                    await then()
            finished = True
            return removed, reset
        except TimeoutError:
            if acquired:
                raise  # 잠금 안에서 난 시간 초과(그래프·DB) — 기다리다 포기한 것이 아니다
            log.info("graph reset gave up waiting for the running ingest user=%s", user_id)
            return None
        finally:
            self._release_yield(user_id)
            if not finished and interrupted and not self._stopping:
                # 멈춰 세웠는데 부른 쪽이 수집을 다시 시작하지 못한다 — 뒤에서 이어 넣는다
                self.start_resume([user_id])

    def _release_yield(self, user_id: str) -> None:
        left = self._yield.get(user_id, 0) - 1
        if left > 0:
            self._yield[user_id] = left
        else:
            self._yield.pop(user_id, None)

    async def resume_all(self) -> dict[str, int]:
        """서버 시작·주기 작업: 미수집 메시지가 남은 사용자마다 이어서 넣고, 작업 상태를 정리한다.

        프로세스가 죽어 pending/running인 채 남은 작업을 살린다. 사용자 단위로 순서대로(잠금 안에서) 돈다.
        상한 때문에 지금 넣을 수 없는 사용자는 건너뛰되(모델 호출 없음), 그 사용자의 pending/running
        작업은 `limited`로 바꾼다 — 재시작할 때마다 다시 돌지도, "정리 중"으로 남지도 않는다.
        """
        done: dict[str, int] = {}
        for user_id in await self._conversations.users_with_pending_graph():
            if self._locks.busy(("user", user_id)):
                continue  # 지금 수집 중이다 — 그 실행이 끝나면서 상태를 정리한다
            if self.capped:
                left, reason = await self.remaining(user_id)
                if left == 0:
                    await self._conversations.limit_stale_graph_jobs(user_id, reason or DAILY_CAP)
                    continue
            try:
                done[user_id] = await self.ingest_user(user_id)
            except Exception as exc:  # noqa: BLE001 - 한 사용자 실패가 다른 사용자를 막지 않는다
                log.warning("graph resume failed user=%s: %s", user_id, exception_kind(exc))
                continue
            await self._conversations.close_stale_graph_jobs(user_id)
        return done
