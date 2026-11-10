"""인증·계정 확장(계약 §4.2): 가입 설정, 초대 코드, 동의, 관리자 표시, 세션 목록·전체 로그아웃, Bearer 토큰,
운영자 비밀번호 재설정. 기본 흐름(가입·로그인·CSRF·비밀번호 변경·계정 삭제)은 `test_auth.py`.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import (
    ConsentRequiredError,
    ForbiddenError,
    InvalidInputError,
    InvalidInviteError,
    NotFoundError,
    TooManyAttemptsError,
    UnauthorizedError,
    VersionConflictError,
)
from app.core.security import token_hash
from app.domain.auth import Session
from app.main import create_app
from app.repositories.auth import InMemoryAuthRepo
from app.services.auth_service import AuthService

CRED = {"username": "minsu_kim", "password": "correct horse battery"}
OTHER = {"username": "someone_else", "password": "another-long-pw"}
PAYLOAD = {"alias": "박지현", "relationship_type": "superior"}


def _settings(**overrides) -> Settings:  # noqa: ANN003
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[call-arg]


def _client(**overrides) -> TestClient:  # noqa: ANN003
    return TestClient(create_app(_settings(**overrides)))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------- 가입 설정·초대 코드·동의 ----------


def test_auth_config_reflects_settings_without_leaking_the_invite_code() -> None:
    assert _client().get("/v1/auth/config").json() == {
        "allow_register": True,
        "invite_required": False,
        "require_consent": False,
        "consent_version": "2026-11-11",
        "app_env": "development",
    }
    strict = _client(
        auth_allow_register=False,
        register_invite_code="come-in-2026",
        require_consent=True,
        consent_version="2026-12-01",
        auth_allow_header_stub=False,
    )
    r = strict.get("/v1/auth/config")  # 로그인 없이 읽는다
    assert r.status_code == 200 and "come-in-2026" not in r.text
    assert r.json() == {
        "allow_register": False,
        "invite_required": True,
        "require_consent": True,
        "consent_version": "2026-12-01",
        "app_env": "development",
    }


def test_register_requires_invite_code_when_configured() -> None:
    client = _client(register_invite_code="come-in-2026", auth_max_failures=3)
    for body in (CRED, {**CRED, "invite_code": "wrong-code"}, {**CRED, "invite_code": ""}):
        r = client.post("/v1/auth/register", json=body)
        assert r.status_code == 403 and r.json()["code"] == "invalid_invite", r.text
    # 코드를 하나씩 맞춰 보는 것을 막는다: 실패 3번 뒤에는 맞는 코드여도 잠시 막힌다 (IP 기준)
    locked = client.post("/v1/auth/register", json={**CRED, "invite_code": "come-in-2026"})
    assert locked.status_code == 429 and locked.json()["code"] == "too_many_attempts"
    assert int(locked.headers["retry-after"]) > 0

    elsewhere = TestClient(client.app, client=("203.0.113.9", 40000))
    ok = elsewhere.post("/v1/auth/register", json={**CRED, "invite_code": " come-in-2026 "})
    assert ok.status_code == 201, ok.text
    # 초대 코드가 틀리면 그 이름이 이미 있는지도 알려 주지 않는다 (409가 아니라 403)
    again = elsewhere.post("/v1/auth/register", json={**CRED, "invite_code": "guess"})
    assert again.status_code == 403 and again.json()["code"] == "invalid_invite"
    # 로그인에는 초대 코드가 필요 없다
    assert elsewhere.post("/v1/auth/login", json=CRED).status_code == 200


def test_register_consent_required_and_recorded() -> None:
    client = _client(require_consent=True)
    for body in (CRED, {**CRED, "consent": False}, {**CRED, "consent": None}):
        r = client.post("/v1/auth/register", json=body)
        assert r.status_code == 422 and r.json()["code"] == "consent_required", r.text
    r = client.post("/v1/auth/register", json={**CRED, "consent": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["consent_version"] == "2026-11-11" and body["consent_current"] is True
    assert body["consented_at"] and body["is_admin"] is False
    me = client.get("/v1/auth/me").json()
    assert (me["consented_at"], me["consent_version"], me["consent_current"]) == (
        body["consented_at"],
        "2026-11-11",
        True,
    )
    # 예전 필드도 그대로
    assert me["user_id"] == body["user_id"] and me["csrf_token"] == body["csrf_token"]


def test_consent_endpoint_records_current_version_and_notices_a_new_one() -> None:
    client = _client()
    reg = client.post("/v1/auth/register", json=CRED).json()  # 동의가 필수가 아니면 기록 없이 가입된다
    assert (reg["consented_at"], reg["consent_version"], reg["consent_current"]) == (None, None, False)

    assert TestClient(client.app).post("/v1/auth/consent", json={}).status_code == 401
    assert client.post("/v1/auth/consent", json={}).status_code == 403  # 쿠키 인증은 CSRF 필수
    h = {"X-CSRF-Token": reg["csrf_token"]}
    done = client.post("/v1/auth/consent", json={}, headers=h)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["consent_version"] == "2026-11-11" and body["consent_current"] is True
    assert body["consented_at"] and body["username"] == "minsu_kim"
    assert body["csrf_token"] == reg["csrf_token"]
    assert client.post("/v1/auth/consent", headers=h).status_code == 200  # 본문은 생략해도 된다

    # 안내 버전이 바뀌면 consent_current가 꺼지고, 다시 동의하면 새 버전이 기록된다
    client.app.state.container.settings.consent_version = "2027-01-01"
    stale = client.get("/v1/auth/me").json()
    assert (stale["consent_version"], stale["consent_current"]) == ("2026-11-11", False)
    assert client.post("/v1/auth/login", json=CRED).json()["consent_current"] is False
    h = {"X-CSRF-Token": client.get("/v1/auth/me").json()["csrf_token"]}
    renewed = client.post("/v1/auth/consent", json={}, headers=h).json()
    assert (renewed["consent_version"], renewed["consent_current"]) == ("2027-01-01", True)
    assert datetime.fromisoformat(renewed["consented_at"]) >= datetime.fromisoformat(body["consented_at"])


# ---------- 관리자 표시·이름 규칙 ----------


def test_is_admin_follows_admin_usernames_exactly() -> None:
    # 설정에 대소문자만 다르게 적으면 관리자가 아니다 (판정은 좁은 쪽으로 — Settings.is_admin_name)
    near = _client(admin_usernames=" Minsu_Kim , boss ")
    assert near.post("/v1/auth/register", json=CRED).json()["is_admin"] is False

    client = _client(admin_usernames=" minsu_kim , boss ")
    assert client.post("/v1/auth/register", json=CRED).json()["is_admin"] is True
    assert client.get("/v1/auth/me").json()["is_admin"] is True
    assert client.post("/v1/auth/login", json=CRED).json()["is_admin"] is True
    other = TestClient(client.app)
    assert other.post("/v1/auth/register", json=OTHER).json()["is_admin"] is False
    # 목록이 비면 아무도 관리자가 아니다
    assert _client().post("/v1/auth/register", json=CRED).json()["is_admin"] is False


def test_username_is_ascii_and_unique_ignoring_case() -> None:
    """관리자 여부는 소문자로 비교한다 → `Minsu_Kim`으로 가입해 `minsu_kim`의 권한을 얻을 수 없어야 한다."""
    client = _client(admin_usernames="minsu_kim")
    assert client.post("/v1/auth/register", json=CRED).status_code == 201

    def register(username: str) -> int:
        body = {"username": username, "password": "long-enough-pw"}
        return TestClient(client.app).post("/v1/auth/register", json=body).status_code

    for variant in ("Minsu_Kim", "MINSU_KIM", "minsu_KIM"):
        assert register(variant) == 409, variant
    # 영문·숫자·_·-만: 모양이 비슷한 다른 글자(전각, 켈빈 기호 K)나 한글·공백은 받지 않는다
    for bad in ("ａｄｍｉｎ", "Kim_user", "민수킴입니다", "min su", "___", "a.b.c"):
        assert register(bad) == 422, bad
    for good in ("Abc", "user-1_x", "a" * 32):
        assert register(good) == 201, good
    # 로그인은 예전처럼 대소문자를 구분한다
    wrong_case = TestClient(client.app).post("/v1/auth/login", json={**CRED, "username": "Minsu_Kim"})
    assert wrong_case.status_code == 401


# ---------- 세션 목록·전체 로그아웃 ----------


def test_sessions_list_and_logout_all() -> None:
    client = _client()
    reg = client.post("/v1/auth/register", json=CRED).json()
    h = {"X-CSRF-Token": reg["csrf_token"]}
    second = TestClient(client.app)  # 두 번째 기기
    assert second.post("/v1/auth/login", json=CRED).status_code == 200
    issued = TestClient(client.app).post("/v1/auth/token", json={**CRED, "client": "mobile"})
    token = issued.json()["access_token"]

    rows = client.get("/v1/auth/sessions").json()
    assert len(rows) == 3
    assert all(set(r) == {"id", "created_at", "expires_at", "current"} for r in rows)
    assert all(len(r["id"]) == 12 and int(r["id"], 16) >= 0 for r in rows)
    created = [datetime.fromisoformat(r["created_at"]) for r in rows]
    assert created == sorted(created, reverse=True)  # 최근 것 먼저
    mine = [r["id"] for r in rows if r["current"]]
    cookie = client.cookies.get("pi_session")
    assert mine == [token_hash(cookie)[:12]]  # 토큰 해시 앞 12자
    assert cookie not in client.get("/v1/auth/sessions").text  # 토큰 원문은 어디에도 없다
    theirs = [r["id"] for r in second.get("/v1/auth/sessions").json() if r["current"]]
    assert len(theirs) == 1 and theirs != mine  # 보는 쪽 기준으로 current가 달라진다
    assert TestClient(client.app).get("/v1/auth/sessions").status_code == 401

    assert client.post("/v1/auth/logout-all").status_code == 403  # CSRF
    out = client.post("/v1/auth/logout-all", headers=h)
    assert out.status_code == 200 and out.json() == {"revoked_sessions": 2}
    assert client.get("/v1/auth/me").status_code == 200  # 지금 세션은 남는다
    assert second.get("/v1/auth/me").status_code == 401
    assert TestClient(client.app).get("/v1/contacts", headers=_bearer(token)).status_code == 401
    assert [r["current"] for r in client.get("/v1/auth/sessions").json()] == [True]
    assert client.post("/v1/auth/logout-all", headers=h).json() == {"revoked_sessions": 0}
    # 로그아웃한 세션은 목록에서 빠진다
    again = TestClient(client.app)
    again.post("/v1/auth/login", json=CRED)
    assert len(client.get("/v1/auth/sessions").json()) == 2
    again.post("/v1/auth/logout")
    assert len(client.get("/v1/auth/sessions").json()) == 1


# ---------- Bearer 토큰 ----------


def test_bearer_token_needs_no_csrf_but_cookie_still_does() -> None:
    client = _client(auth_allow_header_stub=False)
    reg = client.post("/v1/auth/register", json=CRED).json()
    api = TestClient(client.app)  # 쿠키를 쓰지 않는 클라이언트 (모바일 키보드·PC 프로그램)

    issued = api.post("/v1/auth/token", json={**CRED, "client": "desktop"})
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert set(body) == {"access_token", "token_type", "expires_at", "user_id", "username"}
    assert (body["token_type"], body["user_id"], body["username"]) == ("bearer", reg["user_id"], "minsu_kim")
    assert "set-cookie" not in issued.headers and api.cookies.get("pi_session") is None
    assert issued.headers["cache-control"] == "no-store"  # 토큰이 든 응답은 저장되지 않는다
    assert api.post("/v1/auth/token", json={**CRED, "client": "toaster"}).status_code == 422
    assert api.post("/v1/auth/token", json=CRED).status_code == 200  # client는 생략 가능
    auth = _bearer(body["access_token"])

    # 읽기·쓰기 모두 CSRF 헤더 없이
    made = api.post("/v1/contacts", json=PAYLOAD, headers=auth)
    assert made.status_code == 201, made.text
    assert api.get("/v1/contacts", headers=auth).json()[0]["alias"] == "박지현"
    lower = {"Authorization": f"bearer {body['access_token']}"}
    assert api.get("/v1/contacts", headers=lower).status_code == 200
    # 같은 사용자의 데이터다 — 쿠키 세션에서도 보인다. 쿠키 요청은 여전히 CSRF가 필요하다
    assert client.get("/v1/contacts").json()[0]["id"] == made.json()["id"]
    assert client.post("/v1/contacts", json=PAYLOAD).status_code == 403
    with_csrf = client.post("/v1/contacts", json=PAYLOAD, headers={"X-CSRF-Token": reg["csrf_token"]})
    assert with_csrf.status_code == 201

    # 인증 경로도 전부 Bearer로 (CSRF 없이)
    assert api.get("/v1/auth/me", headers=auth).json()["username"] == "minsu_kim"
    assert api.post("/v1/auth/consent", json={}, headers=auth).json()["consent_current"] is True
    assert len(api.get("/v1/auth/sessions", headers=auth).json()) == 3  # 쿠키 1 + 토큰 2
    assert api.post("/v1/auth/logout-all", headers=auth).json() == {"revoked_sessions": 2}
    assert client.get("/v1/auth/me").status_code == 401  # 쿠키 세션이 로그아웃됐다
    new_password = {"current_password": CRED["password"], "new_password": "brand-new-password-9"}
    changed = api.post("/v1/auth/password", json=new_password, headers=auth)
    assert changed.status_code == 200 and changed.json() == {"revoked_sessions": 0}

    # 틀린 토큰·다른 방식은 401. Bearer 실패에는 WWW-Authenticate가 붙는다
    wrong = api.get("/v1/contacts", headers=_bearer("not-a-real-token"))
    assert wrong.status_code == 401 and wrong.json()["code"] == "unauthorized"
    assert wrong.headers["www-authenticate"].startswith("Bearer")
    for value in ("Bearer", "Bearer   ", "Basic abc", f"Token {body['access_token']}", "x" * 600):
        assert api.get("/v1/contacts", headers={"Authorization": value}).status_code == 401, value
    assert api.get("/v1/contacts", headers=_bearer("y" * 600)).status_code == 401

    # 로그아웃은 Bearer 토큰도 폐기한다
    assert api.post("/v1/auth/logout", headers=auth).status_code == 204
    assert api.get("/v1/contacts", headers=auth).status_code == 401
    assert api.get("/v1/auth/me", headers=auth).status_code == 401
    # 계정 삭제도 Bearer로 (비밀번호 확인은 그대로)
    relogin = api.post("/v1/auth/token", json={**CRED, "password": "brand-new-password-9"})
    fresh = _bearer(relogin.json()["access_token"])
    refused = api.request("DELETE", "/v1/auth/me", json={"password": "nope-nope-1"}, headers=fresh)
    assert refused.status_code == 401
    gone = api.request("DELETE", "/v1/auth/me", json={"password": "brand-new-password-9"}, headers=fresh)
    assert gone.status_code == 200 and gone.json()["contacts"] == 2
    assert api.get("/v1/auth/me", headers=fresh).status_code == 401


def test_bearer_wins_over_cookie_and_never_falls_back_to_it() -> None:
    """CSRF를 건너뛰는 것은 세션을 Bearer 헤더로 찾았을 때뿐이다."""
    client = _client()
    assert client.post("/v1/auth/register", json=CRED).status_code == 201  # 쿠키 = 민수
    TestClient(client.app).post("/v1/auth/register", json=OTHER)
    other_token = TestClient(client.app).post("/v1/auth/token", json=OTHER).json()["access_token"]

    # 쿠키(민수)와 Bearer(다른 사람)가 함께 오면 Bearer의 사용자로 처리한다 — 민수의 세션으로 쓰이지 않는다
    made = client.post("/v1/contacts", json=PAYLOAD, headers=_bearer(other_token))
    assert made.status_code == 201
    assert client.get("/v1/contacts").json() == []
    assert client.get("/v1/auth/me", headers=_bearer(other_token)).json()["username"] == "someone_else"
    # Bearer가 틀리면 유효한 쿠키가 있어도 401 — 쿠키로 넘어가지 않는다
    assert client.post("/v1/contacts", json=PAYLOAD, headers=_bearer("wrong-token")).status_code == 401
    assert client.get("/v1/auth/me", headers=_bearer("wrong-token")).status_code == 401
    assert client.post("/v1/auth/logout-all", headers=_bearer("wrong-token")).status_code == 401
    # 개발용 헤더 스텁은 맨 뒤다. 세션이 필요한 경로(내 정보·세션 목록·관리자 검사)는 스텁으로 통과하지 못한다
    stub = TestClient(client.app)
    assert stub.get("/v1/contacts", headers={"X-User-Id": "user-a"}).json() == []
    assert stub.get("/v1/auth/me", headers={"X-User-Id": "user-a"}).status_code == 401
    assert stub.get("/v1/auth/sessions", headers={"X-User-Id": "user-a"}).status_code == 401
    both = stub.get("/v1/contacts", headers={"X-User-Id": "user-a", **_bearer(other_token)})
    assert [c["alias"] for c in both.json()] == ["박지현"]


def test_token_endpoint_shares_login_throttle_and_hides_user_existence() -> None:
    client = _client(auth_max_failures=2)
    client.post("/v1/auth/register", json=CRED)
    api = TestClient(client.app)
    wrong = api.post("/v1/auth/token", json={**CRED, "password": "wrong-password-1"})
    unknown = api.post("/v1/auth/token", json={"username": "nobody_here", "password": "whatever-1"})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()
    # 2026-11-11 검토 뒤: 잠금은 (이름, IP) 쌍 기준이다. 위의 두 실패는 이름이 달라 아직 아무도 안 잠겼다
    # (예전에는 IP 실패 2번으로 이 IP의 모든 로그인이 막혔다 — 같은 공유기 뒤의 다른 사람까지).
    # 토큰 발급과 쿠키 로그인은 같은 기록을 쓴다: 쿠키 로그인으로 한 번 더 틀리면 두 경로가 함께 잠긴다
    assert api.post("/v1/auth/login", json={**CRED, "password": "wrong-password-2"}).status_code == 401
    blocked = api.post(
        "/v1/auth/token", json=CRED
    )  # 같은 곳에서 같은 이름을 2번 틀림 → 맞는 비밀번호여도 잠금
    assert blocked.status_code == 429 and blocked.json()["code"] == "too_many_attempts"
    assert 0 < int(blocked.headers["retry-after"]) <= 15 * 60
    assert api.post("/v1/auth/login", json=CRED).status_code == 429  # 쿠키 로그인과 같은 제한
    # 없는 이름도 똑같이 잠긴다 (잠금 여부로 가입 여부를 알 수 없다)
    assert api.post("/v1/auth/token", json={"username": "nobody_here", "password": "x"}).status_code == 401
    ghost = api.post("/v1/auth/token", json={"username": "nobody_here", "password": "whatever-1"})
    assert ghost.status_code == 429 and ghost.json()["code"] == "too_many_attempts"


def test_cookie_attributes_follow_secure_setting() -> None:
    client = _client(session_cookie_secure=True)
    issued = client.post("/v1/auth/register", json=CRED).headers["set-cookie"]
    for part in ("pi_session=", "HttpOnly", "Secure", "Path=/", "Max-Age=1209600"):
        assert part in issued, part
    cleared = client.post("/v1/auth/logout").headers["set-cookie"]
    for part in ("pi_session=", "Max-Age=0", "HttpOnly", "Secure", "Path=/"):
        assert part in cleared, part
    assert "Secure" not in _client().post("/v1/auth/register", json=CRED).headers["set-cookie"]


# ---------- 서비스 계층 ----------


@pytest.mark.anyio
async def test_login_spends_one_hash_even_when_the_user_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """없는 이름이면 해시를 건너뛰어 응답이 빨라지고, 그 차이로 가입 여부를 알 수 있었다."""
    from app.services import auth_service

    hashed_against: list[str] = []
    real = auth_service.verify_password_async

    async def counting(password: str, stored: str) -> bool:
        hashed_against.append(stored)
        return await real(password, stored)

    monkeypatch.setattr(auth_service, "verify_password_async", counting)
    service = AuthService(_settings(auth_max_failures=50), InMemoryAuthRepo())
    await service.register("minsu_kim", CRED["password"])
    for name in ("minsu_kim", "nobody_here", "nobody_else"):
        with pytest.raises(UnauthorizedError):
            await service.login(name, "wrong-password-1")
    assert len(hashed_against) == 3
    assert hashed_against[1] == hashed_against[2] != hashed_against[0]  # 없는 이름은 같은 미끼 해시와 견준다
    assert hashed_against[1].startswith("scrypt$")
    assert (await service.login("minsu_kim", CRED["password"])).user.username == "minsu_kim"


@pytest.mark.anyio
async def test_signup_enforces_policy_while_register_stays_internal() -> None:
    def service(**overrides) -> AuthService:  # noqa: ANN003
        return AuthService(_settings(**overrides), InMemoryAuthRepo())

    strict = service(register_invite_code="come-in-2026", require_consent=True)
    with pytest.raises(InvalidInviteError):
        await strict.signup("minsu_kim", CRED["password"], consent=True)
    with pytest.raises(ConsentRequiredError):
        await strict.signup("minsu_kim", CRED["password"], invite_code="come-in-2026")
    ok = await strict.signup("minsu_kim", CRED["password"], invite_code="come-in-2026", consent=True)
    assert ok.user.consent_version == "2026-11-11" and ok.user.consented_at is not None
    # 운영 스크립트(prepare_demo.py)가 부르는 안쪽 단계는 초대 코드·동의를 묻지 않는다 (시그니처도 그대로)
    demo = await strict.register("demo", "persona-demo-2026")
    assert demo.user.username == "demo" and demo.user.consented_at is None

    closed = service(auth_allow_register=False, register_invite_code="come-in-2026")
    with pytest.raises(ForbiddenError) as raised:
        await closed.signup("minsu_kim", CRED["password"], invite_code="wrong")
    assert raised.value.code == "forbidden"  # 닫혀 있으면 초대 코드가 맞는지조차 알려 주지 않는다
    with pytest.raises(ForbiddenError):
        await closed.register("minsu_kim", CRED["password"])
    admin = await closed.create_account("admin", "first-admin-password")  # 운영자는 닫혀 있어도 만든다
    assert admin.username == "admin" and closed.is_admin(admin) is False
    assert closed.prune_throttle() == 0


@pytest.mark.anyio
async def test_reset_password_revokes_every_session_and_lifts_lockout() -> None:
    service = AuthService(_settings(auth_max_failures=2), InMemoryAuthRepo())
    first = await service.register("minsu_kim", CRED["password"])
    second = await service.login("minsu_kim", CRED["password"])
    other = await service.register("someone_else", "another-long-pw")

    with pytest.raises(InvalidInputError):
        await service.reset_password("minsu_kim", "short")
    for missing in ("nobody_here", "Minsu_Kim"):  # 이름은 대소문자를 구분한다
        with pytest.raises(NotFoundError):
            await service.reset_password(missing, "long-enough-pw")
    assert await service.resolve(first.session_token) is not None  # 실패한 시도는 아무것도 바꾸지 않는다

    for _ in range(2):  # 비밀번호를 잊은 사용자가 잠길 때까지 틀린다
        with pytest.raises(UnauthorizedError):
            await service.login("minsu_kim", "forgotten-password")
    with pytest.raises(TooManyAttemptsError):
        await service.login("minsu_kim", CRED["password"])

    assert await service.reset_password(" minsu_kim ", "brand-new-password-9") == 2
    assert await service.resolve(first.session_token) is None
    assert await service.resolve(second.session_token) is None
    assert await service.resolve(other.session_token) is not None  # 다른 사용자는 그대로
    relogin = await service.login("minsu_kim", "brand-new-password-9")  # 잠금도 풀렸다
    assert relogin.user.username == "minsu_kim"
    with pytest.raises(UnauthorizedError):
        await service.login("minsu_kim", CRED["password"])


# ---------- scripts/reset_password.py ----------


def _reset_script():  # noqa: ANN202
    """`scripts/reset_password.py`를 모듈로 읽는다 (저장소 루트의 scripts는 패키지가 아니다)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "reset_password.py"
    if not path.is_file():
        pytest.skip("scripts/reset_password.py가 없는 설치입니다 (backend만 복사한 이미지).")
    spec = importlib.util.spec_from_file_location("pz_reset_password_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.anyio
async def test_reset_password_script_resets_or_creates() -> None:
    script = _reset_script()
    repo = InMemoryAuthRepo()
    service = AuthService(_settings(auth_allow_register=False), repo)

    with pytest.raises(NotFoundError):  # --create 없이는 없는 계정을 만들지 않는다
        await script.apply(service, repo, "admin", "first-admin-password")
    made = await script.apply(service, repo, "admin", "first-admin-password", create=True)
    assert "만들었습니다" in made and "first-admin-password" not in made  # 가입이 닫혀 있어도 만든다
    session = await service.login("admin", "first-admin-password")

    reset = await script.apply(service, repo, "admin", "second-admin-password", create=True)  # 있으면 재설정
    assert "바꿨습니다" in reset and "세션 1개" in reset and "second-admin-password" not in reset
    assert await service.resolve(session.session_token) is None
    assert (await service.login("admin", "second-admin-password")).user.username == "admin"
    with pytest.raises(InvalidInputError):
        await script.apply(service, repo, "admin", "short")
    with pytest.raises(VersionConflictError):  # 대소문자만 다른 계정을 새로 만들지 않는다
        await script.apply(service, repo, "ADMIN", "another-long-pw", create=True)


def test_reset_password_script_reads_password_without_echo_or_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _reset_script()
    assert (
        script.read_password(io.StringIO("piped-password-1\nsecond line is ignored\n")) == "piped-password-1"
    )
    assert script.read_password(io.StringIO("keeps inner spaces \r\n")) == "keeps inner spaces "
    with pytest.raises(script.InputError):
        script.read_password(io.StringIO("\n"))

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    typed = iter(["typed-password-1", "typed-password-1", "typed-password-1", "typo-password-2"])
    assert script.read_password(Terminal(), prompt=lambda _: next(typed)) == "typed-password-1"
    with pytest.raises(script.InputError, match="서로 다릅니다"):
        script.read_password(Terminal(), prompt=lambda _: next(typed))

    args = script.parse_args(["--username", "minsu_kim", "--create"])
    assert (args.username, args.create) == ("minsu_kim", True)
    with pytest.raises(SystemExit):  # 비밀번호를 인자로 받는 옵션은 없다 (셸 기록에 남는다)
        script.parse_args(["--username", "minsu_kim", "--password", "on-the-command-line"])
    # DATABASE_URL이 없으면 아무것도 하지 않고 2
    monkeypatch.setattr(script, "Settings", lambda: _settings())
    assert asyncio.run(script.run(script.parse_args(["--username", "minsu_kim"]))) == 2


# ---------- PostgreSQL 경로 (DB가 없으면 건너뛴다) ----------


def _pg_name() -> str:
    return f"pg_{uuid.uuid4().hex[:12]}"


def test_consent_sessions_and_bearer_on_postgres(pg_url: str) -> None:
    name, password = _pg_name(), "long-enough-pw"
    cred = {"username": name, "password": password}
    settings = _settings(database_url=pg_url, admin_usernames=name)
    with TestClient(create_app(settings)) as client:  # 한 이벤트 루프에서 (asyncpg 연결은 루프에 묶인다)
        reg = client.post("/v1/auth/register", json={**cred, "consent": True})
        assert reg.status_code == 201, reg.text
        body = reg.json()
        assert body["consent_version"] == "2026-11-11" and body["consent_current"] and body["is_admin"]
        me = client.get("/v1/auth/me").json()
        assert me["consented_at"] == body["consented_at"] and me["consent_version"] == "2026-11-11"
        h = {"X-CSRF-Token": body["csrf_token"]}
        cookie = client.cookies.get("pi_session")

        # 대소문자만 다른 이름은 같은 이름 (lower() 비교가 DB에서도 맞게 돈다)
        client.cookies.clear()
        assert client.post("/v1/auth/register", json={**cred, "username": name.upper()}).status_code == 409
        # 동의 없이 가입 → 나중에 동의
        fresh = client.post("/v1/auth/register", json={"username": _pg_name(), "password": password}).json()
        assert (fresh["consented_at"], fresh["consent_current"], fresh["is_admin"]) == (None, False, False)
        agreed = client.post("/v1/auth/consent", json={}, headers={"X-CSRF-Token": fresh["csrf_token"]})
        assert agreed.status_code == 200 and agreed.json()["consent_current"] is True
        assert client.get("/v1/auth/me").json()["consented_at"] == agreed.json()["consented_at"]

        # 토큰 발급 → Bearer로 CSRF 없이 쓰기
        client.cookies.clear()
        issued = client.post("/v1/auth/token", json={**cred, "client": "mobile"})
        assert issued.status_code == 200 and "set-cookie" not in issued.headers
        auth = _bearer(issued.json()["access_token"])
        assert client.post("/v1/contacts", json=PAYLOAD, headers=auth).status_code == 201
        rows = client.get("/v1/auth/sessions", headers=auth).json()
        assert len(rows) == 2 and [r["current"] for r in rows] == [True, False]  # 최근(토큰) 먼저

        # 쿠키 세션으로 돌아와 전체 로그아웃 → 토큰이 죽는다
        client.cookies.set("pi_session", cookie)
        assert client.get("/v1/contacts").json()[0]["alias"] == "박지현"
        assert client.post("/v1/auth/logout-all", headers=h).json() == {"revoked_sessions": 1}
        assert [r["current"] for r in client.get("/v1/auth/sessions").json()] == [True]
        client.cookies.clear()
        assert client.get("/v1/contacts", headers=auth).status_code == 401
        # 로그아웃이 Bearer 토큰을 폐기한다
        again = _bearer(client.post("/v1/auth/token", json=cred).json()["access_token"])
        assert client.post("/v1/auth/logout", headers=again).status_code == 204
        assert client.get("/v1/auth/me", headers=again).status_code == 401


def test_auth_repo_new_methods_on_postgres(pg_url: str) -> None:
    from app.db.session import create_engine, create_session_factory
    from app.repositories.auth import PostgresAuthRepo

    async def _run() -> None:
        engine = create_engine(pg_url)
        repo = PostgresAuthRepo(create_session_factory(engine))
        try:
            now = datetime.now(UTC).replace(microsecond=0)
            name = _pg_name()
            user = await repo.create_user(name, "x", consented_at=now, consent_version="v1")
            assert (user.consented_at, user.consent_version) == (now, "v1")
            assert (await repo.get_user(user.id)).consented_at == now
            found = await repo.find_user_by_username(name)
            assert found is not None and found[0].consent_version == "v1"
            assert await repo.find_user_by_username(name.upper()) is None  # 로그인 조회는 대소문자 구분
            assert await repo.username_exists(name.upper())
            assert not await repo.username_exists(name + "x")

            later = now + timedelta(hours=1)
            updated = await repo.record_consent(user.id, "v2", later)
            assert (updated.consented_at, updated.consent_version) == (later, "v2")
            assert (await repo.get_user(user.id)).consent_version == "v2"
            with pytest.raises(NotFoundError):
                await repo.record_consent(uuid.uuid4(), "v2", later)
            plain = await repo.create_user(_pg_name(), "x")
            assert (plain.consented_at, plain.consent_version) == (None, None)

            def mk(owner_id: uuid.UUID, key: str, created: datetime, expires: datetime) -> Session:
                return Session(
                    token_hash=f"{key}-{uuid.uuid4().hex}",
                    user_id=owner_id,
                    csrf_token="c",
                    created_at=created,
                    expires_at=expires,
                )

            day = timedelta(days=1)
            old = await repo.create_session(mk(user.id, "old", now - 2 * day, now + day))
            new = await repo.create_session(mk(user.id, "new", now - timedelta(hours=1), now + day))
            await repo.create_session(mk(user.id, "expired", now - 30 * day, now - day))
            revoked = await repo.create_session(mk(user.id, "rev", now - day, now + day))
            await repo.revoke_session(revoked.token_hash, now)
            theirs = await repo.create_session(mk(plain.id, "theirs", now, now + day))

            listed = await repo.list_active_sessions(user.id, now)
            assert [s.token_hash for s in listed] == [new.token_hash, old.token_hash]  # 활성만, 최근 먼저
            assert await repo.revoke_all_sessions(user.id, now) == 3  # 활성 2 + 만료됐지만 폐기 전이던 1
            assert await repo.list_active_sessions(user.id, now) == []
            assert await repo.revoke_all_sessions(user.id, now) == 0
            remaining = await repo.list_active_sessions(plain.id, now)
            assert [s.token_hash for s in remaining] == [theirs.token_hash]
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_reset_password_script_end_to_end_on_postgres(
    pg_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.db.session import create_engine, create_session_factory
    from app.repositories.auth import PostgresAuthRepo

    script = _reset_script()
    settings = _settings(database_url=pg_url)
    monkeypatch.setattr(script, "Settings", lambda: settings)
    name = _pg_name()

    def run(password: str, *flags: str) -> tuple[int, str]:
        monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
        code = asyncio.run(script.run(script.parse_args(["--username", name, *flags])))
        return code, capsys.readouterr().out

    code, out = run("first-password-1")
    assert code == 1 and "실패" in out  # 없는 사용자
    code, out = run("first-password-1", "--create")
    assert code == 0 and "만들었습니다" in out and "first-password-1" not in out
    code, out = run("short")
    assert code == 1 and "8자" in out
    code, out = run("")
    assert code == 2 and "비어" in out

    async def _login_then_reset() -> None:
        engine = create_engine(pg_url)
        service = AuthService(settings, PostgresAuthRepo(create_session_factory(engine)))
        try:
            session = await service.login(name, "first-password-1")
            assert await service.reset_password(name, "second-password-2") == 1
            assert await service.resolve(session.session_token) is None
            with pytest.raises(UnauthorizedError):
                await service.login(name, "first-password-1")
        finally:
            await engine.dispose()

    asyncio.run(_login_then_reset())
    code, out = run("third-password-3")
    assert code == 0 and "바꿨습니다" in out and "세션 0개" in out


# ---------- 관리자 이름: 판정은 정확히 일치, 운영에서는 공개 가입으로 선점 불가 ----------


def test_admin_flag_requires_the_exact_configured_name() -> None:
    settings = _settings(admin_usernames="boss, Ops_Lead")
    assert settings.is_admin_name("boss") and settings.is_admin_name("Ops_Lead")
    assert not settings.is_admin_name("Boss") and not settings.is_admin_name("ops_lead")
    assert not settings.is_admin_name("") and not _settings().is_admin_name("boss")


def test_admin_names_cannot_be_taken_by_public_signup_in_production() -> None:
    password = "long-enough-pw"
    prod = AuthService(_settings(app_env="production", admin_usernames="boss"), InMemoryAuthRepo())
    for name in ("boss", "Boss", " BOSS "):
        with pytest.raises(VersionConflictError):  # 이미 있는 이름과 구분되지 않는 응답
            asyncio.run(prod.signup(name, password))
    assert asyncio.run(prod.signup("minsu_kim", password)).user.username == "minsu_kim"
    # 운영자가 서버에서 만드는 길(scripts/reset_password.py --create)은 열려 있다
    assert prod.is_admin(asyncio.run(prod.create_account("boss", password)))

    # 개발 모드에서는 화면에서 바로 만들어 쓸 수 있다
    dev = AuthService(_settings(admin_usernames="boss"), InMemoryAuthRepo())
    assert dev.is_admin(asyncio.run(dev.signup("boss", password)).user)
