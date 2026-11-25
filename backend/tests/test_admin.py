"""관리자 API (`/v1/admin/*`) — 접근 검사, 집계, 개인정보 원칙.

- 순수 함수(id 줄이기, 비용 환산, 기간 경계, 오류 종류)는 DB 없이 본다.
- 접근 검사의 401·403·503은 인메모리 앱으로 본다.
- 집계는 PostgreSQL 통합(`pg_url`, 없으면 건너뜀)이다. 테스트 DB는 다른 테스트 파일과 함께 쓰므로
  (1) HTTP로 본 "오늘" 수치는 **호출 전후 차이**로, (2) 백분위처럼 차이로 볼 수 없는 값은 아무 자료도 없는
  먼 과거(2001년)에 행을 직접 넣고 서비스에 그 시점을 `now`로 주어 정확한 값으로 검증한다.
- 가짜 모델은 초안을 그대로 돌려주므로 원문 유지(fallback) 결과와 채널·선택형 사유는 DB에 직접 넣는다.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import InvalidInputError
from app.domain.rewrite import AttemptRecord, RewriteResult, ValidationReport, Violation
from app.main import create_app
from app.services.admin_service import (
    AdminIdentity,
    AdminService,
    GraphDisabledError,
    error_kind,
    estimate_graph_cost,
    estimate_token_cost,
    is_admin_username,
    period_bounds,
    safe_code,
    short_user_id,
)

PATHS = (
    "/v1/admin/overview",
    "/v1/admin/users",
    "/v1/admin/jobs",
    "/v1/admin/feedback",
    "/v1/admin/access-log",
    "/v1/admin/graph/held",
)
PASSWORD = "long-enough-pw"

# 목록 응답의 필드 (계약 §4.6 + 작업의 error_code). 이 밖의 열이 섞여 나오면 실패한다.
USER_FIELDS = "user_id username created_at consent_version contacts messages rewrites last_rewrite_at".split()
JOB_FIELDS = (
    "id user_id status kind imported episodes error_code graph_status graph_episodes graph_error created_at"
).split()
FEEDBACK_FIELDS = "user_id relationship_type channel condition reason_code created_at".split()
ACCESS_FIELDS = "admin_username action detail created_at".split()
HELD_FIELDS = "user_id conversation_id messages attempts oldest_held_at latest_held_at".split()

# 관리자 응답 어디에도 나오면 안 되는 표식. 원문·상황·결과 문장·시도 기록·위반 상세·자유 서술 사유·편집본·
# 대화 내용·상대 이름·대화방 이름·오류 본문에 하나씩 심는다.
SECRETS = {
    "draft": "ZQDRAFT4471",
    "situation": "ZQSITUATION5582",
    "result": "ZQRESULT9926",
    "attempt": "ZQATTEMPT2148",
    "violation": "ZQVIOLATION1037",
    "reason": "ZQREASON6693",
    "edited": "ZQEDITED7704",
    "message": "ZQMESSAGE8815",
    "alias": "ZQALIAS3360",
    "title": "ZQTITLE4471",
    "graph_error": "ZQGRAPHERR3259",
}


def _settings(**overrides: Any) -> Settings:
    """가짜 모델·가짜 임베더, 관리자는 boss. 필요한 설정만 덮어쓴다."""
    values = {"llm_provider": "fake", "embedding_provider": "fake", "admin_usernames": "boss", **overrides}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# ---------- 순수 함수 ----------


def test_short_user_id_hides_the_full_id() -> None:
    assert short_user_id("8f2c1d3e-aaaa-4bbb-8ccc-1234567890a1") == "u_8f2…a1"
    full = str(uuid4())
    assert len(short_user_id(full)) == 8 and full not in short_user_id(full)
    # 개발용 헤더 스텁의 짧은 id는 앞뒤를 다 보이면 전부 드러난다 → 앞부분 일부만
    assert short_user_id("user-a") == "u_us…"
    assert short_user_id("ab") == "u_a…"
    assert short_user_id("") == "u_…"
    assert short_user_id("  demo-user-1  ") == "u_dem…-1"


def test_cost_is_none_until_unit_prices_are_set() -> None:
    assert estimate_token_cost(1_000_000, 500_000, 0.0, 0.0) is None
    assert estimate_token_cost(1_000_000, 500_000, 5.0, 0.0) is None  # 한쪽만으로는 틀린 숫자가 된다
    assert estimate_token_cost(1_000_000, 500_000, 0.0, 25.0) is None
    assert estimate_token_cost(1_000_000, 500_000, 5.0, 25.0) == 17.5
    assert estimate_token_cost(0, 0, 5.0, 25.0) == 0.0
    assert estimate_token_cost(1234, 567, 5.0, 25.0) == 0.020345
    assert estimate_graph_cost(120, 0.0) is None
    assert estimate_graph_cost(120, 0.004) == 0.48
    assert estimate_graph_cost(0, 0.004) == 0.0


def test_period_bounds_follow_the_usage_day_boundary() -> None:
    now = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)  # 한국 시간 11-11 00:30
    today = period_bounds(1, now, 9)
    assert (today.first_day, today.last_day) == (date(2026, 11, 11), date(2026, 11, 11))
    assert today.start == datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    assert today.end == datetime(2026, 11, 11, 15, 0, tzinfo=UTC)

    week = period_bounds(7, now, 9)
    assert week.first_day == date(2026, 11, 5) and week.each_day()[-1] == date(2026, 11, 11)
    assert len(week.each_day()) == 7
    assert week.start.isoformat() == "2026-11-05T00:00:00+09:00"
    assert week.end.isoformat() == "2026-11-12T00:00:00+09:00"
    assert len(period_bounds(30, now, 9).each_day()) == 30

    # 자정 1초 전은 아직 전날, 하루 경계를 UTC로 두면 날짜도 UTC를 따른다
    assert period_bounds(1, datetime(2026, 11, 10, 14, 59, 59, tzinfo=UTC), 9).last_day == date(2026, 11, 10)
    assert period_bounds(1, now, 0).last_day == date(2026, 11, 10)
    assert period_bounds(1, datetime(2026, 11, 10, 15, 30), 9).last_day == date(
        2026, 11, 11
    )  # 시간대 없음=UTC
    for bad in (0, 2, 5, 31, -7):
        with pytest.raises(InvalidInputError):
            period_bounds(bad, now, 9)


def test_error_kind_keeps_the_kind_and_drops_the_text() -> None:
    assert error_kind(None) is None
    assert error_kind("   ") is None
    leaked = "내일 3시에 강남역에서 보자 SECRET-TEXT"
    message = f"RateLimitError: Error code: 429 - {{'type': 'rate_limit_error', 'message': '{leaked}'}}"
    kind = error_kind(message)
    assert kind == "RateLimitError · rate_limit_error · HTTP 429"
    assert "강남역" not in kind and "SECRET-TEXT" not in kind
    assert (
        error_kind("{code: Neo.ClientError.Statement.SyntaxError} {message: Invalid input}")
        == "Neo.ClientError.Statement.SyntaxError"
    )
    assert error_kind("Couldn't connect to localhost:7687 (resolved to ())") == "connection"
    assert error_kind("request timed out after 30s") == "timeout"
    assert error_kind("boom") == "other"
    assert error_kind(leaked) == "other"  # 형태가 정해진 토큰이 없으면 문장을 내보내지 않는다
    assert len(error_kind("A" + "b" * 60 + "Error " + "X" + "y" * 55 + "_error status 503") or "") <= 80


def test_safe_code_lets_only_short_codes_through() -> None:
    assert safe_code("too_stiff") == "too_stiff"
    assert safe_code("A") == "A"
    assert safe_code("fallback_original") == "fallback_original"
    assert safe_code("내일 오전까지 보내드릴게요") == "other"
    assert safe_code("two words") == "other"
    assert safe_code("x" * 41) == "other"
    assert safe_code(None) == "other"
    assert safe_code("", default="unknown") == "unknown"


def test_admin_username_must_match_exactly() -> None:
    settings = _settings(admin_usernames="boss, Ops_Lead ,")
    assert is_admin_username("boss", settings)
    assert is_admin_username("Ops_Lead", settings)
    # 가입은 대소문자를 구분하므로 Boss는 boss와 다른 계정이다 → 관리자가 아니다
    assert not is_admin_username("Boss", settings)
    assert not is_admin_username("ops_lead", settings)
    assert not is_admin_username("", settings)
    assert not is_admin_username("boss", _settings(admin_usernames=""))


# ---------- 접근 검사 (인메모리) ----------


def _code(response) -> str | None:  # noqa: ANN001
    body = response.json()
    return body.get("code") or (body.get("detail") or {}).get("code")


def test_anonymous_and_header_stub_get_401() -> None:
    client = TestClient(create_app(_settings()))
    for path in PATHS:
        r = client.get(path)
        assert r.status_code == 401 and _code(r) == "unauthorized", (path, r.text)
        # 헤더 스텁은 세션이 아니다. 관리자 이름을 그대로 넣어도 열리지 않는다.
        stub = client.get(path, headers={"X-User-Id": "boss"})
        assert stub.status_code == 401 and _code(stub) == "unauthorized", (path, stub.text)


def test_non_admin_gets_403_and_memory_storage_gets_503() -> None:
    client = TestClient(create_app(_settings()))
    first = client.post("/v1/auth/register", json={"username": "minsu_kim", "password": PASSWORD})
    assert first.status_code == 201, first.text
    for path in PATHS:
        r = client.get(path)
        assert r.status_code == 403 and _code(r) == "forbidden", (path, r.text)

    # 대소문자만 다른 이름으로 가입해도 관리자가 되지 않는다
    client.cookies.clear()
    assert (
        client.post("/v1/auth/register", json={"username": "Boss", "password": PASSWORD}).status_code == 201
    )
    assert client.get("/v1/admin/overview").status_code == 403

    # 대소문자만 다른 이름이 이미 있으면 가입 단계에서 막힌다 (auth_service의 중복 규칙)
    client.cookies.clear()
    assert (
        client.post("/v1/auth/register", json={"username": "boss", "password": PASSWORD}).status_code == 409
    )

    # 진짜 관리자라도 인메모리 저장소에서는 집계할 곳이 없다
    client = TestClient(create_app(_settings()))
    assert (
        client.post("/v1/auth/register", json={"username": "boss", "password": PASSWORD}).status_code == 201
    )
    for path in PATHS:
        r = client.get(path)
        assert r.status_code == 503 and _code(r) == "admin_unavailable", (path, r.text)
        assert "PostgreSQL" in r.json()["message"]


def test_nobody_is_admin_when_the_setting_is_empty() -> None:
    client = TestClient(create_app(_settings(admin_usernames="")))
    client.post("/v1/auth/register", json={"username": "boss", "password": PASSWORD})
    assert client.get("/v1/admin/overview").status_code == 403


class _LogDownRepo:
    """접근 기록을 남기지 못하는 저장소. 그래도 자료를 읽으려 하면 어떤 호출이었는지 남는다."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    async def log_access(self, *args: Any) -> None:
        raise RuntimeError("log down")

    def __getattr__(self, name: str):  # noqa: ANN204
        async def _read(*args: Any, **kwargs: Any) -> None:
            self.reads.append(name)

        return _read


def test_nothing_is_read_when_the_access_record_cannot_be_written() -> None:
    repo = _LogDownRepo()
    service = AdminService(_settings(), repo)  # type: ignore[arg-type]
    admin = AdminIdentity(user_id=str(uuid4()), username="boss")
    calls = (
        service.overview(admin, 7),
        service.users(admin, 50, 0),
        service.jobs(admin, 50),
        service.rejected_feedback(admin, 50),
        service.access_log(admin, 50),
        service.held(admin, 50),
    )
    for call in calls:
        with pytest.raises(RuntimeError, match="log down"):
            asyncio.run(call)
    assert repo.reads == []


def test_admin_screen_follows_the_screen_rules() -> None:
    """화면 공통 규칙: 같은 출처의 모듈 스크립트만, 인라인 스크립트·핸들러·style 없음, 색은 토큰만."""
    client = TestClient(create_app(_settings()))
    page = client.get("/admin")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    html = page.text
    assert html.index("/static/css/tokens.css") < html.index("/static/css/admin.css")
    assert re.findall(r"<script\b[^>]*>", html) == ['<script type="module" src="/static/js/admin.js">']
    assert "<style" not in html
    assert not re.search(r"\son[a-z]+\s*=", html)  # onclick= 같은 인라인 핸들러
    assert not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"https?://", html)  # 외부 자원(CDN·폰트·이미지) 없음

    css = client.get("/static/css/admin.css")
    assert css.status_code == 200
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", css.text)  # 색 값은 tokens.css에만

    for name in ("admin.js", "admin/api.js", "admin/format.js", "admin/view.js", "admin/dialog.js"):
        script = client.get(f"/static/js/{name}")
        assert script.status_code == 200, name
        # 서버가 준 문자열을 HTML로 해석하게 만드는 길이 없다
        assert not re.search(r"\.innerHTML\b|insertAdjacentHTML|document\.write|\beval\(", script.text), name


# ---------- PostgreSQL 통합 ----------


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


def _dsn(pg_url: str) -> str:
    from tests.pg_support import dsn

    return dsn(pg_url)


async def _connect(pg_url: str):  # noqa: ANN202
    import asyncpg

    return await asyncpg.connect(_dsn(pg_url))


def _payload(status: str, checks: list[str], latency_ms: int, token_usage: dict[str, int]) -> dict[str, Any]:
    """저장되는 결과(payload) 모양 그대로. 문장이 들어가는 자리마다 표식을 심는다."""
    violations = [
        Violation(check=c, detail=f"{SECRETS['violation']} 보존 항목 누락: 내일 오전") for c in checks
    ]  # type: ignore[arg-type]
    return RewriteResult(
        status=status,  # type: ignore[arg-type]
        result_text=f"{SECRETS['result']} 내일 오전까지 보내드리겠습니다.",
        change_reasons=[f"{SECRETS['result']} 이유"],
        warnings=[f"{SECRETS['violation']} 주의"],
        intent="report",
        preserved_items=[],
        validation=ValidationReport(passed=status == "recommended", violations=violations),
        attempts=2 if violations else 1,
        attempt_log=[
            AttemptRecord(
                attempt=1,
                passed=not violations,
                violations=violations,
                rewritten_text=f"{SECRETS['attempt']} 모레 보내드릴게요.",
            )
        ],
        model_version="fake-direct",
        prompt_version="rewrite-v3",
        retrieval_version="none",
        token_usage=token_usage,
        latency_ms=latency_ms,
    ).model_dump(mode="json")


async def _add_contact(conn, user_id: str, relationship_type: str, at: datetime) -> UUID:  # noqa: ANN001
    contact_id = uuid4()
    await conn.execute(
        "INSERT INTO contacts (id, user_id, alias, relationship_type, created_at)"
        " VALUES ($1, $2, $3, $4, $5)",
        contact_id,
        user_id,
        f"{SECRETS['alias']} 박지현",
        relationship_type,
        at,
    )
    return contact_id


async def _add_rewrite(
    conn,  # noqa: ANN001
    *,
    user_id: str,
    contact_id: UUID,
    at: datetime,
    status: str = "recommended",
    condition: str = "B",
    channel: str = "web",
    latency_ms: int = 100,
    checks: list[str] | None = None,
    token_usage: dict[str, int] | None = None,
) -> UUID:
    """rewrite_requests + rewrite_results 한 쌍을 직접 넣는다 (저장소의 save와 같은 열)."""
    request_id = uuid4()
    usage = token_usage if token_usage is not None else {"input_tokens": 0, "output_tokens": 0}
    payload = _payload(status, checks or [], latency_ms, usage)
    await conn.execute(
        """
        INSERT INTO rewrite_requests
            (id, user_id, contact_id, persona_version_id, draft_version, condition, channel,
             input_hash, idempotency_key, status, created_at)
        VALUES ($1, $2, $3, NULL, 1, $4, $5, $6, NULL, $7, $8)
        """,
        request_id,
        user_id,
        contact_id,
        condition,
        channel,
        uuid4().hex * 2,
        status,
        at,
    )
    await conn.execute(
        """
        INSERT INTO rewrite_results
            (request_id, result_text, validation_status, model_version, prompt_version,
             retrieval_version, token_usage, latency_ms, payload)
        VALUES ($1, $2, $3, 'fake-direct', 'rewrite-v3', 'none', $4::jsonb, $5, $6::jsonb)
        """,
        request_id,
        payload["result_text"],
        "passed" if status == "recommended" else "failed",
        json.dumps(usage),
        latency_ms,
        json.dumps(payload, ensure_ascii=False),
    )
    return request_id


async def _add_feedback(
    conn,  # noqa: ANN001
    *,
    request_id: UUID,
    user_id: str,
    accepted: bool,
    at: datetime,
    reason_code: str | None = None,
    edited_text: str | None = None,
    reason: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO feedback
            (request_id, user_id, accepted, edited_text, reason, reason_code, consent_to_reuse, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, false, $7)
        """,
        request_id,
        user_id,
        accepted,
        edited_text,
        reason,
        reason_code,
        at,
    )


async def _add_usage(conn, user_id: str, day: date, rewrites: int, tokens_in: int, tokens_out: int) -> None:  # noqa: ANN001
    await conn.execute(
        """
        INSERT INTO usage_counters (user_id, day, rewrites, input_tokens, output_tokens, updated_at)
        VALUES ($1, $2, $3, $4, $5, now())
        """,
        user_id,
        day,
        rewrites,
        tokens_in,
        tokens_out,
    )


async def _add_conversation(conn, user_id: str, contact_id: UUID, at: datetime) -> UUID:  # noqa: ANN001
    conversation_id = uuid4()
    await conn.execute(
        """
        INSERT INTO conversations (id, user_id, contact_id, title, source, source_key, me_name, created_at)
        VALUES ($1, $2, $3, $4, 'json', $5, NULL, $6)
        """,
        conversation_id,
        user_id,
        contact_id,
        f"{SECRETS['title']} 팀장 방",
        f"c-{uuid4().hex[:10]}",
        at,
    )
    return conversation_id


async def _add_message(
    conn,  # noqa: ANN001
    user_id: str,
    conversation_id: UUID,
    at: datetime,
    ingested: datetime | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO messages
            (id, user_id, conversation_id, source_key, speaker_role, sent_at, text,
             graph_ingested_at, created_at)
        VALUES ($1, $2, $3, $4, 'me', $5, $6, $7, $5)
        """,
        uuid4(),
        user_id,
        conversation_id,
        f"m-{uuid4().hex[:10]}",
        at,
        f"{SECRETS['message']} 금요일은 어렵고 수요일까지는 가능합니다.",
        ingested,
    )


async def _add_job(
    conn,  # noqa: ANN001
    user_id: str,
    conversation_id: UUID,
    at: datetime,
    graph_status: str,
    graph_error: str | None = None,
    status: str = "completed",
    error_code: str | None = None,
    imported: int = 6,
    duplicates: int = 0,
) -> UUID:
    job_id = uuid4()
    await conn.execute(
        """
        INSERT INTO import_jobs
            (id, user_id, conversation_id, source_hash, status, imported, duplicates, episodes,
             error_code, error_message, graph_status, graph_episodes, graph_error, created_at)
        VALUES ($1, $2, $3, $4, $5, $11, $12, 2, $6, $7, $8, 1, $9, $10)
        """,
        job_id,
        user_id,
        conversation_id,
        uuid4().hex * 2,
        status,
        error_code,
        f"{SECRETS['graph_error']} 임베딩 실패" if error_code else None,
        graph_status,
        graph_error,
        at,
        imported,
        duplicates,
    )
    return job_id


@dataclass
class AdminEnv:
    """PostgreSQL로 띄운 앱 하나. TestClient는 하나만 쓴다 — 엔진의 연결이 그 이벤트 루프에 묶이기 때문이다.

    사용자를 바꿀 때는 새 클라이언트를 만들지 않고 쿠키만 갈아 끼운다.
    """

    client: TestClient
    pg_url: str
    boss_token: str
    boss_id: str

    def use(self, token: str | None) -> None:
        self.client.cookies.clear()
        if token:
            self.client.cookies.set("pi_session", token)

    def register(self, username: str) -> dict[str, str]:
        self.client.cookies.clear()
        r = self.client.post("/v1/auth/register", json={"username": username, "password": PASSWORD})
        assert r.status_code == 201, r.text
        return {
            "token": r.cookies.get("pi_session"),
            "csrf": r.json()["csrf_token"],
            "user_id": r.json()["user_id"],
            "username": username,
        }

    def admin_get(self, path: str):  # noqa: ANN201
        self.use(self.boss_token)
        return self.client.get(path)

    def overview(self, days: int = 1) -> dict[str, Any]:
        r = self.admin_get(f"/v1/admin/overview?days={days}")
        assert r.status_code == 200, r.text
        return r.json()

    def access_log_count(self) -> int:
        async def _count() -> int:
            conn = await _connect(self.pg_url)
            try:
                return int(await conn.fetchval("SELECT count(*) FROM admin_access_log"))
            finally:
                await conn.close()

        return _run(_count())


@pytest.fixture(scope="module")
def env(pg_url: str):  # noqa: ANN201
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("asyncpg")
    with TestClient(create_app(_settings(database_url=pg_url))) as client:
        r = client.post("/v1/auth/register", json={"username": "boss", "password": PASSWORD})
        assert r.status_code == 201, r.text
        yield AdminEnv(
            client=client, pg_url=pg_url, boss_token=r.cookies.get("pi_session"), boss_id=r.json()["user_id"]
        )


def _name(prefix: str = "adm") -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def _rewrite(env: AdminEnv, headers: dict[str, str], contact_id: str, condition: str) -> dict[str, Any]:
    r = env.client.post(
        "/v1/rewrites",
        json={
            "contact_id": contact_id,
            "draft_text": f"{SECRETS['draft']} 오늘 정리해서 내일 오전까지 보내드릴게요.",
            "situation": f"{SECRETS['situation']} 일정 지연 양해",
            "condition": condition,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["result"]["status"] == "recommended"  # 가짜 모델은 초안을 그대로 돌려준다
    return r.json()


def _seed_user_activity(env: AdminEnv) -> dict[str, Any]:
    """일반 사용자 한 명: 상대·페르소나·가져오기 3건·첨삭 3건(B 2, A 1)·피드백(수정해서 적용 1, 거절 1)."""
    user = env.register(_name())
    env.use(user["token"])
    h = {"X-CSRF-Token": user["csrf"]}
    contact = env.client.post(
        "/v1/contacts",
        json={"alias": f"{SECRETS['alias']} 박지현", "relationship_type": "superior"},
        headers=h,
    )
    assert contact.status_code == 201, contact.text
    cid = contact.json()["id"]
    persona = env.client.put(
        f"/v1/contacts/{cid}/persona",
        json={"settings": {"formality": 5, "address_term": "팀장님"}, "expected_version": 0},
        headers=h,
    )
    assert persona.status_code == 200, persona.text
    conv = env.client.post(
        "/v1/conversations",
        json={"contact_id": cid, "title": f"{SECRETS['title']} 팀장 방", "source_key": "c1"},
        headers=h,
    )
    assert conv.status_code == 201, conv.text
    job = env.client.post(
        f"/v1/conversations/{conv.json()['id']}/imports",
        json={
            "speaker_map": {"김민수": "me", "박지현": "contact"},
            "messages": [
                {
                    "id": "m01",
                    "speaker": "박지현",
                    "sent_at": "2026-10-05T09:25:00+09:00",
                    "text": f"{SECRETS['message']} 언제까지 가능해요?",
                },
                {
                    "id": "m02",
                    "speaker": "김민수",
                    "sent_at": "2026-10-05T09:31:00+09:00",
                    "text": f"{SECRETS['message']} 금요일은 어렵고 다음 주 수요일까지는 가능합니다.",
                },
                {
                    "id": "m03",
                    "speaker": "김민수",
                    "sent_at": "2026-10-06T09:00:00+09:00",
                    "text": f"{SECRETS['message']} 어제는 죄송했습니다.",
                },
            ],
        },
        headers=h,
    )
    assert job.status_code == 202, job.text

    rewrites = [_rewrite(env, h, cid, condition) for condition in ("B", "B", "A")]
    accepted = env.client.post(
        f"/v1/rewrites/{rewrites[0]['id']}/feedback",
        json={"accepted": True, "edited_text": f"{SECRETS['edited']} 내일 오전에 드릴게요."},
        headers=h,
    )
    rejected = env.client.post(
        f"/v1/rewrites/{rewrites[1]['id']}/feedback",
        json={"accepted": False, "reason": f"{SECRETS['reason']} 너무 딱딱해서 제 말투가 아니에요"},
        headers=h,
    )
    assert (accepted.status_code, rejected.status_code) == (204, 204)
    return {**user, "contact_id": cid, "job_id": job.json()["id"], "rewrites": rewrites}


def _tokens(rewrites: list[dict[str, Any]]) -> tuple[int, int]:
    usage = [r["result"]["token_usage"] for r in rewrites]
    return sum(u["input_tokens"] for u in usage), sum(u["output_tokens"] for u in usage)


def _count(items: list[dict[str, Any]], key: str, value: str) -> int:
    return next((i["count"] for i in items if i[key] == value), 0)


def test_overview_moves_with_new_activity_and_lists_hide_private_text(env: AdminEnv) -> None:
    log_before = env.access_log_count()
    before = env.overview(1)

    user = _seed_user_activity(env)
    # 개발용 헤더 스텁 사용자: users에 없는 id로 첨삭 1건 (집계에서 빠지면 안 된다)
    env.use(None)
    stub_id = f"stub-{uuid4().hex[:12]}"
    stub_headers = {"X-User-Id": stub_id}
    stub_contact = env.client.post(
        "/v1/contacts", json={"alias": "준호", "relationship_type": "family"}, headers=stub_headers
    ).json()["id"]
    stub_rewrite = _rewrite(env, stub_headers, stub_contact, "A")

    async def _direct() -> None:
        conn = await _connect(env.pg_url)
        try:
            now = datetime.now(UTC)
            cid = UUID(user["contact_id"])
            # 원문 유지 2건: 같은 종류가 두 번 있어도 요청 1건, 두 종류면 양쪽에 1건씩
            await _add_rewrite(
                conn,
                user_id=user["user_id"],
                contact_id=cid,
                at=now,
                status="fallback_original",
                condition="C",
                channel="mobile",
                latency_ms=7000,
                checks=["preserve_missing", "preserve_missing"],
            )
            second = await _add_rewrite(
                conn,
                user_id=user["user_id"],
                contact_id=cid,
                at=now,
                status="fallback_original",
                condition="D",
                channel="desktop",
                latency_ms=9000,
                checks=["fact_added", "refusal"],
            )
            await _add_feedback(
                conn,
                request_id=second,
                user_id=user["user_id"],
                accepted=False,
                at=now,
                reason_code="fact_changed",
                reason=f"{SECRETS['reason']} 날짜가 바뀌었어요",
            )
            # 선택형 사유는 data 담당의 API가 채운다 — 여기서는 방금 만든 거절 피드백에 직접 넣는다
            await conn.execute(
                "UPDATE feedback SET reason_code = 'too_stiff' WHERE request_id = $1",
                UUID(user["rewrites"][1]["id"]),
            )
            # 실패한 수집 작업: 오류 문자열에 대화 내용이 섞여 있다
            conv_id = await conn.fetchval(
                "SELECT id FROM conversations WHERE user_id = $1 LIMIT 1", user["user_id"]
            )
            await _add_job(
                conn,
                user["user_id"],
                conv_id,
                now,
                graph_status="failed",
                graph_error=f"RuntimeError: 수집 실패 — 본문: {SECRETS['graph_error']} 금요일은 어렵다",
                status="failed",
                error_code="reindex_failed",
            )
            # 화자 역할 고치기가 남기는 작업(넣은 메시지 없음, 그래프 다시 넣기 대기)과
            # 같은 파일을 다시 올린 가져오기(전부 중복)
            made["role_fix"] = await _add_job(
                conn, user["user_id"], conv_id, now, graph_status="pending", imported=0, duplicates=0
            )
            made["reupload"] = await _add_job(
                conn, user["user_id"], conv_id, now, graph_status="skipped", imported=0, duplicates=3
            )
        finally:
            await conn.close()

    made: dict[str, UUID] = {}
    _run(_direct())

    after = env.overview(1)
    api_rewrites = [*user["rewrites"], stub_rewrite]
    tok_in, tok_out = _tokens(api_rewrites)

    def delta(*keys: str) -> int:
        a, b = after, before
        for k in keys:
            a, b = a[k], b[k]
        return a - b

    assert after["range"]["days"] == 1 and after["range"]["from"].endswith("+09:00")
    assert (delta("users", "total"), delta("users", "new")) == (1, 1)
    assert (delta("users", "active"), delta("users", "active_unregistered")) == (2, 1)

    assert delta("rewrites", "total") == 6
    assert (delta("rewrites", "recommended"), delta("rewrites", "fallback")) == (4, 2)
    assert {c: delta("rewrites", "by_condition", c) for c in "ABCD"} == {"A": 2, "B": 2, "C": 1, "D": 1}
    assert {c: delta("rewrites", "by_channel", c) for c in ("web", "mobile", "desktop", "api")} == {
        "web": 4,
        "mobile": 1,
        "desktop": 1,
        "api": 0,
    }
    assert len(after["rewrites"]["by_day"]) == 1
    today_before, today_after = before["rewrites"]["by_day"][0], after["rewrites"]["by_day"][0]
    assert today_after["day"] == today_before["day"] == after["range"]["from"][:10]
    assert today_after["total"] - today_before["total"] == 6
    assert today_after["fallback"] - today_before["fallback"] == 2
    assert 0 < after["rewrites"]["success_rate"] < 1
    assert after["rewrites"]["success_rate"] == round(
        after["rewrites"]["recommended"] / after["rewrites"]["total"], 4
    )

    reasons_before, reasons_after = before["fallback_reasons"], after["fallback_reasons"]
    for check, expected in (("preserve_missing", 1), ("fact_added", 1), ("refusal", 1)):
        assert _count(reasons_after, "check", check) - _count(reasons_before, "check", check) == expected
    assert isinstance(after["latency_ms"]["p50"], int) and isinstance(after["latency_ms"]["p95"], int)
    assert after["latency_ms"]["p50"] <= after["latency_ms"]["p95"]

    assert delta("feedback", "total") == 3
    assert (delta("feedback", "accepted"), delta("feedback", "rejected"), delta("feedback", "edited")) == (
        1,
        2,
        1,
    )
    codes_before, codes_after = before["feedback"]["by_reason_code"], after["feedback"]["by_reason_code"]
    for code in ("too_stiff", "fact_changed"):
        assert _count(codes_after, "code", code) - _count(codes_before, "code", code) == 1

    # 토큰은 사용량 카운터 기준: API로 만든 첨삭이 보고한 양만큼 늘고, 단가가 없으면 비용은 null
    assert (delta("tokens", "input"), delta("tokens", "output")) == (tok_in, tok_out)
    assert tok_in > 0 and tok_out > 0
    assert after["tokens"]["estimated_cost_usd"] is None
    assert after["graph"]["estimated_cost_usd"] is None
    assert after["graph"]["enabled"] is False
    assert delta("graph", "pending_messages") == 3  # 가져온 메시지 3건은 그래프에 들어가지 않았다
    assert delta("graph", "jobs", "failed") == 1
    # 역할 고치기 작업도 그래프를 다시 넣으므로 그래프 작업 수에는 그대로 센다
    assert delta("graph", "jobs", "pending") == 1
    assert delta("limits", "today_rewrites") == 4
    assert delta("limits", "today_tokens") == tok_in + tok_out
    assert after["limits"]["daily_rewrite_limit"] == 200
    assert after["health"] == {"postgres": "ok", "neo4j": "skipped"}

    # 7일·30일은 오늘을 포함한다
    week, month = env.overview(7), env.overview(30)
    assert len(week["rewrites"]["by_day"]) == 7 and len(month["rewrites"]["by_day"]) == 30
    assert week["rewrites"]["by_day"][-1] == today_after
    assert month["rewrites"]["total"] >= week["rewrites"]["total"] >= after["rewrites"]["total"]

    # ---- 목록 ----
    users = env.admin_get("/v1/admin/users?limit=200")
    assert users.status_code == 200, users.text
    assert users.json()["total"] >= 2
    mine = next(u for u in users.json()["items"] if u["username"] == user["username"])
    assert sorted(mine) == sorted(USER_FIELDS)
    assert mine["user_id"] == short_user_id(user["user_id"])
    assert re.fullmatch(r"u_.{3}….{2}", mine["user_id"])
    assert (mine["contacts"], mine["messages"], mine["rewrites"]) == (1, 3, 5)
    assert mine["last_rewrite_at"] is not None and mine["consent_version"] is None
    boss = next(u for u in users.json()["items"] if u["username"] == "boss")
    assert (boss["contacts"], boss["messages"], boss["rewrites"], boss["last_rewrite_at"]) == (0, 0, 0, None)
    # 헤더 스텁 사용자는 가입한 계정이 아니므로 목록에 없다
    assert all(u["user_id"] != short_user_id(stub_id) for u in users.json()["items"])
    page = env.admin_get("/v1/admin/users?limit=1&offset=0").json()
    assert len(page["items"]) == 1 and page["total"] == users.json()["total"]
    assert env.admin_get(f"/v1/admin/users?limit=5&offset={page['total']}").json()["items"] == []

    jobs = env.admin_get("/v1/admin/jobs?limit=200")
    assert jobs.status_code == 200, jobs.text
    imported = next(j for j in jobs.json() if j["id"] == user["job_id"])
    assert sorted(imported) == sorted(JOB_FIELDS)
    assert imported["user_id"] == short_user_id(user["user_id"])
    assert (imported["status"], imported["imported"], imported["graph_status"]) == ("completed", 3, "skipped")
    assert imported["episodes"] >= 1 and imported["graph_error"] is None and imported["error_code"] is None
    failed = next(
        j for j in jobs.json() if j["graph_status"] == "failed" and j["user_id"] == imported["user_id"]
    )
    assert failed["graph_error"] == "RuntimeError"  # 오류 종류만
    assert (failed["status"], failed["error_code"]) == ("failed", "reindex_failed")
    # 작업 종류: 역할 고치기만 speaker_roles다. 메시지 0건이어도 중복이 있으면(다시 올림) 가져오기다
    by_id = {j["id"]: j for j in jobs.json()}
    assert (imported["kind"], failed["kind"]) == ("import", "import")
    role_fix, reupload = by_id[str(made["role_fix"])], by_id[str(made["reupload"])]
    assert (role_fix["kind"], role_fix["imported"], role_fix["graph_status"]) == (
        "speaker_roles",
        0,
        "pending",
    )
    assert (reupload["kind"], reupload["imported"]) == ("import", 0)
    created = [datetime.fromisoformat(j["created_at"]) for j in jobs.json()]
    assert created == sorted(created, reverse=True)  # 최근 것이 먼저

    feedback = env.admin_get("/v1/admin/feedback?limit=200")
    assert feedback.status_code == 200, feedback.text
    rejected = [f for f in feedback.json() if f["user_id"] == short_user_id(user["user_id"])]
    assert len(rejected) == 2  # 적용한 피드백은 나오지 않는다
    for item in rejected:
        assert sorted(item) == sorted(FEEDBACK_FIELDS)
        assert item["relationship_type"] == "superior"
    assert {(f["condition"], f["channel"], f["reason_code"]) for f in rejected} == {
        ("B", "web", "too_stiff"),
        ("D", "desktop", "fact_changed"),
    }

    access = env.admin_get("/v1/admin/access-log?limit=200")
    assert access.status_code == 200, access.text
    assert all(sorted(a) == sorted(ACCESS_FIELDS) for a in access.json())
    assert all(a["admin_username"] == "boss" for a in access.json())

    # ---- 접근 기록: 관리자 호출마다 한 줄, 가장 최근 것이 먼저 ----
    # 개요 4(전·후·7일·30일) + users 3 + jobs 1 + feedback 1 + access-log 1 = 10
    assert env.access_log_count() - log_before == 10
    latest = access.json()[:10]
    assert [(a["action"], a["detail"]) for a in latest] == [
        ("access_log", "limit=200"),
        ("feedback", "limit=200"),
        ("jobs", "limit=200"),
        ("users", f"limit=5 offset={page['total']}"),
        ("users", "limit=1 offset=0"),
        ("users", "limit=200 offset=0"),
        ("overview", "days=30"),
        ("overview", "days=7"),
        ("overview", "days=1"),
        ("overview", "days=1"),
    ]

    # ---- 개인정보: 어떤 관리자 응답에도 원문·문장·자유 서술·상대 이름·사용자 id 전체가 없다 ----
    bodies = [
        json.dumps(body, ensure_ascii=False)
        for body in (after, week, month, users.json(), jobs.json(), feedback.json(), access.json())
    ]
    for body in bodies:
        for label, secret in SECRETS.items():
            assert secret not in body, f"{label} 표식이 관리자 응답에 나왔다"
        assert user["user_id"] not in body and env.boss_id not in body and stub_id not in body
        assert user["token"] not in body and env.boss_token not in body
    for forbidden_key in (
        "result_text",
        "draft_text",
        "edited_text",
        '"reason"',
        '"text"',
        "alias",
        "payload",
    ):
        assert all(forbidden_key not in body for body in bodies), forbidden_key


def test_denied_and_invalid_calls_leave_no_access_record(env: AdminEnv) -> None:
    count = env.access_log_count()
    env.use(None)
    assert env.client.get("/v1/admin/overview").status_code == 401
    assert env.client.get("/v1/admin/users", headers={"X-User-Id": "boss"}).status_code == 401
    user = env.register(_name())
    env.use(user["token"])
    for path in PATHS:
        r = env.client.get(path)
        assert r.status_code == 403 and _code(r) == "forbidden", (path, r.text)
    # 관리자여도 잘못된 요청은 자료를 주지 않으므로 기록하지 않는다
    bad_days = env.admin_get("/v1/admin/overview?days=5")
    assert bad_days.status_code == 422 and _code(bad_days) == "invalid_input"
    assert env.admin_get("/v1/admin/users?limit=0").status_code == 422
    assert env.admin_get("/v1/admin/jobs?limit=201").status_code == 422
    assert env.admin_get("/v1/admin/users?offset=-1").status_code == 422
    assert env.admin_get(f"/v1/admin/users?offset={10**30}").status_code == 422  # 500이 아니라 422
    assert env.admin_get("/v1/admin/overview?days=seven").status_code == 422
    assert env.access_log_count() == count

    ok = env.admin_get("/v1/admin/overview")  # 기본은 7일
    assert ok.status_code == 200 and ok.json()["range"]["days"] == 7
    assert ok.headers["cache-control"] == "no-store"
    assert env.access_log_count() == count + 1


def test_cost_is_estimated_only_with_unit_prices(env: AdminEnv, pg_url: str) -> None:
    # 이 테스트만 따로 돌려도 오늘 쓴 토큰이 있도록 첨삭 1건을 만든다
    env.use(None)
    stub = {"X-User-Id": f"stub-{uuid4().hex[:12]}"}
    contact = env.client.post(
        "/v1/contacts", json={"alias": "준호", "relationship_type": "family"}, headers=stub
    )
    _rewrite(env, stub, contact.json()["id"], "A")

    assert env.overview(30)["tokens"]["estimated_cost_usd"] is None
    priced = _settings(
        database_url=pg_url,
        cost_rewrite_input_per_mtok=5.0,
        cost_rewrite_output_per_mtok=25.0,
        cost_graph_per_episode=0.004,
    )
    # 세션은 DB에 있으므로 같은 DB를 보는 다른 앱에서도 관리자 쿠키가 통한다
    with TestClient(create_app(priced)) as client:
        client.cookies.set("pi_session", env.boss_token)
        r = client.get("/v1/admin/overview?days=30")
        assert r.status_code == 200, r.text
        body = r.json()
    tokens = body["tokens"]
    assert tokens["input"] > 0
    assert tokens["estimated_cost_usd"] == round((tokens["input"] * 5.0 + tokens["output"] * 25.0) / 1e6, 6)
    assert body["graph"]["estimated_cost_usd"] == round(body["graph"]["ingested_messages"] * 0.004, 6)


class _PingStore:
    """그래프 저장소 자리에 끼우는 가짜. 실제 Neo4j에는 접속하지 않는다."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def ping(self) -> None:
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        return None


def test_health_reports_graph_store_state(env: AdminEnv) -> None:
    container = env.client.app.state.container
    try:
        container.graph_store = _PingStore()
        ok = env.overview(1)
        assert ok["graph"]["enabled"] is True
        assert ok["health"] == {"postgres": "ok", "neo4j": "ok"}

        container.graph_store = _PingStore(
            ConnectionRefusedError(f"bolt://neo4j:7687 {SECRETS['graph_error']}")
        )
        down = env.overview(1)
        assert down["health"] == {"postgres": "ok", "neo4j": "error: ConnectionRefusedError"}
    finally:
        container.graph_store = None
    assert env.overview(1)["health"]["neo4j"] == "skipped"


def test_isolated_window_gives_exact_numbers(pg_url: str) -> None:
    """2001-03-01 ~ 03-07(한국 시간)에만 있는 자료로 백분위·분포·사유·비용을 정확한 값으로 본다."""
    pytest.importorskip("sqlalchemy")
    from app.db.session import create_engine, create_session_factory
    from app.repositories.admin import AdminRepo

    def at(day: int, hour: int = 0, minute: int = 0, second: int = 0, month: int = 3) -> datetime:
        return datetime(2001, month, day, hour, minute, second, tzinfo=UTC)

    registered = str(uuid4())
    stub = f"adm-stub-{uuid4().hex[:8]}"
    heavy = f"adm-heavy-{uuid4().hex[:8]}"
    now = at(7, 3)  # 한국 시간 2001-03-07 12:00
    settings = _settings(
        cost_rewrite_input_per_mtok=5.0, cost_rewrite_output_per_mtok=25.0, cost_graph_per_episode=0.004
    )
    admin = AdminIdentity(user_id=str(uuid4()), username="boss")

    async def _seed(conn) -> None:  # noqa: ANN001
        await conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at, consent_version)"
            " VALUES ($1, $2, 'x', $3, '2001-03-01')",
            UUID(registered),
            _name("old"),
            at(2),
        )
        boss = await _add_contact(conn, registered, "superior", at(1, month=2))
        friend = await _add_contact(conn, stub, "friend", at(1, month=2))
        rows = [
            # (시각 UTC, 사용자, 상대, 조건, 채널, 상태, 지연, 위반 종류)
            (at(28, 14, 59, 59, month=2), registered, boss, "B", "web", "recommended", 5000, None),  # 기간 앞
            (at(28, 15, month=2), registered, boss, "B", "web", "recommended", 100, None),  # 03-01 00:00
            (at(4, 16), registered, boss, "B", "web", "recommended", 200, None),  # 03-05
            (at(5, 1), registered, boss, "A", "mobile", "recommended", 300, None),
            (at(5, 2), stub, friend, "C", "mobile", "fallback_original", 400, ["preserve_missing"] * 2),
            (at(5, 3), registered, boss, "C", "web", "recommended", 500, None),
            (
                at(6, 15, 30),
                registered,
                boss,
                "C",
                "web",
                "fallback_original",
                600,
                ["fact_added", "preserve_missing"],
            ),
            (at(7, 0), stub, friend, "D", "desktop", "fallback_original", 700, []),  # 위반 목록 없음
            (at(7, 1), registered, boss, "B", "web", "recommended", 800, None),
            (at(7, 2), registered, boss, "A", "mobile", "recommended", 900, None),
            (at(7, 14, 59, 59), registered, boss, "B", "web", "recommended", 1000, None),  # 03-07 23:59:59
            (at(7, 15), registered, boss, "B", "web", "fallback_original", 9000, ["refusal"]),  # 기간 뒤
        ]
        ids = []
        for when, user_id, contact_id, condition, channel, status, latency, checks in rows:
            ids.append(
                await _add_rewrite(
                    conn,
                    user_id=user_id,
                    contact_id=contact_id,
                    at=when,
                    status=status,
                    condition=condition,
                    channel=channel,
                    latency_ms=latency,
                    checks=checks,
                    token_usage={"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 40},
                )
            )
        fb = [
            # (요청 번호, 적용, 시각, 사유 코드, 편집본, 자유 서술)
            (1, True, at(1, 1), None, None, None),
            (2, True, at(5), None, f"{SECRETS['edited']} 고친 문장", None),
            (3, True, at(5, 2), None, "   ", None),  # 빈 편집본은 '수정'으로 세지 않는다
            (4, False, at(5, 3), "too_stiff", None, f"{SECRETS['reason']} 딱딱함"),
            (6, False, at(7), None, None, None),  # 사유를 고르지 않은 거절
            (8, False, at(7, 2), "fact_changed", None, None),
            (5, True, at(7, 16), None, None, None),  # 기간 뒤의 피드백
            (0, False, at(2), "too_stiff", None, None),  # 요청은 기간 앞, 피드백은 기간 안 → 센다
        ]
        for idx, accepted, when, code, edited, reason in fb:
            await _add_feedback(
                conn,
                request_id=ids[idx],
                user_id=rows[idx][1],
                accepted=accepted,
                at=when,
                reason_code=code,
                edited_text=edited,
                reason=reason,
            )
        await _add_usage(conn, registered, date(2001, 2, 28), 1, 99_999, 99_999)  # 기간 앞
        await _add_usage(conn, registered, date(2001, 3, 1), 1, 1000, 400)
        await _add_usage(conn, registered, date(2001, 3, 7), 200, 2000, 600)  # 하루 요청 한도(200)에 닿음
        await _add_usage(conn, stub, date(2001, 3, 5), 1, 500, 100)
        await _add_usage(conn, stub, date(2001, 3, 7), 1, 10, 5)
        await _add_usage(conn, heavy, date(2001, 3, 6), 3, 399_990, 10)  # 토큰 한도(400,000)에 닿음
        await _add_usage(conn, heavy, date(2001, 3, 8), 500, 0, 0)  # 기간 뒤

        conv = await _add_conversation(conn, registered, boss, at(1, month=2))
        for ingested in (at(28, 15, month=2), at(1), at(7, 14), at(28, 14, month=2), None, None):
            await _add_message(conn, registered, conv, at(1, month=2), ingested)
        for graph_status in ("pending", "running", "failed", "limited", "completed", "skipped"):
            await _add_job(conn, registered, conv, at(3), graph_status)

    async def _cleanup(conn) -> None:  # noqa: ANN001
        """넣은 행을 지운다. 2001년 사용량 행이 남으면 정리 작업 테스트(오래된 카운터 삭제 수)가 틀어진다."""
        owners = [registered, stub, heavy]
        # 상대를 지우면 요청·결과·피드백·대화방·메시지·작업이 FK CASCADE로 함께 지워진다
        await conn.execute("DELETE FROM contacts WHERE user_id = ANY($1::text[])", owners)
        await conn.execute("DELETE FROM usage_counters WHERE user_id = ANY($1::text[])", owners)
        await conn.execute("DELETE FROM users WHERE id = $1", UUID(registered))
        await conn.execute("DELETE FROM admin_access_log WHERE admin_user_id = $1", admin.user_id)

    async def _go():  # noqa: ANN202
        engine = create_engine(pg_url)
        repo = AdminRepo(create_session_factory(engine))
        service = AdminService(settings, repo, graph_store=None)
        conn = await _connect(pg_url)
        try:
            logged = await conn.fetchval(
                "SELECT count(*) FROM admin_access_log WHERE admin_user_id = $1", admin.user_id
            )
            base = await service.overview(admin, 7, now=now)
            await _seed(conn)
            seeded = await service.overview(admin, 7, now=now)
            today = await service.overview(admin, 1, now=now)
            logs = await conn.fetch(
                "SELECT admin_username, action, detail FROM admin_access_log WHERE admin_user_id = $1"
                " ORDER BY created_at",
                admin.user_id,
            )
            return logged, base, seeded, today, [tuple(r) for r in logs]
        finally:
            try:
                await _cleanup(conn)
            finally:
                await conn.close()
                await engine.dispose()

    logged, base, seeded, today, logs = _run(_go())
    assert logged == 0
    assert logs == [
        ("boss", "overview", "days=7"),
        ("boss", "overview", "days=7"),
        ("boss", "overview", "days=1"),
    ]

    # 자료를 넣기 전: 표본이 없으면 비율·백분위는 0이 아니라 None, 날짜는 0으로 채운 7칸
    empty = base.model_dump(mode="json", by_alias=True)
    assert empty["range"] == {
        "days": 7,
        "from": "2001-03-01T00:00:00+09:00",
        "to": "2001-03-08T00:00:00+09:00",
    }
    assert empty["rewrites"] == {
        "total": 0,
        "recommended": 0,
        "fallback": 0,
        "success_rate": None,
        "by_condition": {"A": 0, "B": 0, "C": 0, "D": 0},
        "by_channel": {"web": 0, "mobile": 0, "desktop": 0, "api": 0},
        "by_day": [{"day": f"2001-03-0{d}", "total": 0, "fallback": 0} for d in range(1, 8)],
    }
    assert empty["latency_ms"] == {"p50": None, "p95": None}
    assert empty["fallback_reasons"] == []
    assert empty["feedback"] == {
        "total": 0,
        "accepted": 0,
        "rejected": 0,
        "edited": 0,
        "acceptance_rate": None,
        "by_reason_code": [],
    }
    assert (empty["users"]["new"], empty["users"]["active"], empty["users"]["active_unregistered"]) == (
        0,
        0,
        0,
    )
    assert empty["tokens"] == {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "estimated_cost_usd": 0.0,  # 단가는 있고 쓴 토큰이 0 → 0달러 (단가가 없을 때만 null)
    }
    assert empty["graph"]["ingested_messages"] == 0 and empty["graph"]["estimated_cost_usd"] == 0.0
    assert empty["limits"]["users_at_daily_limit"] == 0
    assert (empty["limits"]["today_rewrites"], empty["limits"]["today_tokens"]) == (0, 0)

    out = seeded.model_dump(mode="json", by_alias=True)
    assert out["users"]["new"] == 1
    assert out["users"]["total"] - empty["users"]["total"] == 1
    assert (out["users"]["active"], out["users"]["active_unregistered"]) == (2, 1)
    assert out["rewrites"] == {
        "total": 10,
        "recommended": 7,
        "fallback": 3,
        "success_rate": 0.7,
        "by_condition": {"A": 2, "B": 4, "C": 3, "D": 1},
        "by_channel": {"web": 6, "mobile": 3, "desktop": 1, "api": 0},
        "by_day": [
            {"day": "2001-03-01", "total": 1, "fallback": 0},
            {"day": "2001-03-02", "total": 0, "fallback": 0},
            {"day": "2001-03-03", "total": 0, "fallback": 0},
            {"day": "2001-03-04", "total": 0, "fallback": 0},
            {"day": "2001-03-05", "total": 4, "fallback": 1},
            {"day": "2001-03-06", "total": 0, "fallback": 0},
            {"day": "2001-03-07", "total": 5, "fallback": 2},
        ],
    }
    # 지연 100, 200, …, 1000ms 열 개 → percentile_cont 보간값
    assert out["latency_ms"] == {"p50": 550, "p95": 955}
    assert out["fallback_reasons"] == [
        {"check": "preserve_missing", "count": 2},
        {"check": "fact_added", "count": 1},
        {"check": "unknown", "count": 1},
    ]
    assert out["feedback"] == {
        "total": 7,
        "accepted": 3,
        "rejected": 4,
        "edited": 1,
        "acceptance_rate": 0.4286,
        "by_reason_code": [
            {"code": "too_stiff", "count": 2},
            {"code": "fact_changed", "count": 1},
            {"code": "unspecified", "count": 1},
        ],
    }
    assert out["tokens"] == {
        "input": 403_500,
        "output": 1115,
        "cache_read": 400,  # 기간 안의 결과 10건 × 40
        "cache_creation": 0,
        "estimated_cost_usd": 2.045375,
    }
    assert out["graph"]["enabled"] is False
    assert out["graph"]["ingested_messages"] == 3
    assert out["graph"]["estimated_cost_usd"] == 0.012
    assert out["graph"]["pending_messages"] - empty["graph"]["pending_messages"] == 2
    assert {k: out["graph"]["jobs"][k] - empty["graph"]["jobs"][k] for k in out["graph"]["jobs"]} == {
        "pending": 1,
        "running": 1,
        "failed": 1,
        "limited": 1,
    }
    assert out["limits"] == {
        "users_at_daily_limit": 2,
        "daily_rewrite_limit": 200,
        "daily_token_limit": 400_000,
        "today_rewrites": 201,
        "today_tokens": 2615,
        "global_daily_rewrite_limit": 0,
        "global_daily_token_limit": 0,
    }
    assert out["health"] == {"postgres": "ok", "neo4j": "skipped"}

    # 같은 시점의 '오늘'(2001-03-07)만
    one = today.model_dump(mode="json", by_alias=True)
    assert one["range"]["from"] == "2001-03-07T00:00:00+09:00"
    assert one["rewrites"]["by_day"] == [{"day": "2001-03-07", "total": 5, "fallback": 2}]
    assert (one["rewrites"]["total"], one["rewrites"]["success_rate"]) == (5, 0.6)
    assert one["latency_ms"] == {"p50": 800, "p95": 980}
    assert one["fallback_reasons"] == [
        {"check": "fact_added", "count": 1},
        {"check": "preserve_missing", "count": 1},
        {"check": "unknown", "count": 1},
    ]
    assert (one["tokens"]["input"], one["tokens"]["output"]) == (2010, 605)
    assert one["limits"]["users_at_daily_limit"] == 1
    assert one["graph"]["ingested_messages"] == 1
    assert (one["feedback"]["total"], one["feedback"]["rejected"], one["feedback"]["acceptance_rate"]) == (
        2,
        2,
        0.0,
    )

    text = json.dumps([out, one], ensure_ascii=False)
    assert all(secret not in text for secret in SECRETS.values())
    assert registered not in text and stub not in text


def test_build_overview_keeps_unexpected_codes_countable() -> None:
    """조건·채널에 예상 밖의 값이 있어도 합계에서 빠지지 않고, 문장 같은 값은 other로 묶인다."""
    pytest.importorskip("sqlalchemy")
    from app.repositories.admin import OverviewRaw, RewriteGroup
    from app.services.admin_service import HealthSummary, build_overview

    period = period_bounds(1, datetime(2026, 11, 11, 3, 0, tzinfo=UTC), 9)
    day = period.last_day
    raw = OverviewRaw(
        users_total=3,
        users_new=1,
        active_users=2,
        active_unregistered=0,
        rewrite_groups=[
            RewriteGroup(day, "B", "web", "recommended", 3),
            RewriteGroup(day, "B", "tablet", "fallback_original", 1),
            RewriteGroup(day, "B", "채널이 아닌 문장", "pending", 1),
        ],
        latency_p50=1234.5,
        latency_p95=4000.4,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        fallback_reasons=[("preserve_missing", 1), ("문장이 들어온 경우", 2), ("또 다른 문장", 1)],
        feedback_total=0,
        feedback_accepted=0,
        feedback_rejected=0,
        feedback_edited=0,
        feedback_reasons=[],
        input_tokens=10,
        output_tokens=5,
        users_at_limit=0,
        today_rewrites=5,
        today_tokens=15,
        graph_ingested=0,
        graph_pending=0,
        graph_jobs={"failed": 2, "completed": 9},
    )
    out = build_overview(period, raw, _settings(), False, HealthSummary(postgres="ok", neo4j="skipped"))
    assert (out.rewrites.total, out.rewrites.recommended, out.rewrites.fallback) == (5, 3, 1)
    assert out.rewrites.success_rate == 0.6
    assert out.rewrites.by_channel == {"web": 3, "mobile": 0, "desktop": 0, "api": 0, "tablet": 1, "other": 1}
    assert out.rewrites.by_day[0].total == 5 and out.rewrites.by_day[0].fallback == 1
    assert (out.latency_ms.p50, out.latency_ms.p95) == (1234, 4000)
    assert [(r.check, r.count) for r in out.fallback_reasons] == [("other", 3), ("preserve_missing", 1)]
    assert out.graph.jobs.model_dump() == {"pending": 0, "running": 0, "failed": 2, "limited": 0}
    assert out.tokens.estimated_cost_usd is None and out.graph.estimated_cost_usd is None


# ---------- 보류된 그래프 메시지 다시 시도 (POST /v1/admin/graph/retry-held) ----------

RETRY_PATH = "/v1/admin/graph/retry-held"


def test_retry_held_follows_the_same_access_checks() -> None:
    client = TestClient(create_app(_settings()))
    assert client.post(RETRY_PATH).status_code == 401
    assert client.post(RETRY_PATH, headers={"X-User-Id": "boss"}).status_code == 401

    user = client.post("/v1/auth/register", json={"username": "minsu_kim", "password": PASSWORD}).json()
    denied = client.post(RETRY_PATH, headers={"X-CSRF-Token": user["csrf_token"]})
    assert denied.status_code == 403 and _code(denied) == "forbidden"

    # 관리자라도 CSRF 토큰이 없으면 403, 있으면 저장소 검사(인메모리 503)까지 간다
    client = TestClient(create_app(_settings()))
    boss = client.post("/v1/auth/register", json={"username": "boss", "password": PASSWORD}).json()
    no_csrf = client.post(RETRY_PATH)
    assert no_csrf.status_code == 403 and _code(no_csrf) == "forbidden"
    assert "CSRF" in no_csrf.json()["message"]
    assert client.post(RETRY_PATH, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    unavailable = client.post(RETRY_PATH, headers={"X-CSRF-Token": boss["csrf_token"]})
    assert unavailable.status_code == 503 and _code(unavailable) == "admin_unavailable"


class _CountingIngest:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def release_held(self, user_id: str | None = None, conversation_id: UUID | None = None):  # noqa: ANN201
        scope = [str(v) for v in (user_id, conversation_id) if v is not None]
        self.calls.append(":".join(["release", *scope]))
        return {"u1": 2, "u2": 1}

    def start_resume(self, user_ids) -> bool:  # noqa: ANN001
        self.calls.append("start:" + ",".join(sorted(user_ids)))
        return True


class _LogRepo:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str | None]] = []

    async def log_access(self, user_id: str, username: str, action: str, detail: str | None) -> None:
        self.rows.append((action, detail or None))


def test_retry_held_service_logs_first_and_reports_counts() -> None:
    admin = AdminIdentity(user_id=str(uuid4()), username="boss")

    # 접근 기록을 남기지 못하면 아무것도 풀지 않는다
    ingest = _CountingIngest()
    down = AdminService(_settings(), _LogDownRepo(), graph_ingest=ingest)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="log down"):
        asyncio.run(down.retry_held(admin))
    assert ingest.calls == []

    repo = _LogRepo()
    service = AdminService(_settings(), repo, graph_ingest=ingest)  # type: ignore[arg-type]
    out = asyncio.run(service.retry_held(admin))
    assert out.model_dump() == {"released": 3, "users": 2, "started": True}
    assert ingest.calls == ["release", "start:u1,u2"]
    assert repo.rows == [("graph_retry_held", None), ("graph_retry_held_done", "released=3 users=2")]

    # 범위를 주면 그대로 넘기고, 접근 기록에는 사용자 id를 줄여 적는다
    conv, full = uuid4(), str(uuid4())
    asyncio.run(service.retry_held(admin, user_id=full, conversation_id=conv))
    assert ingest.calls[-2:] == [f"release:{full}:{conv}", "start:u1,u2"]
    assert repo.rows[-2] == ("graph_retry_held", f"user={short_user_id(full)} conversation={conv}")
    assert full not in json.dumps(repo.rows)

    # 그래프가 꺼진 서버: 기록은 남고, 분명한 오류로 끝난다
    off_repo = _LogRepo()
    with pytest.raises(GraphDisabledError):
        asyncio.run(AdminService(_settings(), off_repo).retry_held(admin))  # type: ignore[arg-type]
    assert off_repo.rows == [("graph_retry_held", None)]


# ---------- 보류 목록과 범위를 고른 다시 시도 (인메모리 저장소로 API까지) ----------

HELD_PATH = "/v1/admin/graph/held"


class _NoEngine:
    """접근 검사의 저장소 확인(503)만 넘기는 자리 표시. 집계 SQL은 돌지 않는다."""

    async def dispose(self) -> None:
        return None


@dataclass
class HeldEnv:
    """인메모리 앱 + 가짜 그래프 저장소. 관리자 집계 저장소는 접근 기록만 받는 `_LogRepo`로 바꿔 둔다."""

    client: TestClient
    boss: dict[str, str]
    log: _LogRepo
    ingest: Any
    store: Any
    conversations: dict[str, UUID]  # "ua/c1" → 대화방 id

    @property
    def container(self) -> Any:
        return self.client.app.state.container

    def as_boss(self, csrf: bool = True) -> dict[str, str]:
        self.client.cookies.clear()
        self.client.cookies.set("pi_session", self.boss["token"])
        return {"X-CSRF-Token": self.boss["csrf"]} if csrf else {}

    def held(self) -> list[dict[str, Any]]:
        self.as_boss()
        r = self.client.get(HELD_PATH)
        assert r.status_code == 200, r.text
        return r.json()

    def retry(self, body: dict[str, Any] | None = None):  # noqa: ANN201
        headers = self.as_boss()
        if body is None:
            return self.client.post(RETRY_PATH, headers=headers)
        return self.client.post(RETRY_PATH, headers=headers, json=body)

    def held_counts(self) -> dict[str, int]:
        """대화방 별명 → 지금 보류된 메시지 수 (저장소에서 직접 센다)."""

        async def _count() -> dict[str, int]:
            out = {}
            for key, conv_id in self.conversations.items():
                stats = await self.container.conversations.conversation_stats(key.split("/")[0], conv_id)
                out[key] = stats.graph_held
            return out

        return self.client.portal.call(_count)


HELD_AT = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


@pytest.fixture
def held_env():  # noqa: ANN201
    """사용자 ua(방 c1 2건·c2 1건)와 ub(방 c1 2건)의 메시지가 전부 보류된 상태."""
    from app.graph.ingest import GraphIngestService
    from app.services.import_service import ImportMessage, ImportPayload
    from tests.test_graph import FakeGraphStore

    app = create_app(_settings())
    with TestClient(app) as client:
        r = client.post("/v1/auth/register", json={"username": "boss", "password": PASSWORD})
        assert r.status_code == 201, r.text
        boss = {
            "token": r.cookies.get("pi_session"),
            "csrf": r.json()["csrf_token"],
            "user_id": r.json()["user_id"],
        }
        container = app.state.container
        log_repo = _LogRepo()
        container.engine = _NoEngine()
        app.state.admin_repo = (container.engine, log_repo)
        store = FakeGraphStore()
        ingest = GraphIngestService(store, container.conversations, container.contacts, max_attempts=3)
        container.graph_store, container.graph_ingest = store, ingest

        async def _seed() -> dict[str, UUID]:
            made: dict[str, UUID] = {}
            minute = 0
            for user, key, count in (("ua", "c1", 2), ("ua", "c2", 1), ("ub", "c1", 2)):
                contact = await container.contacts.create(user, f"{SECRETS['alias']} {key}", "superior")
                conv = await container.import_service.create_conversation(
                    user, contact.id, f"{SECRETS['title']} 방", "json", key, me_name="김민수"
                )
                messages = [
                    ImportMessage(
                        id=f"m{i:02d}",
                        speaker="me",
                        sent_at=f"2026-10-05T09:{minute + i:02d}:00+09:00",
                        text=f"{SECRETS['message']} 수요일까지 가능합니다 {i}.",
                    )
                    for i in range(count)
                ]
                minute += 10
                await container.import_service.import_messages(
                    user, conv.id, ImportPayload(messages=messages)
                )
                for i, m in enumerate(await container.conversations.list_messages(user, conv.id)):
                    # 세 번 실패해 보류된 것처럼: 횟수 3, 보류 시각은 방마다·메시지마다 다르게
                    at = HELD_AT.replace(hour=3 + len(made), minute=i)
                    for attempt in range(3):
                        await container.conversations.record_graph_failure(user, m.id, attempt == 2, at)
                made[f"{user}/{key}"] = conv.id
            return made

        conversations = client.portal.call(_seed)
        yield HeldEnv(
            client=client, boss=boss, log=log_repo, ingest=ingest, store=store, conversations=conversations
        )
        container.graph_store, container.graph_ingest = None, None


def test_held_list_groups_by_conversation_without_private_text(held_env: HeldEnv) -> None:
    rows = held_env.held()
    assert all(sorted(row) == sorted(HELD_FIELDS) for row in rows)
    by_conv = {row["conversation_id"]: row for row in rows}
    ids = {key: str(conv_id) for key, conv_id in held_env.conversations.items()}
    assert set(by_conv) == set(ids.values())
    # 최근에 보류된 방부터
    assert [row["conversation_id"] for row in rows] == [ids["ub/c1"], ids["ua/c2"], ids["ua/c1"]]
    first = by_conv[ids["ua/c1"]]
    assert (first["messages"], first["attempts"]) == (2, 3)
    assert first["oldest_held_at"] == "2026-11-20T03:00:00Z"
    assert first["latest_held_at"] == "2026-11-20T03:01:00Z"
    assert by_conv[ids["ua/c2"]]["messages"] == 1 and by_conv[ids["ub/c1"]]["messages"] == 2
    # 사용자 id는 줄인다 — 같은 사용자의 두 방은 같은 줄인 id
    assert first["user_id"] == by_conv[ids["ua/c2"]]["user_id"] == short_user_id("ua")
    assert by_conv[ids["ub/c1"]]["user_id"] == short_user_id("ub")

    # 문장이 들어가는 필드가 없다: 본문·상대 이름·방 제목 표식이 응답과 접근 기록 어디에도 없다
    body = json.dumps([rows, held_env.log.rows], ensure_ascii=False)
    for label, secret in SECRETS.items():
        assert secret not in body, f"{label} 표식이 보류 목록에 나왔다"
    for forbidden_key in ('"text"', "title", "speaker", "alias", "me_name", "source_key"):
        assert forbidden_key not in body, forbidden_key
    assert held_env.log.rows == [("graph_held", "limit=50")]

    held_env.as_boss()
    assert len(held_env.client.get(f"{HELD_PATH}?limit=1").json()) == 1
    assert held_env.client.get(f"{HELD_PATH}?limit=0").status_code == 422
    assert held_env.client.get(HELD_PATH).headers["cache-control"] == "no-store"


def test_held_list_and_retry_refuse_non_admins(held_env: HeldEnv) -> None:
    client = held_env.client
    client.cookies.clear()
    assert client.get(HELD_PATH).status_code == 401
    assert client.get(HELD_PATH, headers={"X-User-Id": "boss"}).status_code == 401
    user = client.post("/v1/auth/register", json={"username": "minsu_kim", "password": PASSWORD}).json()
    scope = {"conversation_id": str(held_env.conversations["ua/c1"])}
    denied = client.get(HELD_PATH)
    assert denied.status_code == 403 and _code(denied) == "forbidden"
    denied = client.post(RETRY_PATH, json=scope, headers={"X-CSRF-Token": user["csrf_token"]})
    assert denied.status_code == 403 and _code(denied) == "forbidden"
    # 관리자여도 CSRF 토큰이 없으면 풀지 않는다
    held_env.as_boss()
    assert client.post(RETRY_PATH, json=scope).status_code == 403
    assert held_env.held_counts() == {"ua/c1": 2, "ua/c2": 1, "ub/c1": 2}
    assert held_env.log.rows == [] and held_env.store.added == []


def test_retry_held_releases_only_the_chosen_scope(held_env: HeldEnv) -> None:
    ids = held_env.conversations
    wait = held_env.ingest.wait_tasks

    # 대화방 하나만: 같은 사용자의 다른 방과 다른 사용자는 그대로 보류다
    one = held_env.retry({"conversation_id": str(ids["ua/c2"])})
    assert one.status_code == 200, one.text
    assert one.json() == {"released": 1, "users": 1, "started": True}
    held_env.client.portal.call(wait)
    assert held_env.held_counts() == {"ua/c1": 2, "ua/c2": 0, "ub/c1": 2}
    assert [(a["group_id"], a["name"]) for a in held_env.store.added] == [("ua", "c2:m00")]
    assert [row["conversation_id"] for row in held_env.held()] == [str(ids["ub/c1"]), str(ids["ua/c1"])]

    # 사용자와 대화방이 어긋나면(남의 방) 아무것도 풀지 않는다. 없는 방·없는 사용자도 0건이다
    for scope in (
        {"user_id": "ub", "conversation_id": str(ids["ua/c1"])},
        {"conversation_id": str(uuid4())},
        {"user_id": "nobody"},
    ):
        none = held_env.retry(scope)
        assert none.status_code == 200 and none.json() == {"released": 0, "users": 0, "started": False}
    assert held_env.held_counts() == {"ua/c1": 2, "ua/c2": 0, "ub/c1": 2}

    # 사용자 하나만
    user = held_env.retry({"user_id": "ua"})
    assert user.json() == {"released": 2, "users": 1, "started": True}
    held_env.client.portal.call(wait)
    assert held_env.held_counts() == {"ua/c1": 0, "ua/c2": 0, "ub/c1": 2}
    assert sorted(a["name"] for a in held_env.store.added if a["group_id"] == "ua") == [
        "c1:m00",
        "c1:m01",
        "c2:m00",
    ]
    assert not [a for a in held_env.store.added if a["group_id"] == "ub"]

    # 잘못된 범위는 422이고 아무것도 풀지 않는다
    assert held_env.retry({"conversation_id": "not-a-uuid"}).status_code == 422
    assert held_env.retry({"user_id": ""}).status_code == 422
    assert held_env.held_counts()["ub/c1"] == 2

    # 접근 기록: 범위와 결과가 남고, 사용자 id는 줄여 적는다
    retries = [row for row in held_env.log.rows if row[0].startswith("graph_retry_held")]
    assert retries[:2] == [
        ("graph_retry_held", f"conversation={ids['ua/c2']}"),
        ("graph_retry_held_done", "released=1 users=1"),
    ]
    assert ("graph_retry_held", f"user={short_user_id('ub')} conversation={ids['ua/c1']}") in retries
    assert retries[-2:] == [
        ("graph_retry_held", f"user={short_user_id('ua')}"),
        ("graph_retry_held_done", "released=2 users=1"),
    ]


@pytest.mark.parametrize("body", [None, {}])
def test_retry_held_without_scope_releases_everything(held_env: HeldEnv, body: dict | None) -> None:
    done = held_env.retry(body)
    assert done.status_code == 200, done.text
    assert done.json() == {"released": 5, "users": 2, "started": True}
    held_env.client.portal.call(held_env.ingest.wait_tasks)
    assert held_env.held_counts() == {"ua/c1": 0, "ua/c2": 0, "ub/c1": 0}
    assert len(held_env.store.added) == 5
    assert held_env.held() == []
    assert ("graph_retry_held", None) in held_env.log.rows
    assert held_env.retry(body).json() == {"released": 0, "users": 0, "started": False}


def test_retry_held_releases_and_reingests_in_the_background(env: AdminEnv) -> None:
    from app.graph.ingest import GraphIngestService
    from tests.test_graph import FakeGraphStore

    container = env.client.app.state.container
    store = FakeGraphStore()
    ingest = GraphIngestService(store, container.conversations, container.contacts, max_attempts=2)
    broken: dict[str, Any] = {"on": True, "first": None}
    calls: dict[str, int] = {}
    orig = store.add_episode

    async def add_episode(**kw):  # noqa: ANN003, ANN202
        # 붙여넣기 가져오기는 메시지 키를 서버가 만든다 — 처음 본 에피소드 하나를 (고칠 때까지) 실패시킨다
        name = kw["name"]
        calls[name] = calls.get(name, 0) + 1
        broken["first"] = broken["first"] or name
        if broken["on"] and name == broken["first"]:
            raise ValueError(f"{SECRETS['graph_error']} 기록에 남으면 안 된다")
        await orig(**kw)

    store.add_episode = add_episode  # type: ignore[method-assign]
    imports = container.import_service
    saved = (container.graph_store, container.graph_ingest, imports._graph_ingest, imports.graph_mode)
    try:
        container.graph_store, container.graph_ingest = store, ingest
        imports._graph_ingest, imports.graph_mode = ingest, "sync"

        env.use(None)
        stub = {"X-User-Id": f"held-{uuid4().hex[:12]}"}
        contact = env.client.post(
            "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=stub
        ).json()["id"]
        conv_id = env.client.post(
            "/v1/conversations", json={"contact_id": contact, "source_key": "c1"}, headers=stub
        ).json()["id"]
        lines = [
            "2026-10-05 09:25 | contact | 언제까지 가능해요?",
            "2026-10-05 09:31 | me | 수요일까지 가능합니다.",
        ]
        job = env.client.post(
            f"/v1/conversations/{conv_id}/imports/raw", json={"text": "\n".join(lines) + "\n"}, headers=stub
        )
        assert job.status_code == 202, job.text
        held_name = broken["first"]

        def stats() -> tuple[int, int, int]:
            env.use(None)
            s = env.client.get(f"/v1/conversations/{conv_id}/stats", headers=stub).json()
            return s["graph_ingested"], s["graph_held"], s["graph_pending"]

        assert stats() == (1, 1, 0) and calls[held_name] == 2
        held_before = env.overview(1)["graph"]["held_messages"]
        assert held_before >= 1

        # 관리자가 아니면, 그리고 CSRF 토큰이 없으면 아무것도 풀리지 않고 기록도 남지 않는다
        logged = env.access_log_count()
        user = env.register(_name())
        env.use(user["token"])
        denied = env.client.post(RETRY_PATH, headers={"X-CSRF-Token": user["csrf"]})
        assert denied.status_code == 403 and _code(denied) == "forbidden"
        env.use(env.boss_token)
        boss_csrf = {"X-CSRF-Token": env.client.get("/v1/auth/me").json()["csrf_token"]}
        assert env.client.post(RETRY_PATH).status_code == 403
        assert env.access_log_count() == logged
        assert stats() == (1, 1, 0)

        # 그래프가 꺼진 서버에서는 409 — 보류는 그대로 둔다
        container.graph_ingest = None
        env.use(env.boss_token)
        off = env.client.post(RETRY_PATH, headers=boss_csrf)
        assert off.status_code == 409 and _code(off) == "graph_disabled"
        assert stats() == (1, 1, 0)
        container.graph_ingest = ingest

        # 풀고 → 백그라운드로 다시 넣는다. 요청은 수집을 기다리지 않는다
        broken["on"] = False
        env.use(env.boss_token)
        done = env.client.post(RETRY_PATH, headers=boss_csrf)
        assert done.status_code == 200, done.text
        body = done.json()
        assert sorted(body) == ["released", "started", "users"]
        # 테스트 DB는 다른 테스트와 함께 쓴다 — 남의 보류가 섞여 풀릴 수 있어 아래로만 본다
        assert body["released"] >= 1 and body["users"] >= 1 and body["started"] is True
        assert done.headers["cache-control"] == "no-store"
        env.client.portal.call(ingest.wait_tasks)
        assert stats() == (2, 0, 0)
        assert calls[held_name] == 3  # 풀린 메시지를 한 번 더 불렀고, 이미 들어간 것은 다시 부르지 않았다
        assert [a["name"] for a in store.added if a["group_id"] == stub["X-User-Id"]].count(held_name) == 1
        assert sum(1 for a in store.added if a["group_id"] == stub["X-User-Id"]) == 2
        assert env.overview(1)["graph"]["held_messages"] <= held_before - 1

        # 접근 기록: 요청과 결과가 한 줄씩. 사용자 id나 문장은 없다
        log_rows = env.admin_get("/v1/admin/access-log?limit=20").json()
        result = next(r for r in log_rows if r["action"] == "graph_retry_held_done")
        assert result["detail"] == f"released={body['released']} users={body['users']}"
        assert any(r["action"] == "graph_retry_held" and r["detail"] is None for r in log_rows)
        assert SECRETS["graph_error"] not in json.dumps([body, log_rows], ensure_ascii=False)

        # 풀 것이 없으면 0건이고 수집을 띄우지 않는다
        env.use(env.boss_token)
        again = env.client.post(RETRY_PATH, headers=boss_csrf).json()
        assert again == {"released": 0, "users": 0, "started": False}
    finally:
        container.graph_store, container.graph_ingest, imports._graph_ingest, imports.graph_mode = saved


def test_held_list_and_scoped_retry_on_postgres(env: AdminEnv) -> None:
    """PostgreSQL 저장소의 묶기 SQL과 범위 풀기를 API로 본다 (테스트 DB는 함께 쓰므로 우리 방만 확인한다)."""
    from app.graph.ingest import GraphIngestService
    from tests.test_graph import FakeGraphStore

    container = env.client.app.state.container
    store = FakeGraphStore()
    ingest = GraphIngestService(store, container.conversations, container.contacts, max_attempts=3)
    saved = (container.graph_store, container.graph_ingest)
    user = env.register(_name("held"))
    stub_id = f"held-{uuid4().hex[:12]}"
    held_at = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)

    async def _seed() -> dict[str, UUID]:
        conn = await _connect(env.pg_url)
        try:
            made = {}
            for owner in (user["user_id"], stub_id):
                contact = await _add_contact(conn, owner, "superior", held_at)
                conv = await _add_conversation(conn, owner, contact, held_at)
                for _ in range(2):
                    await _add_message(conn, owner, conv, held_at, None)
                await conn.execute(
                    "UPDATE messages SET graph_attempts = 3, graph_held_at = $2 WHERE conversation_id = $1",
                    conv,
                    held_at,
                )
                made[owner] = conv
            return made
        finally:
            await conn.close()

    convs = _run(_seed())
    try:
        container.graph_store, container.graph_ingest = store, ingest
        listed = env.admin_get("/v1/admin/graph/held?limit=200")
        assert listed.status_code == 200, listed.text
        assert all(sorted(row) == sorted(HELD_FIELDS) for row in listed.json())
        by_conv = {row["conversation_id"]: row for row in listed.json()}
        for owner, conv in convs.items():
            row = by_conv[str(conv)]
            assert (row["user_id"], row["messages"], row["attempts"]) == (short_user_id(owner), 2, 3)
            assert row["oldest_held_at"] == row["latest_held_at"] == "2026-11-20T03:00:00Z"
        body = json.dumps(listed.json(), ensure_ascii=False)
        for label, secret in SECRETS.items():
            assert secret not in body, f"{label} 표식이 보류 목록에 나왔다"
        assert user["user_id"] not in body and stub_id not in body and env.boss_id not in body

        env.use(env.boss_token)
        csrf = {"X-CSRF-Token": env.client.get("/v1/auth/me").json()["csrf_token"]}
        # 사용자와 대화방이 어긋나면 풀지 않는다
        miss = env.client.post(
            RETRY_PATH,
            headers=csrf,
            json={"user_id": stub_id, "conversation_id": str(convs[user["user_id"]])},
        )
        assert miss.status_code == 200 and miss.json() == {"released": 0, "users": 0, "started": False}
        # 대화방 하나만, 그다음 사용자 하나만
        one = env.client.post(RETRY_PATH, headers=csrf, json={"conversation_id": str(convs[stub_id])})
        assert one.status_code == 200 and one.json() == {"released": 2, "users": 1, "started": True}
        env.client.portal.call(ingest.wait_tasks)
        left = {row["conversation_id"] for row in env.admin_get("/v1/admin/graph/held?limit=200").json()}
        assert str(convs[stub_id]) not in left and str(convs[user["user_id"]]) in left
        env.use(env.boss_token)
        mine = env.client.post(RETRY_PATH, headers=csrf, json={"user_id": user["user_id"]})
        assert mine.status_code == 200 and mine.json() == {"released": 2, "users": 1, "started": True}
        env.client.portal.call(ingest.wait_tasks)
        assert {a["group_id"] for a in store.added} == {user["user_id"], stub_id} and len(store.added) == 4

        log_rows = env.admin_get("/v1/admin/access-log?limit=20").json()
        details = [r["detail"] for r in log_rows if r["action"] == "graph_retry_held"]
        assert f"user={short_user_id(user['user_id'])}" in details
        assert f"conversation={convs[stub_id]}" in details
        assert user["user_id"] not in json.dumps(log_rows, ensure_ascii=False)
        assert any(r["action"] == "graph_held" and r["detail"] == "limit=200" for r in log_rows)
    finally:
        container.graph_store, container.graph_ingest = saved
