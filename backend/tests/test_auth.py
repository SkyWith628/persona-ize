"""세션 쿠키 인증: 가입→쿠키→조회, 쓰기는 CSRF 필수, 로그아웃 뒤 401, 헤더 스텁 끄기, 비밀번호 해시."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import hash_password, token_hash, verify_password
from app.main import create_app

CRED = {"username": "minsu_kim", "password": "correct horse battery"}


def _client(**overrides) -> TestClient:  # noqa: ANN003
    # 이 파일은 세션·CSRF·비밀번호 흐름을 본다. 승인 대기(기본 true)는 test_members.py에서 따로 본다.
    overrides.setdefault("auth_signup_requires_approval", False)
    s = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[call-arg]
    return TestClient(create_app(s))


def test_password_hash_roundtrip_and_format() -> None:
    stored = hash_password("pw-1234567")
    assert stored.startswith("scrypt$16384$8$1$")
    assert verify_password("pw-1234567", stored)
    assert not verify_password("pw-1234568", stored)
    assert not verify_password("x", "garbage")
    assert hash_password("same") != hash_password("same")  # 소금
    assert len(token_hash("abc")) == 64


def test_register_login_cookie_csrf_logout() -> None:
    client = _client()
    r = client.post("/v1/auth/register", json=CRED)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["username"] == "minsu_kim" and body["csrf_token"]
    cookie = r.cookies.get("pi_session")
    assert (
        cookie
        and "HttpOnly" in r.headers["set-cookie"]
        and "SameSite=lax" in r.headers["set-cookie"].replace("Lax", "lax")
    )
    assert "pi_session" not in r.text  # 토큰 원문은 본문에 없다

    # 쿠키만으로 읽기 OK
    assert client.get("/v1/contacts").status_code == 200
    me = client.get("/v1/auth/me").json()
    assert me["user_id"] == body["user_id"] and me["csrf_token"] == body["csrf_token"]

    # 쓰기: CSRF 없으면 403, 틀리면 403, 맞으면 201
    payload = {"alias": "박지현", "relationship_type": "superior"}
    assert client.post("/v1/contacts", json=payload).status_code == 403
    assert client.post("/v1/contacts", json=payload, headers={"X-CSRF-Token": "nope"}).status_code == 403
    ok = client.post("/v1/contacts", json=payload, headers={"X-CSRF-Token": body["csrf_token"]})
    assert ok.status_code == 201, ok.text
    # 쿠키 사용자의 데이터는 쿠키 사용자 id로 격리된다
    assert client.get("/v1/contacts", headers={"X-User-Id": "someone"}).json() == [
        client.get("/v1/contacts").json()[0]
    ]  # 쿠키가 있으면 헤더는 무시

    # 중복 이름 409, 틀린 비밀번호 401 (존재 여부 노출 없음)
    assert client.post("/v1/auth/register", json=CRED).status_code == 409
    wrong = client.post("/v1/auth/login", json={**CRED, "password": "wrong-password"})
    assert wrong.status_code == 401
    assert (
        client.post("/v1/auth/login", json={"username": "nobody_here", "password": "whatever-1"}).json()[
            "message"
        ]
        == wrong.json()["message"]
    )

    # 로그아웃 → 세션 폐기, 같은 쿠키로 401
    assert client.post("/v1/auth/logout").status_code == 204
    client.cookies.set("pi_session", cookie)
    assert client.get("/v1/auth/me").status_code == 401
    assert client.get("/v1/contacts").status_code == 401

    # 다시 로그인하면 새 세션·새 CSRF
    again = client.post("/v1/auth/login", json=CRED)
    assert again.status_code == 200 and again.json()["csrf_token"] != body["csrf_token"]


def test_header_stub_can_be_disabled_and_register_closed() -> None:
    client = _client(auth_allow_header_stub=False, auth_allow_register=False)
    assert client.get("/v1/contacts", headers={"X-User-Id": "user-a"}).status_code == 401
    assert client.get("/v1/contacts").status_code == 401
    assert client.post("/v1/auth/register", json=CRED).status_code == 403


def test_register_validation() -> None:
    client = _client()
    assert client.post("/v1/auth/register", json={"username": "ab", "password": "x" * 10}).status_code == 422
    assert (
        client.post("/v1/auth/register", json={"username": "bad name!", "password": "x" * 10}).status_code
        == 422
    )
    assert (
        client.post("/v1/auth/register", json={"username": "fine_name", "password": "short"}).status_code
        == 422
    )


def test_login_throttle_by_username_then_recovers_on_success() -> None:
    client = _client(auth_max_failures=3, auth_lockout_minutes=15)
    client.post("/v1/auth/register", json=CRED)
    client.post("/v1/auth/logout")
    bad = {**CRED, "password": "wrong-password-1"}
    for _ in range(3):
        assert client.post("/v1/auth/login", json=bad).status_code == 401
    blocked = client.post("/v1/auth/login", json=CRED)  # 맞는 비밀번호여도 잠금
    assert blocked.status_code == 429 and blocked.json()["code"] == "too_many_attempts"
    assert "초 뒤" in blocked.json()["message"]
    # 다른 이름은 영향 없음 (IP는 TestClient가 같지만 이름 키가 먼저 걸린 상태라 IP 키도 3회 → 함께 잠긴다)
    other = client.post("/v1/auth/login", json={"username": "someone_else", "password": "whatever-1"})
    assert other.status_code in (401, 429)


def test_login_throttle_unit_and_reset() -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.auth_service import LoginThrottle

    t = LoginThrottle(2, timedelta(minutes=10))
    now = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    assert t.retry_after("k", now) is None
    t.record_failure("k", now)
    t.record_failure("k", now + timedelta(minutes=1))
    assert t.retry_after("k", now + timedelta(minutes=2)) == 8 * 60  # 첫 실패가 창을 벗어나는 시각까지
    assert t.retry_after("k", now + timedelta(minutes=10, seconds=1)) is None  # 창 지나면 풀림
    t.record_failure("k", now)
    t.record_failure("k", now)
    t.reset("k")
    assert t.retry_after("k", now) is None


def test_change_password_revokes_other_sessions() -> None:
    client = _client()
    reg = client.post("/v1/auth/register", json=CRED)
    csrf = reg.json()["csrf_token"]
    # 두 번째 기기(세션)
    second = TestClient(client.app)
    second.post("/v1/auth/login", json=CRED)
    assert second.get("/v1/auth/me").status_code == 200

    # CSRF 없으면 403, 현재 비밀번호 틀리면 401, 짧으면 422, 같으면 422
    body = {"current_password": CRED["password"], "new_password": "brand-new-password-9"}
    assert client.post("/v1/auth/password", json=body).status_code == 403
    h = {"X-CSRF-Token": csrf}
    assert (
        client.post(
            "/v1/auth/password", json={**body, "current_password": "nope-nope-1"}, headers=h
        ).status_code
        == 401
    )
    assert (
        client.post("/v1/auth/password", json={**body, "new_password": "short"}, headers=h).status_code == 422
    )
    assert (
        client.post(
            "/v1/auth/password", json={**body, "new_password": CRED["password"]}, headers=h
        ).status_code
        == 422
    )
    ok = client.post("/v1/auth/password", json=body, headers=h)
    assert ok.status_code == 200 and ok.json()["revoked_sessions"] == 1

    # 현재 세션은 유지, 다른 세션은 로그아웃, 옛 비밀번호는 거부, 새 비밀번호로 로그인 OK
    assert client.get("/v1/auth/me").status_code == 200
    assert second.get("/v1/auth/me").status_code == 401
    assert client.post("/v1/auth/login", json=CRED).status_code == 401
    assert client.post("/v1/auth/login", json={**CRED, "password": body["new_password"]}).status_code == 200


def test_delete_account_requires_password_and_csrf_then_wipes() -> None:
    client = _client()
    reg = client.post("/v1/auth/register", json=CRED)
    csrf = reg.json()["csrf_token"]
    client.post(
        "/v1/contacts",
        json={"alias": "박지현", "relationship_type": "superior"},
        headers={"X-CSRF-Token": csrf},
    )
    assert (
        client.request("DELETE", "/v1/auth/me", json={"password": CRED["password"]}).status_code == 403
    )  # CSRF 없음
    assert (
        client.request(
            "DELETE", "/v1/auth/me", json={"password": "nope-nope-1"}, headers={"X-CSRF-Token": csrf}
        ).status_code
        == 401
    )
    ok = client.request(
        "DELETE", "/v1/auth/me", json={"password": CRED["password"]}, headers={"X-CSRF-Token": csrf}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["contacts"] == 1
    assert client.get("/v1/auth/me").status_code == 401
    assert client.post("/v1/auth/login", json=CRED).status_code == 401  # 사용자 없음 → 같은 401
    assert client.post("/v1/auth/register", json=CRED).status_code == 201  # 이름 재사용 가능


def test_login_throttle_prunes_stale_keys() -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.auth_service import LoginThrottle

    t = LoginThrottle(max_failures=2, lockout=timedelta(minutes=15))
    t0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    for i in range(5):
        t.record_failure(f"user{i}|1.2.3.4", t0)
    t.record_failure("fresh|1.2.3.4", t0 + timedelta(minutes=14))
    assert t.prune_all(t0 + timedelta(minutes=15)) == 5  # 15분 지난 다섯 키만 비움
    assert t.retry_after("fresh|1.2.3.4", t0 + timedelta(minutes=15)) is None  # 1회 실패라 안 막힘
    assert len(t._failures) == 1


@pytest.mark.anyio
async def test_housekeeping_purges_sessions_jobs_and_usage_in_memory() -> None:
    from datetime import UTC, date, datetime, timedelta
    from uuid import uuid4

    from app.domain.auth import Session
    from app.domain.conversation import ImportJob
    from app.repositories.auth import InMemoryAuthRepo
    from app.repositories.memory import InMemoryConversationRepo
    from app.repositories.usage import InMemoryUsageRepo
    from app.services.housekeeping import Housekeeping

    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    auth, convs, usage = InMemoryAuthRepo(), InMemoryConversationRepo(), InMemoryUsageRepo()
    uid = uuid4()

    def sess(key: str, expires: datetime, revoked: datetime | None = None) -> None:
        auth._sessions[key] = Session(
            token_hash=key,
            user_id=uid,
            csrf_token="c",
            created_at=now - timedelta(days=30),
            expires_at=expires,
            revoked_at=revoked,
        )

    sess("active", now + timedelta(days=5))
    sess("expired-recent", now - timedelta(days=2))  # 유예 7일 안 → 남김
    sess("expired-old", now - timedelta(days=10))
    sess("revoked-old", now + timedelta(days=5), revoked=now - timedelta(days=8))

    def job(age_days: int, graph_status: str = "completed") -> None:
        j = ImportJob(
            id=uuid4(),
            user_id="u",
            conversation_id=uuid4(),
            source_hash="h",
            status="completed",
            graph_status=graph_status,
            created_at=now - timedelta(days=age_days),
        )
        convs._jobs[j.id] = j

    job(1)
    job(40)
    job(45, graph_status="pending")  # 수집 중 → 남김
    for d in (date(2026, 8, 1), date(2026, 11, 8)):
        await usage.reserve_rewrite("u", d, 10)

    r = await Housekeeping(auth, convs, usage).run(now)
    assert (r.sessions, r.import_jobs, r.usage_rows) == (2, 1, 1)
    assert set(auth._sessions) == {"active", "expired-recent"}
    assert sorted(j.graph_status for j in convs._jobs.values()) == ["completed", "pending"]
    assert list(usage._rows) == [("u", date(2026, 11, 8))]


def test_create_account_works_while_register_is_closed_and_validates() -> None:
    """가입이 닫힌 배포에서 운영자가 서버에서 계정을 만든다 (`app.tools.create_user`)."""
    import asyncio

    from app.core.errors import InvalidInputError

    client = _client(auth_allow_header_stub=False, auth_allow_register=False)
    service = client.app.state.container.auth_service  # type: ignore[attr-defined]

    user = asyncio.run(service.create_account(" operator_1 ", "long-enough-pass"))

    assert user.username == "operator_1"
    assert client.post("/v1/auth/register", json=CRED).status_code == 403
    login = client.post("/v1/auth/login", json={"username": "operator_1", "password": "long-enough-pass"})
    assert login.status_code == 200 and login.json()["username"] == "operator_1"
    with pytest.raises(InvalidInputError):
        asyncio.run(service.create_account("ok_name", "short"))
    with pytest.raises(InvalidInputError):
        asyncio.run(service.create_account("bad name!", "long-enough-pass"))
