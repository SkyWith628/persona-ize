"""회원 상태·소셜 로그인: 이메일 가입, 승인 대기·정지, OAuth(가짜 제공자), 계정 연결, 회원관리.

대부분 인메모리 저장소로 돈다. 관리자 API(`/v1/admin/members`)는 PostgreSQL에서만 열리므로 맨 아래
통합 테스트(`pg_url`)가 맡는다 — 같은 테스트가 0012의 SQL 경로(이메일 검색·소셜 연결·상태 변경)도 지난다.
"""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import safe_next, with_auth_error
from app.core.config import Settings
from app.core.errors import InvalidInputError, NotFoundError
from app.core.guard import inspect_production_settings
from app.main import create_app
from app.services import oauth
from app.services.auth_service import normalize_email, username_seed
from app.services.member_admin_service import MemberAdminService
from app.services.oauth import GoogleProvider, KakaoProvider, OAuthError, OAuthProfile, pkce_challenge

PW = "correct horse battery"


def _client(**overrides: Any) -> TestClient:
    values = {"llm_provider": "fake", "embedding_provider": "fake", **overrides}
    return TestClient(create_app(Settings(_env_file=None, **values)))  # type: ignore[call-arg]


def _state(client: TestClient):  # noqa: ANN202
    return client.app.state.container  # type: ignore[attr-defined]


def _code(response: httpx.Response) -> str | None:
    return response.json().get("code")


def _signup(client: TestClient, email: str, **extra: Any) -> httpx.Response:
    return client.post("/v1/auth/signup", json={"email": email, "password": PW, **extra})


def _set_status(client: TestClient, user_id: str, status: str) -> None:
    asyncio.run(_state(client).auth.update_user(UUID(user_id), status=status))


class FakeProvider:
    """네트워크 없는 제공자. 받은 인자를 기록하고 정해 둔 프로필을 돌려준다."""

    def __init__(self, name: str, profile: OAuthProfile, *, uses_pkce: bool) -> None:
        self.name = name
        self.uses_pkce = uses_pkce
        self.profile = profile
        self.calls: list[dict[str, str | None]] = []
        self.challenge: str | None = None

    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str | None) -> str:
        self.challenge = code_challenge
        return f"https://provider.test/{self.name}/authorize?state={state}"

    async def fetch_profile(self, *, code: str, redirect_uri: str, code_verifier: str | None) -> OAuthProfile:
        self.calls.append({"code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier})
        if code == "bad-code":
            raise OAuthError("토큰 교환에 실패했습니다.")
        return self.profile


def _fake(client: TestClient, name: str, profile: OAuthProfile) -> FakeProvider:
    provider = FakeProvider(name, profile, uses_pkce=name == "google")
    _state(client).oauth_providers[name] = provider
    return provider


def _start(client: TestClient, provider: str, next_path: str | None = None) -> tuple[httpx.Response, str]:
    params = {"next": next_path} if next_path is not None else None
    r = client.get(f"/v1/auth/oauth/{provider}/start", params=params, follow_redirects=False)
    assert r.status_code == 302, r.text
    return r, parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def _social_login(client: TestClient, provider: str, next_path: str | None = None) -> httpx.Response:
    _, state = _start(client, provider, next_path)
    return client.get(
        f"/v1/auth/oauth/{provider}/callback",
        params={"code": "good-code", "state": state},
        follow_redirects=False,
    )


GOOGLE_MINSU = OAuthProfile(subject="g-1", email="minsu@example.com", email_verified=True, name="김민수")


# ---------- 순수 함수 ----------


def test_email_and_username_seed_rules() -> None:
    assert normalize_email("  Minsu@Example.COM ") == "minsu@example.com"
    for bad in ("", "no-at", "a@b", "a b@c.d", "x" * 250 + "@a.bc"):
        with pytest.raises(InvalidInputError):
            normalize_email(bad)
    assert username_seed("Minsu.Kim+tag") == "minsu_kim_tag"
    assert username_seed("김") == "user" and username_seed("ab") == "user"
    assert len(username_seed("a" * 80)) == 24


def test_safe_next_allows_only_same_origin_paths() -> None:
    assert safe_next("/app#/compose") == "/app#/compose"
    for bad in (None, "", "https://evil.test/", "//evil.test", "/\\evil", "/a\nb", "x", "/" + "a" * 600):
        assert safe_next(bad) == "/"
    assert with_auth_error("/", "oauth_failed") == "/?auth_error=oauth_failed"
    assert with_auth_error("/app?x=1#/login", "signup_closed") == "/app?x=1&auth_error=signup_closed#/login"


def test_production_guard_wants_a_public_https_address_for_social_login() -> None:
    def report(**overrides: Any):  # noqa: ANN202
        values = {"llm_provider": "fake", "embedding_provider": "fake", **overrides}
        return inspect_production_settings(Settings(_env_file=None, **values))  # type: ignore[call-arg]

    base = report()
    for bad in ("", "http://app.example.com", "https://app.example.com/path"):
        got = report(kakao_client_id="kid", public_base_url=bad)
        assert len(got.errors) == len(base.errors) + 1 and "PUBLIC_BASE_URL" in got.errors[-1], bad
    ok = report(google_client_id="gid", google_client_secret="s", public_base_url="https://app.example.com/")
    assert ok.errors == base.errors
    # 승인제 여부가 가입 경고 문장에 드러난다
    assert any("승인제라" in w for w in report(auth_signup_requires_approval=True).warnings)
    assert any("GOOGLE_CLIENT_SECRET" in w for w in report(google_client_id="gid").warnings)


# ---------- 이메일 가입·로그인 ----------


def test_config_reports_approval_and_configured_providers() -> None:
    plain = _client().get("/v1/auth/config").json()
    assert (plain["requires_approval"], plain["google"], plain["kakao"]) == (False, False, False)
    on = _client(
        auth_signup_requires_approval=True,
        google_client_id="gid",
        google_client_secret="gsecret",
        kakao_client_id="kid",
    )
    body = on.get("/v1/auth/config").json()
    assert (body["requires_approval"], body["google"], body["kakao"]) == (True, True, True)
    assert "gsecret" not in json.dumps(body)
    # Google은 시크릿까지 있어야 켜진다
    assert _client(google_client_id="gid").get("/v1/auth/config").json()["google"] is False


def test_email_signup_creates_active_user_who_logs_in_by_email_or_username() -> None:
    client = _client()
    r = _signup(client, " Minsu.Kim@Example.com ", display_name="  김   민수 ")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["username"] == "minsu_kim" and body["email"] == "minsu.kim@example.com"
    assert (body["display_name"], body["role"], body["status"]) == ("김 민수", "user", "active")
    assert client.get("/v1/contacts").status_code == 200

    for name in ("minsu.kim@example.com", "MINSU.KIM@example.com", "minsu_kim"):
        fresh = _client_sharing(client)
        r = fresh.post("/v1/auth/login", json={"username": name, "password": PW})
        assert r.status_code == 200, (name, r.text)
        assert r.json()["user_id"] == body["user_id"]
    # 토큰 발급도 이메일로 된다
    r = _client_sharing(client).post(
        "/v1/auth/token", json={"username": "minsu.kim@example.com", "password": PW}
    )
    assert r.status_code == 200 and r.json()["username"] == "minsu_kim"

    assert _signup(client, "minsu.kim@example.com").status_code == 409
    # 같은 앞부분의 다른 이메일은 꼬리가 붙은 아이디를 받는다
    other = _signup(client, "minsu.kim@other.example").json()
    assert other["username"].startswith("minsu_kim_") and other["username"] != "minsu_kim"


def _client_sharing(client: TestClient) -> TestClient:
    """같은 앱(같은 저장소)을 쓰는 쿠키 없는 새 클라이언트."""
    return TestClient(client.app)


def test_email_signup_validation_policy_and_reserved_names() -> None:
    client = _client(admin_usernames="boss")
    assert _signup(client, "not-an-email").status_code == 422
    assert client.post("/v1/auth/signup", json={"email": "a@b.co", "password": "short"}).status_code == 422
    # 이메일 앞부분이 관리자 이름이어도 그 아이디를 받지 않는다 (대소문자 무시)
    got = _signup(client, "Boss@example.com").json()
    assert got["username"] != "boss" and got["username"].startswith("boss_") and got["is_admin"] is False

    closed = _client(auth_allow_register=False)
    r = _signup(closed, "a@example.com")
    assert (r.status_code, _code(r)) == (403, "signup_closed")

    # 초대 코드·동의는 아이디 가입과 같은 규칙이다 — 이메일 가입으로 건너뛸 수 없다
    gated = _client(register_invite_code="come-in", require_consent=True)
    r = _signup(gated, "a@example.com")
    assert (r.status_code, _code(r)) == (403, "invalid_invite")
    r = _signup(gated, "a@example.com", invite_code="come-in")
    assert (r.status_code, _code(r)) == (422, "consent_required")
    r = _signup(gated, "a@example.com", invite_code="come-in", consent=True)
    assert r.status_code == 201 and r.json()["consent_current"] is True


# ---------- 승인 대기·정지 ----------


def test_pending_user_is_blocked_outside_auth_until_approved() -> None:
    client = _client(auth_signup_requires_approval=True, auth_allow_header_stub=False)
    body = _signup(client, "wait@example.com").json()
    assert body["status"] == "pending"
    csrf = {"X-CSRF-Token": body["csrf_token"]}

    r = client.get("/v1/contacts")
    assert (r.status_code, _code(r)) == (403, "account_pending")
    assert (
        client.post(
            "/v1/contacts", json={"alias": "팀장", "relationship_type": "boss"}, headers=csrf
        ).status_code
        == 403
    )
    # /v1/auth/*는 열려 있다 — 화면이 상태를 읽어 안내한다
    me = client.get("/v1/auth/me")
    assert me.status_code == 200 and me.json()["status"] == "pending"
    # Bearer 토큰도 같은 문에서 막힌다
    token = (
        _client_sharing(client)
        .post("/v1/auth/token", json={"username": "wait@example.com", "password": PW})
        .json()["access_token"]
    )
    r = _client_sharing(client).get("/v1/contacts", headers={"Authorization": f"Bearer {token}"})
    assert (r.status_code, _code(r)) == (403, "account_pending")

    # 아이디 가입(/register)도 같은 정책이다
    r = _client_sharing(client).post("/v1/auth/register", json={"username": "by_name", "password": PW})
    assert r.status_code == 201 and r.json()["status"] == "pending"

    _set_status(client, body["user_id"], "active")
    assert client.get("/v1/contacts").status_code == 200


def test_suspended_user_cannot_login_or_use_an_old_session() -> None:
    client = _client(auth_allow_header_stub=False)
    body = _signup(client, "gone@example.com").json()
    _set_status(client, body["user_id"], "suspended")

    r = client.get("/v1/contacts")  # 세션을 폐기하지 않았어도 막힌다
    assert (r.status_code, _code(r)) == (403, "account_suspended")
    fresh = _client_sharing(client)
    r = fresh.post("/v1/auth/login", json={"username": "gone@example.com", "password": PW})
    assert (r.status_code, _code(r)) == (403, "account_suspended")
    # 비밀번호가 틀리면 정지 여부를 알려 주지 않는다
    r = fresh.post("/v1/auth/login", json={"username": "gone@example.com", "password": PW + "x"})
    assert r.status_code == 401


# ---------- 소셜 로그인 ----------


def test_oauth_start_redirects_with_state_cookie_and_pkce() -> None:
    client = _client()
    google = _fake(client, "google", GOOGLE_MINSU)
    r, state = _start(client, "google", "/app#/compose")
    assert r.headers["location"].startswith("https://provider.test/google/authorize")
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("pi_oauth=") and "HttpOnly" in cookie and "Path=/v1/auth/oauth" in cookie
    assert "Max-Age=600" in cookie and state not in ("", None)

    done = client.get(
        "/v1/auth/oauth/google/callback", params={"code": "good-code", "state": state}, follow_redirects=False
    )
    assert done.status_code == 302 and done.headers["location"] == "/app#/compose"
    call = google.calls[0]
    assert call["redirect_uri"] == "http://testserver/v1/auth/oauth/google/callback"
    assert call["code_verifier"] and pkce_challenge(call["code_verifier"]) == google.challenge

    # 카카오는 PKCE를 쓰지 않는다. 설정되지 않은 제공자는 404
    kakao = _fake(client, "kakao", OAuthProfile(subject="k-1"))
    _social_login(client, "kakao")
    assert kakao.challenge is None and kakao.calls[0]["code_verifier"] is None
    assert client.get("/v1/auth/oauth/naver/start", follow_redirects=False).status_code == 404

    # PUBLIC_BASE_URL이 있으면 그 주소로 redirect_uri를 만든다
    fixed = _client(public_base_url="https://app.example.com/")
    g2 = _fake(fixed, "google", GOOGLE_MINSU)
    _social_login(fixed, "google")
    assert g2.calls[0]["redirect_uri"] == "https://app.example.com/v1/auth/oauth/google/callback"


def test_oauth_callback_creates_a_social_only_account_and_opens_a_session() -> None:
    client = _client(auth_allow_header_stub=False)
    _fake(client, "google", GOOGLE_MINSU)
    r = _social_login(client, "google")
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert "pi_session=" in r.headers["set-cookie"] and "code=" not in r.headers["location"]

    me = client.get("/v1/auth/me").json()
    assert (me["username"], me["email"], me["display_name"]) == ("minsu", "minsu@example.com", "김민수")
    assert me["providers"] == ["google"] and me["status"] == "active"
    assert client.get("/v1/contacts").status_code == 200
    # 다시 로그인하면 같은 계정이다
    again = _client_sharing(client)
    _fake(again, "google", GOOGLE_MINSU)
    _social_login(again, "google")
    assert again.get("/v1/auth/me").json()["user_id"] == me["user_id"]

    # 비밀번호가 없으므로 비밀번호 로그인·비밀번호 변경은 안 된다
    r = _client_sharing(client).post("/v1/auth/login", json={"username": "minsu@example.com", "password": PW})
    assert r.status_code == 401
    csrf = {"X-CSRF-Token": me["csrf_token"]}
    r = client.post(
        "/v1/auth/password", json={"current_password": PW, "new_password": PW + "!"}, headers=csrf
    )
    assert r.status_code == 401
    # 계정 삭제는 빈 비밀번호로 (세션 + CSRF)
    r = client.request("DELETE", "/v1/auth/me", json={"password": ""}, headers=csrf)
    assert r.status_code == 200, r.text
    assert client.get("/v1/auth/me").status_code == 401


def test_password_account_still_needs_its_password_to_delete() -> None:
    client = _client()
    body = _signup(client, "keep@example.com").json()
    csrf = {"X-CSRF-Token": body["csrf_token"]}
    assert client.request("DELETE", "/v1/auth/me", json={"password": ""}, headers=csrf).status_code == 401
    assert client.request("DELETE", "/v1/auth/me", json={"password": PW}, headers=csrf).status_code == 200


def test_oauth_callback_rejects_bad_state_missing_cookie_denial_and_provider_failure() -> None:
    client = _client()
    _fake(client, "google", GOOGLE_MINSU)

    def callback(c: TestClient, **params: str) -> httpx.Response:
        return c.get("/v1/auth/oauth/google/callback", params=params, follow_redirects=False)

    # 상태 쿠키 없이 온 콜백 (다른 브라우저·만료)
    r = callback(_client_sharing(client), code="good-code", state="whatever")
    assert r.headers["location"] == "/?auth_error=oauth_failed" and "pi_session" not in r.headers.get(
        "set-cookie", ""
    )

    _, state = _start(client, "google", "/app")
    r = callback(client, code="good-code", state=state + "x")
    assert r.headers["location"] == "/app?auth_error=oauth_failed"
    # 실패하면 상태 쿠키를 지운다 — 같은 state로 다시 올 수 없다
    assert callback(client, code="good-code", state=state).headers["location"] == "/?auth_error=oauth_failed"

    _, state = _start(client, "google", "/app")
    assert (
        callback(client, error="access_denied", state=state).headers["location"]
        == "/app?auth_error=oauth_denied"
    )
    _, state = _start(client, "google")
    assert callback(client, state=state).headers["location"] == "/?auth_error=oauth_failed"
    _, state = _start(client, "google")
    assert callback(client, code="bad-code", state=state).headers["location"] == "/?auth_error=oauth_failed"
    assert client.get("/v1/auth/me").status_code == 401


def test_oauth_never_links_to_a_password_account_or_by_unverified_email() -> None:
    client = _client()
    owner = _signup(client, "minsu@example.com").json()

    # 확인되지 않은 같은 이메일: 기존 계정에 붙지 않고, 이메일 없는 새 계정이 된다
    stranger = _client_sharing(client)
    _fake(stranger, "kakao", OAuthProfile(subject="k-9", email="minsu@example.com", email_verified=False))
    _social_login(stranger, "kakao")
    other = stranger.get("/v1/auth/me").json()
    assert other["user_id"] != owner["user_id"] and other["email"] is None
    assert other["username"] == "kakao" and other["providers"] == ["kakao"]

    # 확인된 같은 이메일이어도 **비밀번호로 만든 계정**에는 붙이지 않는다 — 이메일 가입은 주소 소유를 확인하지
    # 않으므로, 남의 주소로 먼저 가입해 둔 사람이 주인의 소셜 로그인을 자기 계정으로 끌어올 수 있다
    victim = _client_sharing(client)
    _fake(victim, "google", GOOGLE_MINSU)
    r = _social_login(victim, "google", "/app")
    assert r.headers["location"] == "/app?auth_error=account_exists"
    assert (
        "pi_session" not in r.headers.get("set-cookie", "") and victim.get("/v1/auth/me").status_code == 401
    )
    assert client.get("/v1/auth/me").json()["providers"] == ["password"]  # 먼저 가입한 계정에 연결되지 않았다


def test_oauth_links_a_second_provider_to_a_social_only_account_by_verified_email() -> None:
    client = _client()
    _fake(client, "google", GOOGLE_MINSU)
    _social_login(client, "google")
    first = client.get("/v1/auth/me").json()
    # 다른 제공자가 같은 이메일을 확인해 주면 소셜 전용 계정에는 연결한다 (양쪽 모두 확인된 이메일)
    again = _client_sharing(client)
    kakao = OAuthProfile(subject="k-1", email="minsu@example.com", email_verified=True)
    _fake(again, "kakao", kakao)
    assert _social_login(again, "kakao").headers["location"] == "/"
    me = again.get("/v1/auth/me").json()
    assert me["user_id"] == first["user_id"] and me["providers"] == ["google", "kakao"]


def test_login_failures_are_counted_per_account_not_per_typed_name() -> None:
    client = _client(auth_max_failures=3, auth_ip_failure_factor=0, auth_name_failure_factor=0)
    assert _signup(client, "minsu@example.com").json()["username"] == "minsu"
    fresh = _client_sharing(client)

    def attempt(name: str, password: str = "wrong-password") -> int:
        return fresh.post("/v1/auth/login", json={"username": name, "password": password}).status_code

    # 아이디와 이메일을 번갈아 틀려도 같은 계정의 실패로 센다 (한도가 두 배가 되지 않는다)
    assert [attempt("minsu"), attempt("minsu@example.com"), attempt("MINSU@example.com")] == [401, 401, 401]
    assert attempt("minsu") == 429 and attempt("minsu@example.com", PW) == 429


def test_signup_is_rate_limited_like_register() -> None:
    from app.core.ratelimit import AUTH_LIMITED

    assert ("POST", "/v1/auth/signup") in AUTH_LIMITED and ("POST", "/v1/auth/register") in AUTH_LIMITED


def test_social_account_must_consent_before_using_the_api_when_consent_is_required() -> None:
    client = _client(require_consent=True, auth_allow_header_stub=False)
    _fake(client, "google", GOOGLE_MINSU)
    _social_login(client, "google")  # 소셜 가입은 가입 때 동의를 받지 않는다
    me = client.get("/v1/auth/me").json()
    assert me["consented_at"] is None and me["consent_current"] is False
    r = client.get("/v1/contacts")
    assert (r.status_code, _code(r)) == (403, "consent_required")
    done = client.post("/v1/auth/consent", json={}, headers={"X-CSRF-Token": me["csrf_token"]})
    # 동의 응답에도 로그인 수단이 실린다 (빠지면 화면이 비밀번호 계정을 소셜 전용으로 오해한다)
    assert done.status_code == 200 and done.json()["providers"] == ["google"]
    assert client.get("/v1/contacts").status_code == 200
    # 동의를 요구하지 않는 서버에서는 막지 않는다
    plain = _client(auth_allow_header_stub=False)
    _fake(plain, "google", GOOGLE_MINSU)
    _social_login(plain, "google")
    assert plain.get("/v1/contacts").status_code == 200


def test_consent_response_keeps_password_in_providers() -> None:
    client = _client(require_consent=True)
    body = client.post(
        "/v1/auth/register", json={"username": "minsu", "password": PW, "consent": True}
    ).json()
    done = client.post("/v1/auth/consent", json={}, headers={"X-CSRF-Token": body["csrf_token"]})
    assert done.json()["providers"] == ["password"]


def test_token_response_that_is_not_an_object_is_an_oauth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_http(monkeypatch, lambda _: httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(OAuthError):
        asyncio.run(GoogleProvider("gid", "s").fetch_profile(code="c", redirect_uri="r", code_verifier=None))


def test_new_social_accounts_follow_signup_policy_but_existing_ones_still_log_in() -> None:
    # 가입 닫힘: 새 소셜 계정은 못 만들지만 이미 연결된 계정·연결할 수 있는 계정은 로그인된다
    client = _client()
    _fake(client, "google", GOOGLE_MINSU)
    _social_login(client, "google")
    _state(client).settings.auth_allow_register = False
    known = _client_sharing(client)
    assert _social_login(known, "google").headers["location"] == "/"
    _fake(client, "kakao", OAuthProfile(subject="k-new"))
    r = _social_login(_client_sharing(client), "kakao", "/app")
    assert r.headers["location"] == "/app?auth_error=signup_closed"

    # 초대 코드 서버: 승인제가 아니면 소셜 신규 가입은 닫힌다 (초대 코드를 건너뛰는 길이 되므로)
    invite = _client(register_invite_code="come-in")
    _fake(invite, "google", GOOGLE_MINSU)
    assert _social_login(invite, "google").headers["location"] == "/?auth_error=signup_closed"
    # 승인제면 pending으로 받는다
    gated = _client(register_invite_code="come-in", auth_signup_requires_approval=True)
    _fake(gated, "google", GOOGLE_MINSU)
    assert _social_login(gated, "google").headers["location"] == "/"
    assert gated.get("/v1/auth/me").json()["status"] == "pending"
    assert _code(gated.get("/v1/contacts")) == "account_pending"


def test_suspended_social_user_is_sent_back_with_account_suspended() -> None:
    client = _client()
    _fake(client, "google", GOOGLE_MINSU)
    _social_login(client, "google")
    _set_status(client, client.get("/v1/auth/me").json()["user_id"], "suspended")
    r = _social_login(_client_sharing(client), "google", "/app")
    assert r.headers["location"] == "/app?auth_error=account_suspended"
    assert "pi_session" not in r.headers.get("set-cookie", "")


# ---------- 회원관리 서비스 (인메모리) ----------


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.down = False

    async def __call__(self, action: str, detail: str) -> None:
        if self.down:
            raise RuntimeError("log store down")
        self.lines.append((action, detail))


def test_member_service_approves_suspends_promotes_and_deletes() -> None:
    client = _client(auth_signup_requires_approval=True, admin_usernames="boss")
    state = _state(client)
    log = _Log()
    service = MemberAdminService(state.auth, state.auth_service, state.deletion_service, log)

    async def scenario() -> None:
        boss = await state.auth_service.create_account("boss", PW)
        waiting = (await state.auth_service.signup_email("wait@example.com", PW, "대기")).user
        session = await state.auth_service.resolve(
            (await state.auth_service.login("wait@example.com", PW)).session_token
        )
        assert session is not None

        total, views = await service.list_members(query="", status="pending", limit=50, offset=0)
        assert total == 1 and views[0].user.email == "wait@example.com" and views[0].providers == ["password"]
        total, _ = await service.list_members(query="EXAMPLE.com", status=None, limit=50, offset=0)
        assert total == 1
        assert (await service.list_members(query="100%", status=None, limit=50, offset=0))[0] == 0
        # 검색어 자체는 기록에 남기지 않는다
        assert all("example" not in detail.lower() for _, detail in log.lines)

        approved = await service.update_member(boss.id, waiting.id, status="active", role=None)
        assert approved.user.status == "active" and approved.is_admin is False
        promoted = await service.update_member(boss.id, waiting.id, status=None, role="admin")
        assert promoted.is_admin is True  # 역할로도 관리자가 된다

        suspended = await service.update_member(boss.id, waiting.id, status="suspended", role=None)
        assert suspended.user.status == "suspended"
        assert await state.auth.list_active_sessions(waiting.id, datetime.now(UTC)) == []  # 세션 폐기

        for kwargs in ({"status": "suspended", "role": None}, {"status": None, "role": "user"}):
            with pytest.raises(InvalidInputError):
                await service.update_member(boss.id, boss.id, **kwargs)
        with pytest.raises(InvalidInputError):
            await service.update_member(boss.id, waiting.id, status=None, role=None)
        with pytest.raises(InvalidInputError):
            await service.update_member(boss.id, waiting.id, status="pending", role=None)
        with pytest.raises(NotFoundError):
            await service.update_member(boss.id, uuid4(), status="active", role=None)
        with pytest.raises(InvalidInputError):
            await service.delete_member(boss.id, boss.id)

        # 기록을 못 남기면 아무것도 바꾸지 않는다
        log.down = True
        with pytest.raises(RuntimeError):
            await service.delete_member(boss.id, waiting.id)
        assert (await state.auth.get_user(waiting.id)).username == waiting.username
        log.down = False

        await service.update_member(boss.id, waiting.id, status="active", role=None)
        await state.auth_service.login("wait@example.com", PW)
        assert await service.revoke_sessions(waiting.id) == 1
        deleted = await service.delete_member(boss.id, waiting.id)
        assert deleted.user_id == str(waiting.id)
        with pytest.raises(NotFoundError):
            await state.auth.get_user(waiting.id)

    asyncio.run(scenario())
    actions = [action for action, _ in log.lines]
    assert actions.count("member_update") == 4 and actions[-2:] == ["member_revoke_sessions", "member_delete"]
    assert any(a == "member_delete" and d.startswith("target=wait id=u_") for a, d in log.lines)


def test_configured_admin_cannot_be_suspended_demoted_or_deleted_from_the_screen() -> None:
    client = _client(admin_usernames="boss")
    state = _state(client)
    log = _Log()
    service = MemberAdminService(state.auth, state.auth_service, state.deletion_service, log)

    async def scenario() -> None:
        boss = await state.auth_service.create_account("boss", PW)
        other = await state.auth_service.create_account("helper", PW)
        await state.auth.update_user(other.id, role="admin")  # role로 관리자가 된 사람
        for kwargs in ({"status": "suspended", "role": None}, {"status": None, "role": "user"}):
            with pytest.raises(InvalidInputError):
                await service.update_member(other.id, boss.id, **kwargs)
        with pytest.raises(InvalidInputError):
            await service.delete_member(other.id, boss.id)
        assert (await state.auth.get_user(boss.id)).status == "active"
        # 거부한 요청은 기록에도 남기지 않는다 (바꾼 것이 없다)
        assert log.lines == []
        # 설정 관리자는 role 관리자를 내릴 수 있다
        demoted = await service.update_member(boss.id, other.id, status=None, role="user")
        assert demoted.is_admin is False

    asyncio.run(scenario())


# ---------- 제공자 HTTP (모의 전송) ----------
def _mock_http(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # noqa: ANN001
    real = httpx.AsyncClient
    monkeypatch.setattr(
        oauth.httpx, "AsyncClient", functools.partial(real, transport=httpx.MockTransport(handler))
    )


@pytest.mark.anyio
async def test_google_provider_exchanges_code_and_reads_userinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": "at-123", "id_token": "x"})
        assert str(request.url) == "https://openidconnect.googleapis.com/v1/userinfo"
        return httpx.Response(
            200, json={"sub": "1098", "email": "Minsu@Example.com", "email_verified": True, "name": "김 민수"}
        )

    _mock_http(monkeypatch, handler)
    profile = await GoogleProvider("gid", "gsecret").fetch_profile(
        code="c0de", redirect_uri="https://p.example/cb", code_verifier="verifier"
    )
    assert profile == OAuthProfile(
        subject="1098", email="minsu@example.com", email_verified=True, name="김 민수"
    )
    form = parse_qs(seen[0].content.decode())
    assert {k: v[0] for k, v in form.items()} == {
        "grant_type": "authorization_code",
        "code": "c0de",
        "client_id": "gid",
        "client_secret": "gsecret",
        "redirect_uri": "https://p.example/cb",
        "code_verifier": "verifier",
    }
    assert seen[0].method == "POST" and seen[1].headers["authorization"] == "Bearer at-123"

    # email_verified가 문자열 "true"처럼 와도 참으로 보지 않는다. 토큰 교환 실패는 OAuthError.
    def unverified(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"sub": "1", "email": "a@example.com", "email_verified": "true"})

    _mock_http(monkeypatch, unverified)
    got = await GoogleProvider("gid", "s").fetch_profile(code="c", redirect_uri="r", code_verifier=None)
    assert got.email_verified is False
    _mock_http(monkeypatch, lambda _: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(OAuthError):
        await GoogleProvider("gid", "s").fetch_profile(code="c", redirect_uri="r", code_verifier=None)


@pytest.mark.anyio
async def test_kakao_provider_reads_nested_account_and_optional_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forms: list[dict[str, str]] = []
    me: dict = {
        "id": 4242,
        "kakao_account": {
            "email": "minsu@kakao.com",
            "is_email_valid": True,
            "is_email_verified": True,
            "profile": {"nickname": "민수"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://kauth.kakao.com/oauth/token":
            forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(200, json={"access_token": "kat", "token_type": "bearer"})
        assert str(request.url) == "https://kapi.kakao.com/v2/user/me"
        assert request.headers["authorization"] == "Bearer kat"
        return httpx.Response(200, content=json.dumps(me))

    _mock_http(monkeypatch, handler)
    profile = await KakaoProvider("rest-key").fetch_profile(
        code="c", redirect_uri="https://p/cb", code_verifier=None
    )
    assert profile == OAuthProfile(subject="4242", email="minsu@kakao.com", email_verified=True, name="민수")
    assert "client_secret" not in forms[0] and forms[0]["client_id"] == "rest-key"
    await KakaoProvider("rest-key", "ksecret").fetch_profile(code="c", redirect_uri="r", code_verifier=None)
    assert forms[1]["client_secret"] == "ksecret"

    # 이메일 동의가 없거나 유효하지 않으면 확인되지 않은 것으로 본다.
    me["kakao_account"] = {"email": "minsu@kakao.com", "is_email_valid": False, "is_email_verified": True}
    assert (
        await KakaoProvider("k").fetch_profile(code="c", redirect_uri="r", code_verifier=None)
    ).email_verified is False
    me.pop("kakao_account")
    bare = await KakaoProvider("k").fetch_profile(code="c", redirect_uri="r", code_verifier=None)
    assert bare == OAuthProfile(subject="4242")
    me.pop("id")
    with pytest.raises(OAuthError):
        await KakaoProvider("k").fetch_profile(code="c", redirect_uri="r", code_verifier=None)


# ---- 관리자 ----

# ---------- 관리자 API (PostgreSQL) ----------


@pytest.fixture(scope="module")
def pg_client(pg_url: str):  # noqa: ANN201
    """PostgreSQL로 띄운 앱 하나 (승인제, 관리자 boss). 사용자를 바꿀 때는 쿠키만 갈아 끼운다."""
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("asyncpg")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="fake",
        embedding_provider="fake",
        database_url=pg_url,
        admin_usernames="mboss",
        auth_signup_requires_approval=True,
        auth_allow_header_stub=False,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _login(client: TestClient, username: str) -> dict[str, str]:
    client.cookies.clear()
    r = client.post("/v1/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def test_admin_members_api_approves_promotes_and_records(pg_client: TestClient) -> None:
    client = pg_client
    tag = uuid4().hex[:8]
    email = f"m{tag}@example.com"

    # 관리자 계정은 서버에서 만든다 (공개 가입으로는 관리자 이름을 못 쓴다) — 포털로 실행해 같은 루프를 쓴다
    client.portal.call(_state(client).auth_service.create_account, "mboss", PW)  # type: ignore[union-attr]

    client.cookies.clear()
    r = _signup(client, email, display_name="대기 회원")
    assert r.status_code == 201 and r.json()["status"] == "pending", r.text
    member_id = r.json()["user_id"]
    member_csrf = {"X-CSRF-Token": r.json()["csrf_token"]}
    assert _code(client.get("/v1/contacts")) == "account_pending"
    # 관리자가 아니면 403 (pending 계정이어도 같은 답)
    assert client.get("/v1/admin/members").status_code == 403
    assert (
        client.patch(
            f"/v1/admin/members/{member_id}", json={"status": "active"}, headers=member_csrf
        ).status_code
        == 403
    )

    # 승인 대기 계정은 role=admin을 받아도 관리자 API를 쓰지 못한다 (스스로를 승인하지 못하게)
    client.portal.call(functools.partial(_state(client).auth.update_user, UUID(member_id), role="admin"))  # type: ignore[union-attr]
    r = client.get("/v1/admin/members")
    assert (r.status_code, _code(r)) == (403, "account_pending")
    client.portal.call(functools.partial(_state(client).auth.update_user, UUID(member_id), role="user"))  # type: ignore[union-attr]

    boss = _login(client, "mboss")
    page = client.get("/v1/admin/members", params={"status": "pending", "query": f"M{tag}@"})
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"] == "no-store"
    (item,) = page.json()["items"]
    assert (item["user_id"], item["email"], item["display_name"]) == (member_id, email, "대기 회원")
    assert (item["status"], item["role"], item["is_admin"], item["providers"]) == (
        "pending",
        "user",
        False,
        ["password"],
    )
    # ILIKE 특수문자는 글자 그대로 찾는다
    assert client.get("/v1/admin/members", params={"query": "%"}).json()["total"] == 0

    # 쓰기는 CSRF 필수
    assert client.patch(f"/v1/admin/members/{member_id}", json={"status": "active"}).status_code == 403
    r = client.patch(f"/v1/admin/members/{member_id}", json={"status": "active"}, headers=boss)
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    r = client.patch(f"/v1/admin/members/{member_id}", json={"role": "admin"}, headers=boss)
    assert r.json()["is_admin"] is True
    assert (
        client.patch(f"/v1/admin/members/{uuid4()}", json={"status": "active"}, headers=boss).status_code
        == 404
    )
    boss_id = client.get("/v1/auth/me").json()["user_id"]
    assert (
        client.patch(f"/v1/admin/members/{boss_id}", json={"status": "suspended"}, headers=boss).status_code
        == 422
    )
    assert client.delete(f"/v1/admin/members/{boss_id}", headers=boss).status_code == 422

    # 승인된 회원은 API를 쓰고, 역할이 admin이라 관리자 API도 열린다
    _login(client, email)
    assert client.get("/v1/auth/me").json()["role"] == "admin"
    assert client.get("/v1/contacts").status_code == 200
    assert client.get("/v1/admin/members").status_code == 200

    # 정지 → 세션 폐기 + 로그인 거부, 그 뒤 삭제
    boss = _login(client, "mboss")
    r = client.post(f"/v1/admin/members/{member_id}/revoke-sessions", headers=boss)
    assert r.status_code == 200 and r.json()["revoked"] >= 1
    r = client.patch(f"/v1/admin/members/{member_id}", json={"status": "suspended"}, headers=boss)
    assert r.json()["status"] == "suspended"
    client.cookies.clear()
    r = client.post("/v1/auth/login", json={"username": email, "password": PW})
    assert (r.status_code, _code(r)) == (403, "account_suspended")
    boss = _login(client, "mboss")
    assert client.delete(f"/v1/admin/members/{member_id}", headers=boss).status_code == 200
    assert client.get("/v1/admin/members", params={"query": email}).json()["total"] == 0

    actions = [line["action"] for line in client.get("/v1/admin/access-log?limit=50").json()]
    for expected in ("members", "member_update", "member_revoke_sessions", "member_delete"):
        assert expected in actions


def test_social_identity_round_trip_on_postgres(pg_client: TestClient) -> None:
    client = pg_client
    tag = uuid4().hex[:8]
    profile = OAuthProfile(subject=f"g-{tag}", email=f"s{tag}@example.com", email_verified=True, name="소셜")
    _fake(client, "google", profile)
    client.cookies.clear()
    assert _social_login(client, "google").headers["location"] == "/"
    me = client.get("/v1/auth/me").json()
    assert (me["email"], me["providers"], me["status"]) == (profile.email, ["google"], "pending")
    assert me["username"] == f"s{tag}"
    # 같은 subject로 다시 로그인하면 같은 계정
    client.cookies.clear()
    _social_login(client, "google")
    assert client.get("/v1/auth/me").json()["user_id"] == me["user_id"]
    # 같은 이메일로 이메일 가입은 409
    client.cookies.clear()
    assert _signup(client, profile.email or "").status_code == 409
    _state(client).oauth_providers.pop("google")
