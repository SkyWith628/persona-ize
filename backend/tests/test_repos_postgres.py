"""PostgreSQL 저장소 통합 테스트. 로컬 Postgres가 없으면 건너뛴다 (`docker compose up -d postgres`).

같은 서버에 `persona_ize_test` DB를 만들어 public 스키마를 비우고 Alembic head까지 올린 뒤 테스트한다.
`.env`의 DATABASE_URL이 가리키는 운영 DB는 건드리지 않는다. `TEST_DATABASE_URL`을 직접 주면 그 DB를 쓰되,
이름이 `_test`로 끝나야 한다 (매 실행마다 스키마를 지우기 때문).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("sqlalchemy")
asyncpg = pytest.importorskip("asyncpg")

from app.ai.embeddings import FakeEmbedder  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.errors import NotFoundError, VersionConflictError  # noqa: E402
from app.db.migrate import upgrade_to_head  # noqa: E402
from app.db.session import create_engine, create_session_factory  # noqa: E402
from app.domain.persona import PersonaSettings  # noqa: E402
from app.domain.rewrite import Feedback, RewriteResult, ValidationReport  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.postgres import (  # noqa: E402
    PostgresContactRepo,
    PostgresConversationRepo,
    PostgresRewriteRepo,
)

pytestmark = pytest.mark.anyio

TEST_DB = "persona_ize_test"


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _urls() -> tuple[str, str] | None:
    """(관리용 접속 URL, 테스트 DB URL). DATABASE_URL이 없으면 None."""
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        if not explicit.rpartition("/")[2].split("?")[0].endswith("_test"):
            raise RuntimeError("TEST_DATABASE_URL의 DB 이름은 _test로 끝나야 합니다 (스키마를 지웁니다).")
        return explicit, explicit
    base = Settings().database_url  # .env를 읽는다
    if not base:
        return None
    head, _, _ = base.rpartition("/")
    return base, f"{head}/{TEST_DB}"


async def _prepare(admin_url: str, test_url: str) -> None:
    if admin_url != test_url:
        conn = await asyncpg.connect(_dsn(admin_url), timeout=3)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
        finally:
            await conn.close()
    conn = await asyncpg.connect(_dsn(test_url), timeout=3)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def pg_url() -> str:
    urls = _urls()
    if urls is None:
        pytest.skip("DATABASE_URL이 비어 있어 PostgreSQL 테스트를 건너뜁니다.")
    admin_url, test_url = urls
    try:
        asyncio.run(_prepare(admin_url, test_url))
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL 연결 실패({type(exc).__name__}) — `docker compose up -d postgres` 후 다시.")
    upgrade_to_head(test_url)
    return test_url


@pytest.fixture
async def repos(pg_url: str):  # noqa: ANN201
    engine = create_engine(pg_url)
    sessions = create_session_factory(engine)
    try:
        yield PostgresContactRepo(sessions), PostgresRewriteRepo(sessions)
    finally:
        await engine.dispose()


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
        prompt_version="rewrite-v1",
        retrieval_version="none",
        token_usage={"input": 10, "output": 5},
        latency_ms=12,
    )


async def test_contact_crud_and_isolation(repos) -> None:  # noqa: ANN001
    contacts, _ = repos
    a, b = _user(), _user()
    c1 = await contacts.create(a, "박지현", "superior")
    c2 = await contacts.create(a, "준호", "family")

    listed = await contacts.list(a)
    assert [c.id for c in listed] == [c1.id, c2.id]  # created_at 순
    assert (await contacts.get(a, c1.id)).alias == "박지현"
    assert await contacts.list(b) == []
    with pytest.raises(NotFoundError):
        await contacts.get(b, c1.id)
    with pytest.raises(NotFoundError):
        await contacts.latest_persona(b, c1.id)


async def test_persona_versions_and_conflict(repos) -> None:  # noqa: ANN001
    contacts, _ = repos
    a = _user()
    c = await contacts.create(a, "박지현", "superior")
    assert await contacts.latest_persona(a, c.id) is None

    v1 = await contacts.save_persona(a, c.id, PersonaSettings(formality=5, address_term="팀장님"), 0)
    assert v1.version == 1
    v2 = await contacts.save_persona(a, c.id, PersonaSettings(formality=4), 1)
    assert v2.version == 2
    assert (await contacts.latest_persona(a, c.id)).version == 2
    assert (await contacts.get_persona(a, c.id, 1)).settings.address_term == "팀장님"

    with pytest.raises(VersionConflictError):
        await contacts.save_persona(a, c.id, PersonaSettings(), 1)  # 현재 2
    with pytest.raises(NotFoundError):
        await contacts.get_persona(a, c.id, 9)
    # expected_version 생략은 무조건 추가
    assert (await contacts.save_persona(a, c.id, PersonaSettings(), None)).version == 3


async def test_rewrite_save_get_idempotency_feedback(repos) -> None:  # noqa: ANN001
    contacts, rewrites = repos
    a, b = _user(), _user()
    c = await contacts.create(a, "박지현", "superior")
    pv = await contacts.save_persona(a, c.id, PersonaSettings(formality=5), 0)

    assert await rewrites.find_by_idempotency(a, "k1") is None
    rec = await rewrites.save(
        user_id=a,
        contact_id=c.id,
        persona_version_id=pv.id,
        draft_version=1,
        input_hash="h" * 64,
        idempotency_key="k1",
        result=_result(),
    )
    got = await rewrites.get(a, rec.id)
    assert got.result == rec.result  # JSONB 왕복 후 동일
    assert got.persona_version_id == pv.id
    assert (await rewrites.find_by_idempotency(a, "k1")).id == rec.id
    assert await rewrites.find_by_idempotency(b, "k1") is None
    with pytest.raises(NotFoundError):
        await rewrites.get(b, rec.id)

    # 같은 사용자·같은 키 재삽입은 유일 제약 → 409. 키 없는 요청은 여러 번 저장된다.
    with pytest.raises(VersionConflictError):
        await rewrites.save(
            user_id=a,
            contact_id=c.id,
            persona_version_id=None,
            draft_version=1,
            input_hash="x" * 64,
            idempotency_key="k1",
            result=_result(),
        )
    for _ in range(2):
        await rewrites.save(
            user_id=a,
            contact_id=c.id,
            persona_version_id=None,
            draft_version=1,
            input_hash="y" * 64,
            idempotency_key=None,
            result=_result(),
        )

    # D 검색기 버전 문자열은 64자를 넘는다 (E2E에서 500, 0004로 200자)
    long_version = "knn-style-v1/st:intfloat/multilingual-e5-small:384:v1+graph-context-v1"
    assert len(long_version) > 64
    long_rec = await rewrites.save(
        user_id=a,
        contact_id=c.id,
        persona_version_id=None,
        draft_version=1,
        input_hash="z" * 64,
        idempotency_key=None,
        result=_result().model_copy(update={"retrieval_version": long_version}),
    )
    assert (await rewrites.get(a, long_rec.id)).result.retrieval_version == long_version

    fb = Feedback(request_id=rec.id, accepted=False, edited_text="수정본", reason="너무 격식")
    assert await rewrites.save_feedback(a, fb) == fb
    assert await rewrites.save_feedback(a, Feedback(request_id=rec.id, accepted=True)) is not None  # 덮어쓰기
    with pytest.raises(NotFoundError):
        await rewrites.save_feedback(b, fb)


async def test_usage_reserve_is_atomic_and_counts_tokens(pg_url: str) -> None:
    import asyncio as _asyncio
    from datetime import date

    from app.repositories.usage import PostgresUsageRepo

    engine = create_engine(pg_url)
    repo = PostgresUsageRepo(create_session_factory(engine))
    try:
        user, day = _user(), date(2026, 11, 9)
        assert (await repo.get(user, day)).rewrites == 0
        # 동시 10개 예약, 한도 3 → 정확히 3개만 성공
        results = await _asyncio.gather(*(repo.reserve_rewrite(user, day, 3) for _ in range(10)))
        assert sum(r is not None for r in results) == 3
        assert (await repo.get(user, day)).rewrites == 3
        assert await repo.reserve_rewrite(user, day, 3) is None
        assert await repo.reserve_rewrite(user, day, 0) is None  # 한도 0은 전부 차단
        after = await repo.add_tokens(user, day, 120, 45)
        after = await repo.add_tokens(user, day, 10, 5)
        assert (after.input_tokens, after.output_tokens, after.total_tokens) == (130, 50, 180)
        # 다른 날은 별도
        assert (await repo.get(user, date(2026, 11, 10))).rewrites == 0
    finally:
        await engine.dispose()


def test_api_vertical_flow_on_postgres(pg_url: str) -> None:
    """HTTP → 서비스 → PostgreSQL 저장소 배선 확인. 로직은 인메모리 테스트(test_api.py)와 같다."""
    settings = Settings(_env_file=None, llm_provider="fake", database_url=pg_url)  # type: ignore[call-arg]
    headers = {"X-User-Id": _user()}
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json()["storage"] == "postgres"
        cid = client.post(
            "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=headers
        ).json()["id"]
        p = client.put(
            f"/v1/contacts/{cid}/persona",
            json={"settings": {"formality": 5, "address_term": "팀장님"}, "expected_version": 0},
            headers=headers,
        )
        assert p.status_code == 200, p.text
        assert client.get("/v1/contacts", headers=headers).json()[0]["persona_version"] == 1

        h = {**headers, "Idempotency-Key": "pg-1"}
        body = {"contact_id": cid, "draft_text": "오늘 정리해서 내일 오전까지 보내드릴게요."}
        first = client.post("/v1/rewrites", json=body, headers=h)
        assert first.status_code == 201, first.text
        second = client.post("/v1/rewrites", json=body, headers=h)
        assert second.json()["id"] == first.json()["id"]
        assert client.get(f"/v1/rewrites/{first.json()['id']}", headers=headers).status_code == 200
        fb = client.post(
            f"/v1/rewrites/{first.json()['id']}/feedback", json={"accepted": True}, headers=headers
        )
        assert fb.status_code == 204
        assert client.get(f"/v1/contacts/{cid}", headers={"X-User-Id": _user()}).status_code == 404


def test_import_and_vector_search_on_postgres(pg_url: str) -> None:
    """가져오기 → episodes(pgvector) 저장 → C 조건 첨삭이 kNN 결과를 받는지. 임베더는 fake."""
    settings = Settings(_env_file=None, llm_provider="fake", database_url=pg_url, embedding_provider="fake")  # type: ignore[call-arg]
    headers = {"X-User-Id": _user()}
    with TestClient(create_app(settings)) as client:
        cid = client.post(
            "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=headers
        ).json()["id"]
        client.put(
            f"/v1/contacts/{cid}/persona",
            json={"settings": {"formality": 5}, "expected_version": 0},
            headers=headers,
        )
        conv_id = client.post(
            "/v1/conversations", json={"contact_id": cid, "source_key": "c1"}, headers=headers
        ).json()["id"]
        job = client.post(
            f"/v1/conversations/{conv_id}/imports",
            json={
                "speaker_map": {"김민수": "me", "박지현": "contact"},
                "messages": [
                    {
                        "id": "m01",
                        "speaker": "박지현",
                        "sent_at": "2026-10-05T09:25:00+09:00",
                        "text": "언제까지 가능해요?",
                    },
                    {
                        "id": "m02",
                        "speaker": "김민수",
                        "sent_at": "2026-10-05T09:31:00+09:00",
                        "text": "팀장님, 금요일은 어렵고 다음 주 수요일까지는 가능합니다.",
                    },
                    {
                        "id": "m03",
                        "speaker": "me",
                        "sent_at": "2026-10-06T09:00:00+09:00",
                        "text": "어제는 죄송했습니다.",
                    },
                ],
            },
            headers=headers,
        )
        assert job.status_code == 202, job.text
        assert (job.json()["imported"], job.json()["episodes"]) == (3, 1)  # m02·m03 연속 본인 발화 → 한 묶음
        # 같은 내용 재업로드는 같은 작업
        assert (
            client.post(
                f"/v1/conversations/{conv_id}/imports",
                json={
                    "speaker_map": {"김민수": "me", "박지현": "contact"},
                    "messages": [
                        {
                            "id": "m01",
                            "speaker": "박지현",
                            "sent_at": "2026-10-05T09:25:00+09:00",
                            "text": "언제까지 가능해요?",
                        },
                        {
                            "id": "m02",
                            "speaker": "김민수",
                            "sent_at": "2026-10-05T09:31:00+09:00",
                            "text": "팀장님, 금요일은 어렵고 다음 주 수요일까지는 가능합니다.",
                        },
                        {
                            "id": "m03",
                            "speaker": "me",
                            "sent_at": "2026-10-06T09:00:00+09:00",
                            "text": "어제는 죄송했습니다.",
                        },
                    ],
                },
                headers=headers,
            ).json()["id"]
            == job.json()["id"]
        )
        r = client.post(
            "/v1/rewrites",
            json={
                "contact_id": cid,
                "draft_text": "금요일은 어렵고 수요일까지는 가능합니다.",
                "condition": "C",
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text
        assert r.json()["result"]["retrieval_version"].startswith("knn-style-v1/fake")

    # 저장소 직접: 검색 범위·순서
    async def _search() -> list[str]:
        engine = create_engine(pg_url)
        repo = PostgresConversationRepo(create_session_factory(engine))
        try:
            emb = FakeEmbedder(dim=384)
            hits = await repo.search_episodes(
                headers["X-User-Id"], uuid.UUID(cid), await emb.embed_query("금요일은 어렵고 수요일까지"), 5
            )
            return [h.episode.style_text for h in hits]
        finally:
            await engine.dispose()

    texts = asyncio.run(_search())
    assert texts == ["팀장님, 금요일은 어렵고 다음 주 수요일까지는 가능합니다.\n어제는 죄송했습니다."]


def test_auth_register_login_on_postgres(pg_url: str) -> None:
    """users·sessions 테이블 왕복: 가입 → 쿠키 → me, 로그아웃 뒤 401, 재로그인."""
    settings = Settings(_env_file=None, llm_provider="fake", database_url=pg_url, embedding_provider="fake")  # type: ignore[call-arg]
    name = f"pg_{_user().replace('-', '_')}"
    with TestClient(create_app(settings)) as client:
        r = client.post("/v1/auth/register", json={"username": name, "password": "long-enough-pw"})
        assert r.status_code == 201, r.text
        assert client.get("/v1/auth/me").json()["username"] == name
        csrf = r.json()["csrf_token"]
        ok = client.post(
            "/v1/contacts",
            json={"alias": "박지현", "relationship_type": "superior"},
            headers={"X-CSRF-Token": csrf},
        )
        assert ok.status_code == 201, ok.text
        assert client.post("/v1/auth/logout").status_code == 204
        assert client.get("/v1/auth/me").status_code == 401
        again = client.post("/v1/auth/login", json={"username": name, "password": "long-enough-pw"})
        assert again.status_code == 200 and again.json()["user_id"] == r.json()["user_id"]
        assert client.get("/v1/contacts").json()[0]["alias"] == "박지현"


def test_deep_healthz_on_postgres(pg_url: str) -> None:
    settings = Settings(_env_file=None, llm_provider="fake", database_url=pg_url, embedding_provider="fake")  # type: ignore[call-arg]
    with TestClient(create_app(settings)) as client:
        r = client.get("/healthz?deep=true")
        assert r.status_code == 200, r.text
        assert r.json()["checks"] == {"postgres": "ok", "neo4j": "skipped"}


def test_delete_contact_and_account_cascade_on_postgres(pg_url: str) -> None:
    """FK CASCADE 확인: 상대 삭제 → 대화방·메시지·묶음·페르소나·첨삭 이력, 계정 삭제 → 사용자·세션·사용량."""
    settings = Settings(_env_file=None, llm_provider="fake", database_url=pg_url, embedding_provider="fake")  # type: ignore[call-arg]
    name = f"pg_{_user().replace('-', '_')}"
    with TestClient(create_app(settings)) as client:
        reg = client.post("/v1/auth/register", json={"username": name, "password": "long-enough-pw"})
        h = {"X-CSRF-Token": reg.json()["csrf_token"]}
        cid = client.post(
            "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=h
        ).json()["id"]
        p = client.put(
            f"/v1/contacts/{cid}/persona",
            json={"settings": {"formality": 5, "address_term": "팀장님"}, "expected_version": 0},
            headers=h,
        )
        assert p.status_code == 200, p.text
        conv = client.post(
            "/v1/conversations", json={"contact_id": cid, "source_key": "c1"}, headers=h
        ).json()
        imp = client.post(
            f"/v1/conversations/{conv['id']}/imports",
            json={
                "messages": [
                    {
                        "id": "m01",
                        "speaker": "me",
                        "sent_at": "2026-10-05T09:00:00+09:00",
                        "text": "안녕하세요",
                    },
                    {"id": "m02", "speaker": "contact", "sent_at": "2026-10-05T09:01:00+09:00", "text": "네"},
                ]
            },
            headers=h,
        )
        assert imp.status_code == 202, imp.text
        rec = client.post(
            "/v1/rewrites",
            json={"contact_id": cid, "draft_text": "오늘 정리해서 내일 오전까지 보내드릴게요."},
            headers=h,
        )
        assert rec.status_code == 201, rec.text
        client.post(f"/v1/rewrites/{rec.json()['id']}/feedback", json={"accepted": True}, headers=h)
        assert client.get("/v1/usage").json()["rewrites"] == 1

        d = client.delete(f"/v1/contacts/{cid}", headers=h)
        assert d.status_code == 200, d.text
        assert (d.json()["conversations"], d.json()["messages"], d.json()["rewrites"]) == (1, 2, 1)
        assert client.get(f"/v1/contacts/{cid}").status_code == 404
        assert client.get(f"/v1/conversations/{conv['id']}").status_code == 404
        assert client.get(f"/v1/rewrites/{rec.json()['id']}").status_code == 404
        assert client.get("/v1/rewrites").json() == []

        # 계정 삭제: 상대 하나 더 만들어 두고 통째로
        client.post("/v1/contacts", json={"alias": "준호", "relationship_type": "family"}, headers=h)
        gone = client.request("DELETE", "/v1/auth/me", json={"password": "long-enough-pw"}, headers=h)
        assert gone.status_code == 200, gone.text
        assert gone.json()["contacts"] == 1
        assert client.get("/v1/auth/me").status_code == 401
        assert (
            client.post("/v1/auth/login", json={"username": name, "password": "long-enough-pw"}).status_code
            == 401
        )

    async def _count_rows() -> dict[str, int]:
        conn = await asyncpg.connect(_dsn(pg_url))
        try:
            out = {}
            for table in (
                "users",
                "sessions",
                "usage_counters",
                "contacts",
                "conversations",
                "messages",
                "episodes",
            ):
                col = "username" if table == "users" else "user_id"
                val = name if table == "users" else str(reg.json()["user_id"])
                out[table] = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {col} = $1", val)  # noqa: S608
            return out
        finally:
            await conn.close()

    assert asyncio.run(_count_rows()) == dict.fromkeys(
        ("users", "sessions", "usage_counters", "contacts", "conversations", "messages", "episodes"), 0
    )


def test_usage_report_reads_counters_and_results_on_postgres(pg_url: str) -> None:
    """usage_counters와 rewrite_requests/results를 함께 읽어 조건별로 묶는다."""
    from datetime import date, timedelta

    from app.domain.usage import usage_day
    from app.tools.usage_report import load_report

    settings = Settings(_env_file=None, llm_provider="fake", database_url=pg_url, embedding_provider="fake")  # type: ignore[call-arg]
    user = _user()
    with TestClient(create_app(settings)) as client:
        h = {"X-User-Id": user}
        c = client.post(
            "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=h
        ).json()
        client.put(
            f"/v1/contacts/{c['id']}/persona",
            json={"settings": {"formality": 5, "address_term": "팀장님"}, "expected_version": 0},
            headers=h,
        )
        for cond in ("B", "B", "A"):
            r = client.post(
                "/v1/rewrites",
                json={
                    "contact_id": c["id"],
                    "draft_text": "오늘 정리해서 내일 오전까지 보내드릴게요.",
                    "condition": cond,
                },
                headers=h,
            )
            assert r.status_code == 201, r.text

    today = usage_day(offset_hours=settings.usage_day_offset_hours)

    async def _load():  # noqa: ANN202
        engine = create_engine(pg_url)
        try:
            full = await load_report(engine, today - timedelta(days=1), today, 9, user_id=user)
            none = await load_report(engine, date(2020, 1, 1), date(2020, 1, 2), 9, user_id=user)
        finally:
            await engine.dispose()
        return full, none

    report, empty = asyncio.run(_load())
    assert [(c.user_id, c.day, c.rewrites) for c in report.counters] == [(user, today, 3)]
    assert [(c.condition, c.requests, c.failed) for c in report.conditions] == [("A", 1, 0), ("B", 2, 0)]
    assert (
        report.conditions[1].input_tokens
        == report.counters[0].input_tokens - report.conditions[0].input_tokens
    )
    assert (empty.counters, empty.conditions) == ([], [])
