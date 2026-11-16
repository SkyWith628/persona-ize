"""PostgreSQL에서 본 데이터 API 확장 (저장소 새 메서드 + HTTP 흐름). Postgres가 없으면 건너뛴다.

인메모리와 같은 동작을 실제 SQL로 확인한다: 집계 쿼리(stats·summary), 피드백 읽기, 이력 삭제의 CASCADE,
참조로 메시지 찾기, 그래프 수집 상한의 집계, 전체 사용량 합계, 보존 기간 정리.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("sqlalchemy")
asyncpg = pytest.importorskip("asyncpg")

from app.core.config import Settings  # noqa: E402
from app.core.errors import NotFoundError  # noqa: E402
from app.db.session import create_engine, create_session_factory  # noqa: E402
from app.domain.conversation import ImportJob, NewMessage  # noqa: E402
from app.domain.persona import PersonaSettings  # noqa: E402
from app.domain.rewrite import Feedback, RewriteResult, ValidationReport  # noqa: E402
from app.graph.ingest import GraphIngestService  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.auth import PostgresAuthRepo  # noqa: E402
from app.repositories.postgres import (  # noqa: E402
    PostgresContactRepo,
    PostgresConversationRepo,
    PostgresRewriteRepo,
)
from app.repositories.usage import PostgresUsageRepo  # noqa: E402
from app.services.housekeeping import Housekeeping  # noqa: E402
from tests.pg_support import dsn as _dsn  # noqa: E402
from tests.test_graph import FakeGraphStore  # noqa: E402

pytestmark = pytest.mark.anyio

DRAFT = "오늘 정리해서 내일 오전까지 보내드릴게요."
KAKAO = """박지현 님과 카카오톡 대화
저장한 날짜 : 2026년 10월 20일 오후 3:12

2026년 10월 5일 오전 9:25, 박지현 : 민수씨, 개편은 언제까지 가능해요? 010-1234-5678로 전화 주세요.
2026년 10월 5일 오전 9:31, 회원님 : 팀장님, 이번 주 금요일은 어렵고,
다음 주 수요일까지는 가능합니다.
2026년 10월 5일 오전 9:31, 회원님 : 죄송합니다.
2026년 10월 5일 오전 9:40, 박지현 : 사진
2026년 10월 6일 오전 8:01, 박지현 : 목요일까지는 안 될까요?
2026년 10월 6일 오전 8:05, 회원님 : 팀장님, 목요일이면 테스트를 못 돌립니다.
"""
ROLES = {"회원님": "me", "박지현": "contact"}


def _user() -> str:
    return f"u-{uuid.uuid4().hex[:8]}"


def _result(text: str = "내일 오전까지 보내드리겠습니다.") -> RewriteResult:
    return RewriteResult(
        status="recommended",
        result_text=text,
        intent="report",
        preserved_items=[],
        validation=ValidationReport(passed=True),
        attempts=1,
        model_version="fake-echo",
        prompt_version="rewrite-v3",
        retrieval_version="none",
        token_usage={"input_tokens": 10, "output_tokens": 5},
        latency_ms=12,
    )


@pytest.fixture
async def repos(pg_url: str):  # noqa: ANN201
    engine = create_engine(pg_url)
    sessions = create_session_factory(engine)
    try:
        yield (
            PostgresContactRepo(sessions),
            PostgresRewriteRepo(sessions),
            PostgresConversationRepo(sessions),
            PostgresUsageRepo(sessions),
            PostgresAuthRepo(sessions),
        )
    finally:
        await engine.dispose()


async def _fetch(pg_url: str, query: str, *args: object) -> int:
    conn = await asyncpg.connect(_dsn(pg_url))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _execute(pg_url: str, query: str, *args: object) -> None:
    conn = await asyncpg.connect(_dsn(pg_url))
    try:
        await conn.execute(query, *args)
    finally:
        await conn.close()


async def test_contact_update_and_persona_listing(repos) -> None:  # noqa: ANN001
    contacts, *_ = repos
    a, b = _user(), _user()
    c = await contacts.create(a, "이서연", "colleague")
    bare = await contacts.create(a, "준호", "family")
    assert await contacts.latest_persona_versions(a) == {}
    for formality in (3, 4, 5):
        await contacts.save_persona(a, c.id, PersonaSettings(formality=formality), None)

    renamed = await contacts.update(a, c.id, alias="이서연 파트장")
    assert (renamed.alias, renamed.relationship_type) == ("이서연 파트장", "colleague")
    promoted = await contacts.update(a, c.id, relationship_type="superior")
    assert (promoted.alias, promoted.relationship_type) == ("이서연 파트장", "superior")
    assert (await contacts.update(a, c.id)).alias == "이서연 파트장"  # 바꿀 것이 없으면 그대로
    assert (await contacts.get(a, c.id)).relationship_type == "superior"
    with pytest.raises(NotFoundError):
        await contacts.update(b, c.id, alias="남의 것")

    versions = await contacts.list_personas(a, c.id)
    assert [(v.version, v.settings.formality) for v in versions] == [(3, 5), (2, 4), (1, 3)]
    assert [v.version for v in await contacts.list_personas(a, c.id, limit=2)] == [3, 2]
    assert await contacts.list_personas(a, bare.id) == []
    with pytest.raises(NotFoundError):
        await contacts.list_personas(b, c.id)
    assert await contacts.latest_persona_versions(a) == {c.id: 3}
    assert await contacts.latest_persona_versions(b) == {}


async def test_rewrite_channel_feedback_paging_delete_and_stats(repos, pg_url: str) -> None:  # noqa: ANN001
    contacts, rewrites, *_ = repos
    a, b = _user(), _user()
    c1 = await contacts.create(a, "박지현", "superior")
    c2 = await contacts.create(a, "최유진", "friend")

    async def save(contact_id, key=None, channel=None):  # noqa: ANN001, ANN202
        extra = {"channel": channel} if channel else {}
        return await rewrites.save(
            user_id=a,
            contact_id=contact_id,
            persona_version_id=None,
            draft_version=1,
            input_hash="h" * 64,
            idempotency_key=key,
            result=_result(),
            condition="C",
            **extra,
        )

    first = await save(c1.id, key="k1")
    second = await save(c1.id, channel="mobile")
    third = await save(c2.id, channel="desktop")
    assert (first.channel, second.channel, third.channel) == ("web", "mobile", "desktop")  # 기본값 web
    assert (await rewrites.get(a, second.id)).channel == "mobile"
    assert first.feedback is None

    # 피드백(선택형 사유 포함)은 조회·목록·멱등 조회에 함께 온다
    fb = Feedback(request_id=first.id, accepted=False, reason="너무 딱딱함", reason_code="too_stiff")
    assert await rewrites.save_feedback(a, fb) == fb
    got = await rewrites.get(a, first.id)
    assert got.feedback is not None and got.feedback.created_at is not None
    assert (got.feedback.accepted, got.feedback.reason_code, got.feedback.reason) == (
        False,
        "too_stiff",
        "너무 딱딱함",
    )
    assert (await rewrites.find_by_idempotency(a, "k1")).feedback.reason_code == "too_stiff"
    listed = await rewrites.list(a)
    assert [r.id for r in listed] == [third.id, second.id, first.id]
    assert [r.feedback.reason_code if r.feedback else None for r in listed] == [None, None, "too_stiff"]
    await rewrites.save_feedback(a, Feedback(request_id=first.id, accepted=True))  # 덮어쓰기
    assert (await rewrites.get(a, first.id)).feedback.reason_code is None

    # before: created_at < before
    assert [r.id for r in await rewrites.list(a, before=third.created_at)] == [second.id, first.id]
    assert [r.id for r in await rewrites.list(a, c1.id, 1, second.created_at)] == [first.id]
    assert await rewrites.list(a, before=first.created_at) == []

    stats = await rewrites.stats_by_contact(a)
    assert stats == {c1.id: (2, second.created_at), c2.id: (1, third.created_at)}
    assert await rewrites.stats_by_contact(b) == {}

    # 한 건 삭제: 결과·피드백 행까지 사라진다
    with pytest.raises(NotFoundError):
        await rewrites.delete(b, first.id)
    await rewrites.delete(a, first.id)
    with pytest.raises(NotFoundError):
        await rewrites.get(a, first.id)
    for table in ("rewrite_requests", "rewrite_results", "feedback"):
        key = "id" if table == "rewrite_requests" else "request_id"
        assert await _fetch(pg_url, f"SELECT count(*) FROM {table} WHERE {key} = $1", first.id) == 0  # noqa: S608
    assert await rewrites.find_by_idempotency(a, "k1") is None

    # 보존 기간 정리: before보다 오래된 요청만 (다른 사용자의 최근 행은 건드리지 않는다)
    await rewrites.save_feedback(a, Feedback(request_id=second.id, accepted=True))
    old = datetime(2000, 1, 1, tzinfo=UTC)
    await _execute(pg_url, "UPDATE rewrite_requests SET created_at = $1 WHERE id = $2", old, second.id)
    assert await rewrites.purge_before(datetime(2001, 1, 1, tzinfo=UTC)) == 1
    assert [r.id for r in await rewrites.list(a)] == [third.id]
    assert await _fetch(pg_url, "SELECT count(*) FROM feedback WHERE request_id = $1", second.id) == 0


async def test_conversation_stats_refs_and_graph_counts(repos) -> None:  # noqa: ANN001
    contacts, _, convs, *_ = repos
    a, b = _user(), _user()
    boss = await contacts.create(a, "박지현", "superior")
    friend = await contacts.create(a, "최유진", "friend")
    lonely = await contacts.create(a, "이서연", "colleague")
    c1 = await convs.create(a, boss.id, "팀장 방", "kakao", "c1")
    c2 = await convs.create(a, boss.id, "두 번째 방", "kakao", "c:2")  # 키에 ':'이 있어도 된다
    c3 = await convs.create(a, friend.id, "빈 방", "kakao", "c3")
    other = await contacts.create(b, "남", "friend")
    cb = await convs.create(b, other.id, "남의 방", "kakao", "c1")  # 다른 사용자의 같은 키

    def msg(key: str, role: str, minute: int) -> NewMessage:
        when = datetime(2026, 10, 5, 0, minute, tzinfo=UTC)
        return NewMessage(source_key=key, speaker_role=role, sent_at=when, text=f"{key} 본문")  # type: ignore[arg-type]

    await convs.add_messages(a, c1.id, [msg("m1", "contact", 1), msg("m2", "me", 2), msg("m3", "me", 3)])
    await convs.add_messages(a, c2.id, [msg("m1", "me", 4)])
    await convs.add_messages(b, cb.id, [msg("m1", "me", 5)])

    empty = await convs.conversation_stats(a, c3.id)
    assert (empty.messages, empty.episodes, empty.first_message_at, empty.graph_pending) == (0, 0, None, 0)
    with pytest.raises(NotFoundError):
        await convs.conversation_stats(b, c1.id)

    # 그래프 수집 표시: 2031년 시각을 써서 다른 테스트의 행과 섞이지 않게 센다
    future = datetime(2031, 1, 1, tzinfo=UTC)
    ids = [m.id for m in await convs.list_messages(a, c1.id)]
    await convs.mark_graph_ingested(a, ids[:2], future)
    st = await convs.conversation_stats(a, c1.id)
    assert (st.messages, st.my_messages, st.graph_ingested, st.graph_pending) == (3, 2, 2, 1)
    assert st.first_message_at == datetime(2026, 10, 5, 0, 1, tzinfo=UTC)
    assert st.last_message_at == datetime(2026, 10, 5, 0, 3, tzinfo=UTC)
    assert await convs.count_graph_ingested(a) == 2
    assert await convs.count_graph_ingested(b) == 0
    assert await convs.count_graph_ingested(None, future) == 2
    assert await convs.count_graph_ingested(a, future + timedelta(seconds=1)) == 0

    by_contact = await convs.contact_stats(a)
    assert set(by_contact) == {boss.id, friend.id}  # 대화방이 없는 상대(lonely)는 빠진다
    assert lonely.id not in by_contact
    bs = by_contact[boss.id]
    assert (bs.conversations, bs.messages, bs.my_messages, bs.graph_ingested, bs.episodes) == (2, 4, 3, 2, 0)
    fs = by_contact[friend.id]
    assert (fs.conversations, fs.messages, fs.my_messages) == (1, 0, 0)
    assert list(await convs.contact_stats(a, friend.id)) == [friend.id]
    assert await convs.contact_stats(_user()) == {}

    # 참조(`대화방키:메시지키`)로 지금 남아 있는 메시지 찾기 — 내 것만, 없는 것은 빠진다
    found = await convs.find_messages(a, ["c1:m2", "c:2:m1", "c1:zzz", "nope:m1"])
    assert {ref: m.text for ref, m in found.items()} == {"c1:m2": "m2 본문", "c:2:m1": "m1 본문"}
    assert (await convs.find_messages(b, ["c1:m1"]))["c1:m1"].user_id == b
    assert await convs.find_messages(a, []) == {}
    await convs.delete_conversation(a, c1.id)
    assert list(await convs.find_messages(a, ["c1:m2", "c:2:m1"])) == ["c:2:m1"]

    # 가장 최근 작업, limited 작업 정리
    assert await convs.latest_job(a, c2.id) is None
    now = datetime.now(UTC)
    for i, status in enumerate(("completed", "limited")):
        await convs.save_job(
            ImportJob(
                id=uuid.uuid4(),
                user_id=a,
                conversation_id=c2.id,
                source_hash=f"{i}" * 64,
                status="completed",
                graph_status=status,  # type: ignore[arg-type]
                graph_error="user_cap" if status == "limited" else None,
                created_at=now + timedelta(seconds=i),
            )
        )
    latest = await convs.latest_job(a, c2.id)
    assert latest is not None and (latest.graph_status, latest.graph_error) == ("limited", "user_cap")
    assert await convs.close_stale_graph_jobs(a) == 0  # c2에 미수집 메시지가 남아 있다
    pending = [m.id for m in await convs.list_messages(a, c2.id)]
    await convs.mark_graph_ingested(a, pending, future)
    assert await convs.close_stale_graph_jobs(a) == 1
    closed = await convs.latest_job(a, c2.id)
    assert closed is not None and (closed.graph_status, closed.graph_error) == ("completed", None)


async def test_usage_totals_sum_all_users(repos) -> None:  # noqa: ANN001
    *_, usage, _ = repos
    day = date(2031, 3, 1) + timedelta(days=uuid.uuid4().int % 300)  # 다른 테스트·재실행과 겹치지 않는 날
    assert (await usage.totals(day)).rewrites == 0
    u1, u2 = _user(), _user()
    for user, n in ((u1, 2), (u2, 1)):
        for _ in range(n):
            await usage.reserve_rewrite(user, day, 10)
    await usage.add_tokens(u1, day, 100, 40)
    await usage.add_tokens(u2, day, 7, 3)
    await usage.reserve_rewrite(u1, day + timedelta(days=1), 10)  # 다른 날은 섞이지 않는다
    total = await usage.totals(day)
    assert (total.user_id, total.rewrites, total.input_tokens, total.output_tokens) == ("*", 3, 107, 43)
    assert total.total_tokens == 150


async def test_graph_caps_count_from_postgres(repos) -> None:  # noqa: ANN001
    contacts, _, convs, *_ = repos
    a = _user()
    boss = await contacts.create(a, "박지현", "superior")
    conv = await convs.create(a, boss.id, "팀장 방", "kakao", "c1", "김민수")
    await convs.add_messages(
        a,
        conv.id,
        [
            NewMessage(
                source_key=f"m{i}",
                speaker_role="me",
                sent_at=datetime(2026, 10, 5, 0, i, tzinfo=UTC),
                text=f"{i}번째 메시지입니다.",
            )
            for i in range(1, 4)
        ],
    )
    job = await convs.save_job(
        ImportJob(
            id=uuid.uuid4(),
            user_id=a,
            conversation_id=conv.id,
            source_hash="c" * 64,
            status="completed",
            graph_status="pending",
            created_at=datetime.now(UTC),
        )
    )
    store = FakeGraphStore()
    ingest = GraphIngestService(store, convs, contacts, user_message_cap=2)
    assert await ingest.remaining(a) == (2, "user_cap")
    assert await ingest.ingest_user(a, job.id) == 2
    assert [x["name"] for x in store.added] == ["c1:m2", "c1:m3"]  # 가장 최근 것을 예산만큼, 시각 순으로
    limited = await convs.get_job(a, job.id)
    assert (limited.graph_status, limited.graph_episodes, limited.graph_error) == ("limited", 2, "user_cap")
    assert await ingest.is_limited(a) is True
    # 재시작 이어받기: 이 사용자는 미수집 메시지가 있어도 다시 돌리지 않는다
    assert a in await convs.users_with_pending_graph()
    await ingest.ingest_user(a)  # 직접 불러도 예산이 없으면 모델을 부르지 않는다
    assert len(store.added) == 2
    # 상한이 없는 서비스(스크립트·상한 0)는 남은 것을 마저 넣는다
    assert await GraphIngestService(store, convs, contacts).ingest_user(a) == 1
    assert (await convs.get_job(a, job.id)).graph_status == "completed"


async def test_graph_usage_counter_survives_deletion_on_postgres(repos, pg_url: str) -> None:  # noqa: ANN001
    """그래프에 넣은 양은 usage_counters.graph_episodes(사용자·일)에 남는다 — 대화를 지워도 줄지 않는다."""
    contacts, _, convs, usage, _ = repos
    a, b = _user(), _user()
    boss = await contacts.create(a, "박지현", "superior")
    store = FakeGraphStore()
    ingest = GraphIngestService(store, convs, contacts, user_message_cap=6)
    day, _ = ingest._today()
    before_all = await convs.graph_usage(day)

    async def import_room(key: str, texts: list[str]):  # noqa: ANN202
        conv = await convs.create(a, boss.id, key, "kakao", key, "김민수")
        await convs.add_messages(
            a,
            conv.id,
            [
                NewMessage(
                    source_key=f"m{i}",
                    speaker_role="me",
                    sent_at=datetime(2026, 10, 5, 0, i, tzinfo=UTC),
                    text=text,
                )
                for i, text in enumerate(texts)
            ],
        )
        return conv

    # 긴 메시지는 더 센다: 2,500자 = 3, 짧은 것 = 1 (SQL의 합계가 파이썬 쪽 식과 같아야 한다)
    c1 = await import_room("c1", ["짧은 말", "가" * 2500, "나" * 999])
    assert await ingest.ingest_user(a) == 3
    assert await convs.graph_units_in_graph(a) == 5
    assert await convs.graph_usage(day, a) == 5
    assert await convs.graph_usage(day) == before_all + 5
    assert (await usage.get(a, day)).graph_episodes == 5  # 첨삭 사용량과 같은 행에 있다
    assert await ingest.remaining(a) == (1, "user_cap")

    # 지우면 그래프에 든 양은 0이 되지만 오늘 넣은 양은 그대로다
    await convs.delete_conversation(a, c1.id)
    assert await convs.graph_units_in_graph(a) == 0
    assert await convs.graph_usage(day, a) == 5
    assert await ingest.remaining(a) == (1, "user_cap")
    c2 = await import_room("c2", ["다시 올린 말 하나", "다시 올린 말 둘", "다시 올린 말 셋"])
    assert await ingest.ingest_user(a) == 1  # 남은 1만큼, 가장 최근 것
    assert [x["name"] for x in store.added][-1] == "c2:m2"
    assert await ingest.remaining(a) == (0, "user_cap")

    # 예약을 돌려놓기(실패한 에피소드): 음수로 더하면 줄고, 0 아래로는 내려가지 않는다
    await convs.add_graph_usage(b, day, 3)
    await convs.add_graph_usage(b, day, -1)
    assert await convs.graph_usage(day, b) == 2
    await convs.add_graph_usage(b, day, -10)
    assert await convs.graph_usage(day, b) == 0
    # 첨삭 카운터와 서로 건드리지 않는다
    await usage.reserve_rewrite(a, day, 10)
    await usage.add_tokens(a, day, 7, 3)
    row = await usage.get(a, day)
    assert (row.rewrites, row.input_tokens, row.output_tokens, row.graph_episodes) == (1, 7, 3, 6)

    # 상한 때문에 남은 작업 정리: 미수집 메시지가 남은 방의 pending/running 작업만 limited가 된다
    def job_for(conv_id: uuid.UUID, status: str) -> ImportJob:
        return ImportJob(
            id=uuid.uuid4(),
            user_id=a,
            conversation_id=conv_id,
            source_hash=uuid.uuid4().hex * 2,
            status="completed",
            graph_status=status,  # type: ignore[arg-type]
            created_at=datetime.now(UTC),
        )

    c3 = await import_room("c3", ["전부 들어간 방"])
    await convs.mark_graph_ingested(a, [m.id for m in await convs.list_messages(a, c3.id)], datetime.now(UTC))
    stale = await convs.save_job(job_for(c2.id, "running"))
    waiting = await convs.save_job(job_for(c2.id, "pending"))
    failed = await convs.save_job(job_for(c2.id, "failed"))
    done_room = await convs.save_job(job_for(c3.id, "running"))
    assert await convs.limit_stale_graph_jobs(a, "user_cap") == 2
    states = [(await convs.get_job(a, j.id)).graph_status for j in (stale, waiting, failed, done_room)]
    assert states == ["limited", "limited", "failed", "running"]
    assert (await convs.get_job(a, stale.id)).graph_error == "user_cap"


def test_data_api_flow_on_postgres(pg_url: str) -> None:
    """HTTP → 서비스 → PostgreSQL 배선.

    미리보기 → 원문 가져오기 → 진행 → auto 첨삭 → 근거 → 요약 → 내보내기 → 삭제.
    """
    settings = Settings(_env_file=None, llm_provider="fake", database_url=pg_url, embedding_provider="fake")  # type: ignore[call-arg]
    h, stranger = {"X-User-Id": _user()}, {"X-User-Id": _user()}
    with TestClient(create_app(settings)) as client:
        contact = client.post(
            "/v1/contacts",
            json={"alias": "박지현", "relationship_type": "superior", "use_default_persona": True},
            headers=h,
        )
        assert contact.status_code == 201, contact.text
        cid = contact.json()["id"]
        assert contact.json()["persona_version"] == 1
        assert set(client.get("/v1/persona-defaults", headers=h).json()) >= {"superior", "friend"}

        preview = client.post("/v1/imports/parse", json={"text": KAKAO}, headers=h)
        assert preview.status_code == 200, preview.text
        assert (
            preview.json()["variant"],
            preview.json()["messages_total"],
            preview.json()["masked_pii"],
        ) == (
            "android",
            5,
            1,
        )
        conv_id = client.post("/v1/conversations", json={"contact_id": cid}, headers=h).json()["id"]
        url = f"/v1/conversations/{conv_id}/imports/raw"
        assert (
            client.post(url, json={"text": KAKAO, "speaker_map": {"회원님": "me"}}, headers=h).status_code
            == 422
        )
        job = client.post(url, json={"format": "kakao", "text": KAKAO, "speaker_map": ROLES}, headers=h)
        assert job.status_code == 202, job.text
        assert (job.json()["imported"], job.json()["duplicates"], job.json()["episodes"]) == (5, 0, 2)
        assert (
            client.post(url, json={"text": KAKAO, "speaker_map": ROLES}, headers=h).json()["id"]
            == job.json()["id"]
        )

        msgs = client.get(f"/v1/conversations/{conv_id}/messages", headers=h).json()
        assert [m["source_key"] for m in msgs] == [s["id"] for s in preview.json()["sample"]]
        assert [m["speaker_role"] for m in msgs] == [
            "contact",
            "me",
            "me",
            "contact",
            "me",
        ]  # 같은 분 안 순서 유지
        assert "[전화번호]" in msgs[0]["text"]

        stats = client.get(f"/v1/conversations/{conv_id}/stats", headers=h).json()
        assert (stats["messages"], stats["my_messages"], stats["episodes"], stats["graph_enabled"]) == (
            5,
            3,
            2,
            False,
        )
        assert stats["latest_job"]["id"] == job.json()["id"]
        assert client.get(f"/v1/conversations/{conv_id}/stats", headers=stranger).status_code == 404

        rec = client.post(
            "/v1/rewrites",
            json={
                "contact_id": cid,
                "draft_text": "목요일이면 테스트를 못 돌립니다.",
                "condition": "auto",
                "adjust": "shorter",
                "channel": "mobile",
            },
            headers={**h, "Idempotency-Key": "flow-1"},
        )
        assert rec.status_code == 201, rec.text
        body = rec.json()
        assert (body["condition"], body["channel"], body["result"]["adjust"]) == ("C", "mobile", "shorter")
        assert len(body["result"]["evidence_ids"]) == 1 and body["feedback"] is None
        rid = body["id"]
        assert client.get(f"/v1/contacts/{cid}/persona", headers=h).json()["settings"]["length"] == "same"

        evidence = client.get(f"/v1/rewrites/{rid}/evidence", headers=h)
        assert evidence.status_code == 200, evidence.text
        assert evidence.json()["missing"] == 0 and len(evidence.json()["items"]) == 1
        assert evidence.json()["items"][0]["messages"][-1]["speaker_role"] == "me"
        assert client.get(f"/v1/rewrites/{rid}/evidence", headers=stranger).status_code == 404

        fb = client.post(
            f"/v1/rewrites/{rid}/feedback", json={"accepted": False, "reason_code": "too_short"}, headers=h
        )
        assert fb.status_code == 204
        assert client.get(f"/v1/rewrites/{rid}", headers=h).json()["feedback"]["reason_code"] == "too_short"

        for i in range(2):
            more = client.post(
                "/v1/rewrites", json={"contact_id": cid, "draft_text": f"{i}번 문장입니다."}, headers=h
            )
            assert more.status_code == 201, more.text
        page = client.get("/v1/rewrites?limit=2", headers=h).json()
        older = client.get(f"/v1/rewrites?limit=2&before={quote(page[-1]['created_at'])}", headers=h).json()
        assert [r["id"] for r in older] == [rid]

        summary = client.get("/v1/data/summary", headers=h).json()
        row = summary["contacts"][0]
        assert (row["contact_id"], row["persona_version"], row["conversations"]) == (cid, 1, 1)
        assert (row["messages"], row["my_messages"], row["graph_ingested"], row["rewrites"]) == (5, 3, 0, 3)
        assert summary["totals"] == {"contacts": 1, "conversations": 1, "messages": 5, "rewrites": 3}
        assert client.get("/v1/data/summary", headers=stranger).json()["totals"]["contacts"] == 0

        suggestion = client.get(f"/v1/contacts/{cid}/persona/suggestion", headers=h).json()
        # 내 메시지 3건: 표본이 작아 겉모습 추정(messages)은 하지 않는다 (집계 쿼리가 내 메시지만 세는지는
        # 여기서 본다). 말투 프로필(register)은 건수 문턱이 없어 두 번 쓴 호칭을 제안한다 (설계 3.2).
        assert suggestion["based_on_messages"] == 3
        assert [(s["field"], s["value"], s["source"]) for s in suggestion["signals"]] == [
            ("address_term", "팀장님", "register")
        ]

        export = client.get("/v1/export", headers=h)
        assert export.status_code == 200 and "attachment" in export.headers["content-disposition"]
        exported = export.json()["contacts"][0]
        assert len(exported["conversations"][0]["messages"]) == 5 and len(exported["rewrites"]) == 3
        assert exported["rewrites"][-1]["feedback"]["reason_code"] == "too_short"

        patched = client.patch(f"/v1/contacts/{cid}", json={"alias": "지현 팀장님"}, headers=h)
        assert patched.status_code == 200 and patched.json()["alias"] == "지현 팀장님"
        assert [
            v["version"] for v in client.get(f"/v1/contacts/{cid}/persona/versions", headers=h).json()
        ] == [1]

        # 대화방을 지우면 근거 원문도 사라지고, 다음 auto는 B로 돌아간다
        assert client.delete(f"/v1/conversations/{conv_id}", headers=h).status_code == 200
        assert client.get(f"/v1/rewrites/{rid}/evidence", headers=h).json() == {
            "items": [],
            "missing": 1,
            "refs_recorded": True,
        }
        back = client.post(
            "/v1/rewrites", json={"contact_id": cid, "draft_text": DRAFT, "condition": "auto"}, headers=h
        )
        assert back.json()["condition"] == "B"

        assert client.delete(f"/v1/rewrites/{rid}", headers=h).status_code == 204
        assert client.get(f"/v1/rewrites/{rid}", headers=h).status_code == 404
        assert client.delete(f"/v1/rewrites?contact_id={cid}", headers=h).json() == {"deleted": 3}
        assert client.get("/v1/rewrites", headers=h).json() == []


def test_run_housekeeping_applies_retention_on_postgres(pg_url: str) -> None:
    from app.services.housekeeping import run_housekeeping

    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        database_url=pg_url,
        embedding_provider="fake",
        rewrite_retention_days=30,
    )  # type: ignore[call-arg]
    h = {"X-User-Id": _user()}
    with TestClient(create_app(settings)) as client:
        cid = client.post(
            "/v1/contacts",
            json={"alias": "박지현", "relationship_type": "superior", "use_default_persona": True},
            headers=h,
        ).json()["id"]
        ids = [
            client.post("/v1/rewrites", json={"contact_id": cid, "draft_text": DRAFT}, headers=h).json()["id"]
            for _ in range(2)
        ]
        client.post(f"/v1/rewrites/{ids[0]}/feedback", json={"accepted": True}, headers=h)
        assert client.get("/v1/data/summary", headers=h).json()["retention_days"] == 30

    async def _age_and_run() -> dict[str, int]:
        await _execute(
            pg_url,
            "UPDATE rewrite_requests SET created_at = now() - interval '45 days' WHERE id = $1",
            uuid.UUID(ids[0]),
        )
        from app.api.deps import build_state

        state = build_state(settings)
        try:
            return await run_housekeeping(state)
        finally:
            await state.close()

    result = asyncio.run(_age_and_run())
    assert set(result) == {"sessions", "import_jobs", "usage_rows", "rewrites"}
    assert result["rewrites"] >= 1  # 이 테스트가 묵힌 한 건 (다른 테스트의 행은 최근이라 남는다)

    async def _left() -> tuple[int, int, int]:
        old = await _fetch(pg_url, "SELECT count(*) FROM rewrite_requests WHERE id = $1", uuid.UUID(ids[0]))
        fb = await _fetch(pg_url, "SELECT count(*) FROM feedback WHERE request_id = $1", uuid.UUID(ids[0]))
        new = await _fetch(pg_url, "SELECT count(*) FROM rewrite_requests WHERE id = $1", uuid.UUID(ids[1]))
        return old, fb, new

    assert asyncio.run(_left()) == (0, 0, 1)


async def test_housekeeping_class_keeps_old_signature(repos, pg_url: str) -> None:  # noqa: ANN001
    """예전 호출 방식(`Housekeeping(auth, convs, usage).run(now)`)은 첨삭 이력을 건드리지 않는다."""
    contacts, rewrites, convs, usage, auth = repos
    a = _user()
    c = await contacts.create(a, "박지현", "superior")
    rec = await rewrites.save(
        user_id=a,
        contact_id=c.id,
        persona_version_id=None,
        draft_version=1,
        input_hash="h" * 64,
        idempotency_key=None,
        result=_result(),
    )
    old = datetime(2000, 1, 1, tzinfo=UTC)
    await _execute(pg_url, "UPDATE rewrite_requests SET created_at = $1 WHERE id = $2", old, rec.id)
    try:
        result = await Housekeeping(auth, convs, usage).run(datetime.now(UTC))
        assert result.rewrites == 0 and result.as_dict()["rewrites"] == 0
        assert (await rewrites.get(a, rec.id)).created_at == old  # 아주 오래된 이력도 그대로
    finally:
        await rewrites.delete(a, rec.id)  # 다른 테스트의 보존 기간 집계에 섞이지 않게 치운다
