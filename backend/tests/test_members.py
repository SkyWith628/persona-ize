"""회원관리·소셜 로그인: 이메일 가입, 승인 대기·정지, OAuth(가짜 제공자), 계정 연결, 관리자 API·기록."""

from __future__ import annotations

import asyncio
import functools
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import safe_next
from app.core.config import Settings
from app.main import create_app
from app.services import oauth
from app.services.oauth import GoogleProvider, KakaoProvider, OAuthError, OAuthProfile, pkce_challenge

PW = "correct horse battery"


def _client(**overrides) -> TestClient:  # noqa: ANN003
    s = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[call-arg]
    return TestClient(create_app(s))


def _service(client: TestClient):  # noqa: ANN202
    return client.app.state.container.auth_service  # type: ignore[attr-defined]


def _admin(client: TestClient, name: str = "boss", role: str = "user") -> dict[str, str]:
    """운영자 도구처럼 계정을 만들고 로그인한다. CSRF 헤더를 돌려준다."""
    asyncio.run(_service(client).create_account(name, PW, role=role))
    r = client.post("/v1/auth/login", json={"username": name, "password": PW})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


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
    client.app.state.container.oauth_providers[name] = provider  # type: ignore[attr-defined]
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


# ---- 이메일 가입·로그인 ----


def test_providers_reports_policy_and_configured_social_logins() -> None:
    assert _client().get("/v1/auth/providers").json() == {
        "signup_open": True,
        "requires_approval": True,
        "google": False,
        "kakao": False,
    }
    configured = _client(
        auth_allow_register=False,
        auth_signup_requires_approval=False,
        google_client_id="gid",
        google_client_secret="gsecret",
        kakao_client_id="kid",
    )
    assert configured.get("/v1/auth/providers").json() == {
        "signup_open": False,
        "requires_approval": False,
        "google": True,
        "kakao": True,
    }
    # Google은 시크릿까지 있어야 켜진다.
    assert _client(google_client_id="gid").get("/v1/auth/providers").json()["google"] is False


def test_email_signup_open_creates_active_user_and_login_by_email_or_username() -> None:
    client = _client(auth_signup_requires_approval=False)
    r = client.post(
        "/v1/auth/signup",
        json={"email": "  Minsu.Kim@Example.com ", "password": PW, "display_name": " 김민수 "},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["email"], body["display_name"], body["role"], body["status"], body["is_admin"]) == (
        "minsu.kim@example.com",
        "김민수",
        "user",
        "active",
        False,
    )
    assert body["username"] == "minsu_kim" and body["csrf_token"] and "pi_session" in r.cookies
    assert client.get("/v1/contacts").status_code == 200
    me = client.get("/v1/auth/me").json()
    assert me["providers"] == ["password"] and me["email"] == "minsu.kim@example.com"

    # 같은 이메일(대소문자 달라도) 409, 같은 앞부분의 다른 이메일은 아이디에 꼬리가 붙는다.
    dup = client.post("/v1/auth/signup", json={"email": "MINSU.KIM@example.com", "password": PW})
    assert (dup.status_code, dup.json()["code"]) == (409, "conflict")
    other = TestClient(client.app).post(
        "/v1/auth/signup", json={"email": "minsu.kim@other.org", "password": PW}
    )
    assert other.status_code == 201 and other.json()["username"].startswith("minsu_kim_")

    # 로그인: 이메일(대소문자 무시)·아이디 모두, 틀리면 없는 계정과 같은 401
    fresh = TestClient(client.app)
    by_email = fresh.post("/v1/auth/login", json={"username": "Minsu.Kim@EXAMPLE.com", "password": PW})
    assert by_email.status_code == 200 and by_email.json()["user_id"] == body["user_id"]
    assert fresh.post("/v1/auth/login", json={"username": "minsu_kim", "password": PW}).status_code == 200
    wrong = fresh.post(
        "/v1/auth/login", json={"username": "minsu.kim@example.com", "password": "wrong-pass-1"}
    )
    unknown = fresh.post(
        "/v1/auth/login", json={"username": "nobody@example.com", "password": "wrong-pass-1"}
    )
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()
    assert fresh.get("/v1/auth/me").json()["user_id"] == body["user_id"]


def test_email_signup_validation_and_closed() -> None:
    client = _client()
    for bad in ("not-an-email", "a@b", "two@@example.com", "sp ace@example.com", "x" * 250 + "@example.com"):
        r = client.post("/v1/auth/signup", json={"email": bad, "password": PW})
        assert r.status_code == 422, bad
    assert (
        client.post("/v1/auth/signup", json={"email": "ok@example.com", "password": "short"}).status_code
        == 422
    )
    long_name = client.post(
        "/v1/auth/signup", json={"email": "ok@example.com", "password": PW, "display_name": "가" * 41}
    )
    assert long_name.status_code == 422

    closed = _client(auth_allow_register=False)
    r = closed.post("/v1/auth/signup", json={"email": "ok@example.com", "password": PW})
    assert (r.status_code, r.json()["code"]) == (403, "signup_closed")
    reg = closed.post("/v1/auth/register", json={"username": "someone", "password": PW})
    assert (reg.status_code, reg.json()["code"]) == (403, "signup_closed")


def test_generated_username_never_takes_an_admin_name() -> None:
    client = _client(admin_usernames="boss", auth_signup_requires_approval=False)
    r = client.post("/v1/auth/signup", json={"email": "boss@example.com", "password": PW})
    assert r.status_code == 201 and r.json()["username"] != "boss" and r.json()["is_admin"] is False
    assert client.get("/v1/admin/overview").status_code == 403
    # 짧거나 한글뿐인 앞부분은 기본 이름으로
    short = TestClient(client.app).post("/v1/auth/signup", json={"email": "가@example.com", "password": PW})
    assert short.status_code == 201 and short.json()["username"].startswith("user")


def test_pending_user_is_blocked_outside_auth_until_approved() -> None:
    client = _client(admin_usernames="boss")  # 승인제 기본 true
    member = TestClient(client.app)
    signed = member.post("/v1/auth/signup", json={"email": "wait@example.com", "password": PW})
    assert signed.status_code == 201 and signed.json()["status"] == "pending"
    csrf = {"X-CSRF-Token": signed.json()["csrf_token"]}

    for r in (
        member.get("/v1/contacts"),
        member.get("/v1/usage"),
        member.post("/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=csrf),
    ):
        assert (r.status_code, r.json()["code"]) == (403, "account_pending")
    me = member.get("/v1/auth/me")
    assert me.status_code == 200 and me.json()["status"] == "pending"
    # 아이디 가입도 같은 정책, 헤더 스텁(개발용)은 계정이 없어 영향이 없다.
    reg = TestClient(client.app).post("/v1/auth/register", json={"username": "old_style", "password": PW})
    assert reg.json()["status"] == "pending"
    assert TestClient(client.app).get("/v1/contacts", headers={"X-User-Id": "dev"}).status_code == 200
    # 대기 중에도 다시 로그인은 된다.
    again = TestClient(client.app).post(
        "/v1/auth/login", json={"username": "wait@example.com", "password": PW}
    )
    assert again.status_code == 200 and again.json()["status"] == "pending"

    admin_csrf = _admin(client)
    approved = client.patch(
        f"/v1/admin/users/{signed.json()['user_id']}", json={"status": "active"}, headers=admin_csrf
    )
    assert approved.status_code == 200 and approved.json()["status"] == "active"
    assert member.get("/v1/contacts").status_code == 200
    # 승인 대기 계정도 스스로 탈퇴할 수 있다 (auth 경로).
    waiting = TestClient(client.app)
    w = waiting.post("/v1/auth/signup", json={"email": "leave@example.com", "password": PW})
    gone = waiting.request(
        "DELETE", "/v1/auth/me", json={"password": PW}, headers={"X-CSRF-Token": w.json()["csrf_token"]}
    )
    assert gone.status_code == 200


def test_suspended_user_loses_sessions_and_cannot_login() -> None:
    client = _client(admin_usernames="boss", auth_signup_requires_approval=False)
    member = TestClient(client.app)
    uid = member.post("/v1/auth/signup", json={"email": "bad@example.com", "password": PW}).json()["user_id"]
    assert member.get("/v1/contacts").status_code == 200
    admin_csrf = _admin(client)

    r = client.patch(f"/v1/admin/users/{uid}", json={"status": "suspended"}, headers=admin_csrf)
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    assert member.get("/v1/auth/me").status_code == 401  # 세션 폐기
    assert member.get("/v1/contacts").status_code == 401
    denied = member.post("/v1/auth/login", json={"username": "bad@example.com", "password": PW})
    assert (denied.status_code, denied.json()["code"]) == (403, "account_suspended")
    # 비밀번호가 틀리면 정지 여부를 알리지 않는다.
    assert (
        member.post(
            "/v1/auth/login", json={"username": "bad@example.com", "password": "nope-nope-1"}
        ).status_code
        == 401
    )

    client.patch(f"/v1/admin/users/{uid}", json={"status": "active"}, headers=admin_csrf)
    assert (
        member.post("/v1/auth/login", json={"username": "bad@example.com", "password": PW}).status_code == 200
    )


# ---- 소셜 로그인 ----


def test_oauth_start_redirects_to_provider_with_state_cookie_and_pkce() -> None:
    client = _client(
        google_client_id="gid.apps",
        google_client_secret="gsecret",
        kakao_client_id="kakao-rest-key",
        kakao_client_secret="ksecret",
        public_base_url="https://persona.example.com/",
    )
    r, state = _start(client, "google", "/manage")
    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert (q["client_id"], q["response_type"], q["scope"]) == ("gid.apps", "code", "openid email profile")
    assert q["redirect_uri"] == "https://persona.example.com/v1/auth/oauth/google/callback"
    assert len(state) >= 32 and q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert "gsecret" not in r.headers["location"]
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("pi_oauth=") and "HttpOnly" in cookie and "Max-Age=600" in cookie
    assert "samesite=lax" in cookie.lower() and "Path=/v1/auth/oauth" in cookie

    k, k_state = _start(client, "kakao")
    kurl = urlparse(k.headers["location"])
    kq = {key: v[0] for key, v in parse_qs(kurl.query).items()}
    assert f"{kurl.scheme}://{kurl.netloc}{kurl.path}" == "https://kauth.kakao.com/oauth/authorize"
    assert kq == {
        "client_id": "kakao-rest-key",
        "redirect_uri": "https://persona.example.com/v1/auth/oauth/kakao/callback",
        "response_type": "code",
        "state": k_state,
    }
    assert k_state != state

    # 설정되지 않은·모르는 제공자는 404, Secure 설정이면 쿠키에도 Secure
    assert _client().get("/v1/auth/oauth/google/start", follow_redirects=False).status_code == 404
    assert client.get("/v1/auth/oauth/naver/start", follow_redirects=False).status_code == 404
    assert client.get("/v1/auth/oauth/naver/callback", follow_redirects=False).status_code == 404
    secure = _client(kakao_client_id="k", session_cookie_secure=True)
    assert "Secure" in secure.get("/v1/auth/oauth/kakao/start", follow_redirects=False).headers["set-cookie"]
    # 공개 주소가 없으면 요청 주소로 콜백을 만든다 (로컬 개발).
    local = _client(kakao_client_id="k").get("/v1/auth/oauth/kakao/start", follow_redirects=False)
    assert (
        "redirect_uri=http%3A%2F%2Ftestserver%2Fv1%2Fauth%2Foauth%2Fkakao%2Fcallback"
        in local.headers["location"]
    )


def test_oauth_callback_google_creates_user_sets_session_and_returns_to_next() -> None:
    client = _client(auth_signup_requires_approval=False, public_base_url="https://persona.example.com")
    google = _fake(
        client,
        "google",
        OAuthProfile(subject="g-1", email="minsu@example.com", email_verified=True, name="김민수"),
    )
    start, state = _start(client, "google", "/manage?tab=1")
    done = client.get(
        "/v1/auth/oauth/google/callback", params={"code": "good-code", "state": state}, follow_redirects=False
    )
    assert done.status_code == 302 and done.headers["location"] == "/manage?tab=1"
    cookies = done.headers.get_list("set-cookie")
    assert any(c.startswith("pi_session=") and "HttpOnly" in c for c in cookies)
    assert any(c.startswith('pi_oauth=""') and "Max-Age=0" in c for c in cookies)  # 한 번 쓰고 지운다
    # 제공자에는 같은 redirect_uri와, 시작 때 만든 verifier가 간다 (challenge = S256(verifier)).
    call = google.calls[0]
    assert call["redirect_uri"] == "https://persona.example.com/v1/auth/oauth/google/callback"
    assert call["code_verifier"] and pkce_challenge(call["code_verifier"]) == google.challenge
    assert "good-code" not in done.headers["location"]

    me = client.get("/v1/auth/me").json()
    assert (me["email"], me["display_name"], me["status"], me["providers"]) == (
        "minsu@example.com",
        "김민수",
        "active",
        ["google"],
    )
    assert me["username"] == "minsu" and client.get("/v1/contacts").status_code == 200

    # 다시 로그인하면 같은 사용자 (이메일이 바뀌어도 subject로 찾는다). 가입이 닫혀도 기존 계정은 들어온다.
    google.profile = OAuthProfile(subject="g-1", email="changed@example.com", email_verified=True)
    client.app.state.container.settings.auth_allow_register = False  # type: ignore[attr-defined]
    second = TestClient(client.app)
    assert _social_login(second, "google").headers["location"] == "/"
    assert second.get("/v1/auth/me").json()["user_id"] == me["user_id"]

    # 소셜 전용 계정: 비밀번호 로그인은 같은 401, 탈퇴는 빈 비밀번호로.
    assert (
        second.post("/v1/auth/login", json={"username": "minsu@example.com", "password": PW}).status_code
        == 401
    )
    csrf = {"X-CSRF-Token": second.get("/v1/auth/me").json()["csrf_token"]}
    assert second.request("DELETE", "/v1/auth/me", json={"password": ""}).status_code == 403  # CSRF 없음
    assert second.request("DELETE", "/v1/auth/me", json={"password": ""}, headers=csrf).status_code == 200
    assert client.get("/v1/auth/me").status_code == 401


def test_oauth_callback_kakao_without_email_creates_pending_account() -> None:
    client = _client()  # 승인제 기본 true
    kakao = _fake(client, "kakao", OAuthProfile(subject="987654321", name="민수"))
    done = _social_login(client, "kakao")
    assert done.status_code == 302 and done.headers["location"] == "/"
    assert kakao.calls[0]["code_verifier"] is None  # 카카오는 PKCE를 쓰지 않는다
    me = client.get("/v1/auth/me").json()
    assert (me["email"], me["display_name"], me["status"], me["providers"]) == (
        None,
        "민수",
        "pending",
        ["kakao"],
    )
    assert me["username"] == "kakao"
    assert client.get("/v1/contacts").json()["code"] == "account_pending"
    # 이메일 없는 계정이 여럿이어도 된다 (아이디만 겹치지 않게).
    kakao.profile = OAuthProfile(subject="111")
    other = TestClient(client.app)
    assert _social_login(other, "kakao").headers["location"] == "/"
    assert other.get("/v1/auth/me").json()["username"].startswith("kakao_")


def test_oauth_callback_rejects_state_mismatch_missing_cookie_denial_and_provider_failure() -> None:
    client = _client(auth_signup_requires_approval=False)
    google = _fake(client, "google", OAuthProfile(subject="g-1", email="a@example.com", email_verified=True))
    _fake(client, "kakao", OAuthProfile(subject="k-1"))

    def callback(c: TestClient, provider: str = "google", **params: str) -> httpx.Response:
        return c.get(f"/v1/auth/oauth/{provider}/callback", params=params, follow_redirects=False)

    def failed(r: httpx.Response, code: str) -> bool:
        cleared = any(c.startswith('pi_oauth=""') for c in r.headers.get_list("set-cookie"))
        no_session = not any(c.startswith("pi_session=") for c in r.headers.get_list("set-cookie"))
        return (
            r.status_code == 302
            and r.headers["location"] == f"/?auth_error={code}"
            and cleared
            and no_session
        )

    # state 불일치 → 실패, 쿠키도 지워져 맞는 state로 다시 와도 실패 (한 번만 쓴다)
    _, state = _start(client, "google")
    assert failed(callback(client, code="good-code", state="forged-state"), "oauth_failed")
    assert failed(callback(client, code="good-code", state=state), "oauth_failed")
    # 쿠키 없는 브라우저(공격자가 만든 링크), state 없음
    assert failed(callback(TestClient(client.app), code="good-code", state=state), "oauth_failed")
    _start(client, "google")
    assert failed(callback(client, code="good-code"), "oauth_failed")
    # google로 시작한 state를 kakao 콜백에 쓰면 실패
    _, state = _start(client, "google")
    client.cookies.set("pi_oauth", client.cookies.get("pi_oauth"), path="/v1/auth/oauth")
    assert failed(callback(client, "kakao", code="good-code", state=state), "oauth_failed")
    assert google.calls == []  # 여기까지 제공자를 한 번도 부르지 않았다

    # 사용자가 동의를 취소, 코드 없음, 제공자 교환 실패
    _, state = _start(client, "google")
    assert failed(callback(client, error="access_denied", state=state), "oauth_denied")
    _, state = _start(client, "google")
    assert failed(callback(client, state=state), "oauth_failed")
    _, state = _start(client, "google")
    assert failed(callback(client, code="bad-code", state=state), "oauth_failed")
    assert client.get("/v1/auth/me").status_code == 401
    # state 없이 온 error는 신뢰하지 않는다 (oauth_denied가 아니라 oauth_failed).
    assert failed(callback(TestClient(client.app), error="access_denied"), "oauth_failed")


def test_oauth_links_by_verified_email_but_never_by_unverified_email() -> None:
    client = _client(auth_signup_requires_approval=False)
    owner = client.post("/v1/auth/signup", json={"email": "owner@example.com", "password": PW}).json()

    # 확인된 이메일이 같으면 기존 계정에 연결된다 (구글 → 카카오도 같은 계정).
    linked = TestClient(client.app)
    _fake(client, "google", OAuthProfile(subject="g-owner", email="owner@example.com", email_verified=True))
    assert _social_login(linked, "google").headers["location"] == "/"
    me = linked.get("/v1/auth/me").json()
    assert me["user_id"] == owner["user_id"] and me["providers"] == ["password", "google"]
    _fake(client, "kakao", OAuthProfile(subject="k-owner", email="owner@example.com", email_verified=True))
    assert _social_login(linked, "kakao").headers["location"] == "/"
    assert linked.get("/v1/auth/me").json()["providers"] == ["password", "google", "kakao"]

    # 확인되지 않은 이메일은 연결하지 않는다 → 이메일 없는 새 계정 (남의 주소로 계정을 가로챌 수 없다).
    attacker = TestClient(client.app)
    _fake(
        client, "kakao", OAuthProfile(subject="k-attacker", email="owner@example.com", email_verified=False)
    )
    assert _social_login(attacker, "kakao").headers["location"] == "/"
    stranger = attacker.get("/v1/auth/me").json()
    assert stranger["user_id"] != owner["user_id"]
    assert (stranger["email"], stranger["providers"]) == (None, ["kakao"])
    # 원래 주인의 로그인·연결은 그대로다.
    assert client.get("/v1/auth/me").json()["providers"] == ["password", "google", "kakao"]


def test_oauth_signup_closed_blocks_new_social_user_but_not_existing_or_linkable() -> None:
    client = _client(auth_allow_register=False)
    asyncio.run(_service(client).create_account("operator", PW))
    _fake(client, "google", OAuthProfile(subject="g-new", email="new@example.com", email_verified=True))
    r = _social_login(client, "google")
    assert r.status_code == 302 and r.headers["location"] == "/?auth_error=signup_closed"
    assert client.get("/v1/auth/me").status_code == 401
    # 계정이 만들어지지 않았다.
    assert asyncio.run(client.app.state.container.auth.list_users())[0] == 1  # type: ignore[attr-defined]


def test_oauth_suspended_user_is_redirected_with_account_suspended() -> None:
    client = _client(admin_usernames="boss", auth_signup_requires_approval=False)
    _fake(client, "kakao", OAuthProfile(subject="k-1"))
    member = TestClient(client.app)
    _social_login(member, "kakao")
    uid = member.get("/v1/auth/me").json()["user_id"]
    client.patch(f"/v1/admin/users/{uid}", json={"status": "suspended"}, headers=_admin(client))
    r = _social_login(member, "kakao")
    assert r.headers["location"] == "/?auth_error=account_suspended"
    assert member.get("/v1/auth/me").status_code == 401


def test_safe_next_allows_only_same_origin_paths() -> None:
    assert safe_next("/manage?tab=1#x") == "/manage?tab=1#x"
    for bad in (
        None,
        "",
        "manage",
        "//evil.example",
        "/\\evil.example",
        "https://evil.example",
        "/a\nb",
        "/" + "a" * 600,
    ):
        assert safe_next(bad) == "/", bad
    client = _client(kakao_client_id="k", auth_signup_requires_approval=False)
    _fake(client, "kakao", OAuthProfile(subject="k-1"))
    assert _social_login(client, "kakao", "//evil.example/x").headers["location"] == "/"
    assert _social_login(client, "kakao", "https://evil.example").headers["location"] == "/"


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


def test_admin_users_list_search_counts_and_non_admin_is_forbidden() -> None:
    client = _client(admin_usernames="boss")
    assert client.get("/v1/admin/users").status_code == 401
    assert client.get("/v1/admin/users", headers={"X-User-Id": "boss"}).status_code == 401  # 헤더 스텁
    member = TestClient(client.app)
    m = member.post(
        "/v1/auth/signup", json={"email": "jihyun@example.com", "password": PW, "display_name": "박지현"}
    ).json()
    for path in ("/v1/admin/users", "/v1/admin/audit", "/v1/admin/overview"):
        r = member.get(path)
        assert (r.status_code, r.json()["code"]) == (403, "forbidden"), path
    member_csrf = {"X-CSRF-Token": m["csrf_token"]}
    for r in (
        member.patch(f"/v1/admin/users/{m['user_id']}", json={"status": "active"}, headers=member_csrf),
        member.post(f"/v1/admin/users/{m['user_id']}/revoke-sessions", headers=member_csrf),
        member.delete(f"/v1/admin/users/{m['user_id']}", headers=member_csrf),
    ):
        assert r.status_code == 403
    assert member.get("/v1/auth/me").json()["status"] == "pending"  # 스스로 승인하지 못했다

    csrf = _admin(client)
    kakao_user = TestClient(client.app)
    _fake(client, "kakao", OAuthProfile(subject="k-9", name="카카오 회원"))
    _social_login(kakao_user, "kakao")
    # 승인 뒤 상대를 만들고 첨삭 한 번 → 목록 수치에 잡힌다.
    client.patch(f"/v1/admin/users/{m['user_id']}", json={"status": "active"}, headers=csrf)
    contact = member.post(
        "/v1/contacts", json={"alias": "팀장님", "relationship_type": "superior"}, headers=member_csrf
    ).json()
    draft = "내일 3시까지 초안 보내드릴게요."
    assert (
        member.post(
            "/v1/rewrites",
            json={"contact_id": contact["id"], "draft_text": draft, "condition": "A"},
            headers=member_csrf,
        ).status_code
        == 201
    )

    r = client.get("/v1/admin/users")
    body = r.json()
    assert r.status_code == 200 and body["total"] == 3
    assert [u["username"] for u in body["items"]] == ["kakao", "boss", "jihyun"]  # 최신 가입순
    by_name = {u["username"]: u for u in body["items"]}
    assert set(by_name["jihyun"]) == {
        "user_id",
        "username",
        "email",
        "display_name",
        "role",
        "status",
        "is_admin",
        "providers",
        "created_at",
        "last_login_at",
        "rewrites_today",
        "contacts",
    }
    assert (by_name["jihyun"]["rewrites_today"], by_name["jihyun"]["contacts"]) == (1, 1)
    assert (by_name["jihyun"]["email"], by_name["jihyun"]["status"]) == ("jihyun@example.com", "active")
    assert by_name["jihyun"]["last_login_at"] is not None
    assert (by_name["boss"]["is_admin"], by_name["boss"]["role"], by_name["boss"]["providers"]) == (
        True,
        "user",
        ["password"],
    )
    assert (by_name["kakao"]["status"], by_name["kakao"]["providers"], by_name["kakao"]["contacts"]) == (
        "pending",
        ["kakao"],
        0,
    )
    assert draft not in r.text and "팀장님" not in r.text  # 원문·상대 이름은 없다

    def names(**params: str | int) -> list[str]:
        return [u["username"] for u in client.get("/v1/admin/users", params=params).json()["items"]]

    assert names(query="JIHYUN@") == ["jihyun"]  # 이메일 부분 일치, 대소문자 무시
    assert names(query="카카오") == ["kakao"]  # 표시 이름
    assert names(query="os") == ["boss"]
    assert names(status="pending") == ["kakao"] and names(role="admin") == []
    assert names(limit=1, offset=1) == ["boss"]
    assert client.get("/v1/admin/users", params={"limit": 1}).json()["total"] == 3
    assert client.get("/v1/admin/users", params={"status": "nope"}).status_code == 422
    assert client.get("/v1/admin/users", params={"limit": 0}).status_code == 422

    overview = client.get("/v1/admin/overview").json()
    assert overview["users"] == {"total": 3, "pending": 1, "suspended": 0, "new_in_range": 3}


def test_admin_patch_role_self_protection_csrf_and_not_found() -> None:
    client = _client(admin_usernames="boss", auth_signup_requires_approval=False)
    csrf = _admin(client)
    boss_id = client.get("/v1/auth/me").json()["user_id"]
    member = TestClient(client.app)
    uid = member.post("/v1/auth/signup", json={"email": "m@example.com", "password": PW}).json()["user_id"]

    # CSRF 없으면 403, 없는 사용자 404, 빈 본문 400, 모르는 값 422
    assert client.patch(f"/v1/admin/users/{uid}", json={"role": "admin"}).status_code == 403
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.patch(f"/v1/admin/users/{missing}", json={"role": "admin"}, headers=csrf).status_code == 404
    empty = client.patch(f"/v1/admin/users/{uid}", json={}, headers=csrf)
    assert (empty.status_code, empty.json()["code"]) == (400, "invalid_input")
    assert client.patch(f"/v1/admin/users/{uid}", json={"status": "pending"}, headers=csrf).status_code == 422
    assert client.patch(f"/v1/admin/users/{uid}", json={"role": "owner"}, headers=csrf).status_code == 422

    # 권한 올리기 → 그 사용자도 관리자 API를 쓴다
    assert member.get("/v1/admin/users").status_code == 403
    promoted = client.patch(f"/v1/admin/users/{uid}", json={"role": "admin"}, headers=csrf)
    assert promoted.status_code == 200 and (promoted.json()["role"], promoted.json()["is_admin"]) == (
        "admin",
        True,
    )
    assert member.get("/v1/admin/users").status_code == 200
    assert member.get("/v1/auth/me").json()["is_admin"] is True

    # 자기 자신: 정지·권한 내리기·삭제 400
    member_csrf = {"X-CSRF-Token": member.get("/v1/auth/me").json()["csrf_token"]}
    for body in ({"status": "suspended"}, {"role": "user"}, {"status": "suspended", "role": "user"}):
        r = member.patch(f"/v1/admin/users/{uid}", json=body, headers=member_csrf)
        assert (r.status_code, r.json()["code"]) == (400, "invalid_input"), body
    assert (
        client.patch(f"/v1/admin/users/{boss_id}", json={"status": "suspended"}, headers=csrf).status_code
        == 400
    )
    gone = client.delete(f"/v1/admin/users/{boss_id}", headers=csrf)
    assert (gone.status_code, gone.json()["code"]) == (400, "invalid_input")
    assert (
        member.get("/v1/auth/me").json()["role"] == "admin" and client.get("/v1/auth/me").status_code == 200
    )

    # 다른 관리자가 내리는 것은 된다
    demoted = client.patch(f"/v1/admin/users/{uid}", json={"role": "user"}, headers=csrf)
    assert demoted.json()["role"] == "user" and member.get("/v1/admin/users").status_code == 403


def test_admin_revoke_sessions_delete_user_and_audit_log() -> None:
    client = _client(admin_usernames="boss")
    csrf = _admin(client)
    member = TestClient(client.app)
    signed = member.post("/v1/auth/signup", json={"email": "m@example.com", "password": PW}).json()
    uid, member_csrf = signed["user_id"], {"X-CSRF-Token": signed["csrf_token"]}
    second_device = TestClient(client.app)
    second_device.post("/v1/auth/login", json={"username": "m@example.com", "password": PW})

    client.get("/v1/admin/users", params={"query": "m@"})
    client.patch(f"/v1/admin/users/{uid}", json={"status": "active"}, headers=csrf)  # 승인
    client.patch(f"/v1/admin/users/{uid}", json={"status": "active"}, headers=csrf)  # 그대로 → 기록 없음
    member.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=member_csrf
    )

    assert client.post(f"/v1/admin/users/{uid}/revoke-sessions").status_code == 403  # CSRF
    revoked = client.post(f"/v1/admin/users/{uid}/revoke-sessions", headers=csrf)
    assert revoked.status_code == 200 and revoked.json() == {"revoked": 2}
    assert (
        member.get("/v1/auth/me").status_code == 401 and second_device.get("/v1/auth/me").status_code == 401
    )
    assert client.get("/v1/auth/me").status_code == 200  # 관리자 세션은 그대로

    client.patch(f"/v1/admin/users/{uid}", json={"status": "suspended", "role": "admin"}, headers=csrf)
    client.patch(f"/v1/admin/users/{uid}", json={"status": "active"}, headers=csrf)  # 정지 해제

    assert client.delete(f"/v1/admin/users/{uid}").status_code == 403  # CSRF
    deleted = client.delete(f"/v1/admin/users/{uid}", headers=csrf)
    assert deleted.status_code == 200
    assert deleted.json() == {
        "contacts": 1,
        "conversations": 0,
        "messages": 0,
        "graph_nodes_removed": 0,
        "graph_error": None,
    }
    assert client.delete(f"/v1/admin/users/{uid}", headers=csrf).status_code == 404
    assert (
        member.post("/v1/auth/login", json={"username": "m@example.com", "password": PW}).status_code == 401
    )
    assert client.get("/v1/admin/users").json()["total"] == 1

    r = client.get("/v1/admin/audit")
    items = r.json()["items"]
    assert r.status_code == 200
    actions = [i["action"] for i in items]
    assert actions == [
        "users.view",
        "user.delete",
        "user.activate",
        "user.role",
        "user.suspend",
        "user.revoke_sessions",
        "user.approve",
        "users.view",
    ]
    assert set(items[0]) == {"id", "at", "admin_username", "action", "target_username", "detail"}
    assert all(i["admin_username"] == "boss" for i in items)
    assert items[1]["target_username"] == "user" and "contacts=1" in items[1]["detail"]  # 'm'은 짧다
    assert items[3]["detail"] == "user→admin" and items[2]["detail"] == "suspended→active"
    assert items[-1]["target_username"] is None and "m@" in items[-1]["detail"]
    assert PW not in r.text and "박지현" not in r.text
    assert len(client.get("/v1/admin/audit", params={"limit": 2}).json()["items"]) == 2
    assert client.get("/v1/admin/audit", params={"limit": 0}).status_code == 422


# ---- 운영자 도구 ----


def test_create_account_admin_role_and_reset_password() -> None:
    from app.core.errors import InvalidInputError
    from app.tools.create_user import parse_args

    client = _client(auth_allow_register=False, auth_allow_header_stub=False)
    service = _service(client)
    admin = asyncio.run(service.create_account("root_admin", PW, role="admin"))
    assert (admin.role, admin.status) == ("admin", "active")
    assert (
        client.post("/v1/auth/login", json={"username": "root_admin", "password": PW}).json()["is_admin"]
        is True
    )
    assert client.get("/v1/admin/users").status_code == 200  # ADMIN_USERNAMES 없이 role만으로

    asyncio.run(service.create_account("plain_user", PW))
    device = TestClient(client.app)
    device.post("/v1/auth/login", json={"username": "plain_user", "password": PW})
    changed = asyncio.run(service.reset_password("plain_user", "brand-new-password-9"))
    assert changed.role == "user"
    assert device.get("/v1/auth/me").status_code == 401  # 세션 폐기
    assert device.post("/v1/auth/login", json={"username": "plain_user", "password": PW}).status_code == 401
    ok = device.post("/v1/auth/login", json={"username": "plain_user", "password": "brand-new-password-9"})
    assert ok.status_code == 200 and ok.json()["is_admin"] is False
    assert (
        asyncio.run(service.reset_password("plain_user", "another-password-1", make_admin=True)).role
        == "admin"
    )
    with pytest.raises(InvalidInputError):
        asyncio.run(service.reset_password("nobody_here", "long-enough-pass"))
    with pytest.raises(InvalidInputError):
        asyncio.run(service.reset_password("plain_user", "short"))

    args = parse_args(["someone", "--admin", "--reset-password"])
    assert (args.username, args.admin, args.reset_password) == ("someone", True, True)
    plain = parse_args(["someone"])
    assert (plain.admin, plain.reset_password) == (False, False)


# ---- 화면 에이전트와 맞춘 계약 확인 ----


def test_oauth_failure_returns_to_stored_next_with_error_appended() -> None:
    from app.api.routes.auth import with_auth_error

    assert with_auth_error("/", "oauth_denied") == "/?auth_error=oauth_denied"
    assert with_auth_error("/manage?tab=1", "oauth_failed") == "/manage?tab=1&auth_error=oauth_failed"
    assert with_auth_error("/admin#users", "signup_closed") == "/admin?auth_error=signup_closed#users"

    client = _client(auth_allow_register=False)
    _fake(client, "google", OAuthProfile(subject="g-new", email="new@example.com", email_verified=True))

    def callback(**params: str) -> str:
        r = client.get("/v1/auth/oauth/google/callback", params=params, follow_redirects=False)
        assert r.status_code == 302
        return r.headers["location"]

    _, state = _start(client, "google", "/admin")
    assert callback(error="access_denied", state=state) == "/admin?auth_error=oauth_denied"
    _, state = _start(client, "google", "/manage?tab=1")
    assert callback(code="bad-code", state=state) == "/manage?tab=1&auth_error=oauth_failed"
    _, state = _start(client, "google", "/admin")
    assert callback(code="good-code", state=state) == "/admin?auth_error=signup_closed"  # 가입 닫힘
    _, state = _start(client, "google", "/admin")
    assert callback(code="good-code", state="forged") == "/admin?auth_error=oauth_failed"
    # 상태 쿠키가 없으면(또는 깨졌으면) 돌아갈 곳을 모른다 → `/`
    assert callback(code="good-code", state=state) == "/?auth_error=oauth_failed"
    client.cookies.set("pi_oauth", "not-base64-json", path="/v1/auth/oauth")
    assert callback(code="good-code", state=state) == "/?auth_error=oauth_failed"
    # 바깥 주소를 next로 줘도 실패 리다이렉트는 같은 출처다.
    _, state = _start(client, "google", "https://evil.example/x")
    assert callback(error="access_denied", state=state) == "/?auth_error=oauth_denied"


def test_users_view_audit_skips_count_polls_and_later_pages() -> None:
    client = _client(admin_usernames="boss")
    _admin(client)
    TestClient(client.app).post("/v1/auth/signup", json={"email": "wait@example.com", "password": PW})

    def views() -> int:
        items = client.get("/v1/admin/audit").json()["items"]
        return sum(1 for i in items if i["action"] == "users.view")

    # 화면이 대기 수를 세는 조회: total은 쪽 크기가 아니라 조건에 맞는 전체 수, 기록은 남기지 않는다.
    poll = client.get("/v1/admin/users", params={"status": "pending", "limit": 1}).json()
    assert poll["total"] == 1 and len(poll["items"]) == 1
    assert client.get("/v1/admin/users", params={"limit": 1}).json()["total"] == 2
    client.get("/v1/admin/users", params={"limit": 50, "offset": 50})  # 다음 쪽
    assert views() == 0
    client.get("/v1/admin/users")
    client.get("/v1/admin/users", params={"query": "wait", "limit": 2})
    assert views() == 2
    detail = client.get("/v1/admin/audit").json()["items"][0]["detail"]
    assert isinstance(detail, str) and detail == "query='wait' status=- role=- n=1"


def test_contract_details_for_screens() -> None:
    client = _client(admin_usernames="boss")
    csrf = _admin(client)

    # 가입 201: 세션 쿠키 + csrf_token, AuthOut 필드 전부
    member = TestClient(client.app)
    signed = member.post("/v1/auth/signup", json={"email": "new@example.com", "password": PW})
    assert signed.status_code == 201 and signed.cookies.get("pi_session") and signed.json()["csrf_token"]
    auth_out = {"user_id", "username", "csrf_token", "expires_at", "email", "display_name", "role", "status"}
    assert set(signed.json()) == auth_out | {"is_admin"}
    uid = signed.json()["user_id"]

    # 대기 세션: me 200(필드 전부, providers에 password), 로그아웃 204
    me = member.get("/v1/auth/me")
    assert me.status_code == 200 and me.json()["status"] == "pending" and "password" in me.json()["providers"]
    assert set(me.json()) == (auth_out - {"expires_at"}) | {"is_admin", "providers"}
    assert member.post("/v1/auth/logout").status_code == 204
    assert member.get("/v1/auth/me").status_code == 401
    member.post("/v1/auth/login", json={"username": "new@example.com", "password": PW})

    # PATCH는 AdminUser 전체를 돌려준다
    patched = client.patch(f"/v1/admin/users/{uid}", json={"status": "active"}, headers=csrf)
    listed = client.get("/v1/admin/users", params={"query": "new@"}).json()["items"][0]
    assert patched.status_code == 200 and patched.json() == listed
    assert patched.json()["providers"] == ["password"] and patched.json()["rewrites_today"] == 0

    # 정지 → 세션 폐기 → 다음 me는 401. 오류 본문은 평평한 {code, message}.
    assert member.get("/v1/auth/me").status_code == 200
    client.patch(f"/v1/admin/users/{uid}", json={"status": "suspended"}, headers=csrf)
    after = member.get("/v1/auth/me")
    assert after.status_code == 401 and set(after.json()) == {"code", "message"}

    # 아이디 가입(하위 호환)도 같은 모양으로 201
    legacy = TestClient(client.app).post("/v1/auth/register", json={"username": "old_style", "password": PW})
    assert legacy.status_code == 201 and set(legacy.json()) == auth_out | {"is_admin"}
    assert legacy.cookies.get("pi_session") and legacy.json()["email"] is None
