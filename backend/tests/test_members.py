"""회원관리·소셜 로그인 (2026-11-16, `design/iphone-mobile` c001cbb의 test_members.py를 main에 맞춰 옮김).

- 이메일 가입·이메일 로그인, 승인제(대기 계정은 로그인 불가), 정지(세션·Bearer 즉시 차단)
- OAuth: state(쿠키 묶임·일회용·만료)·PKCE·돌아갈 경로 검사·확인된 이메일로만 연결,
  새 계정의 가입 문(닫힘·초대·동의·승인)
- 제공자 HTTP는 `httpx.MockTransport`, 라우트는 가짜 제공자 — 네트워크를 쓰지 않는다
- 회원관리 규칙(자기 보호·설정상의 관리자 보호·기록 먼저)은 인메모리로, 관리자 API는 PostgreSQL 통합으로 본다
  (main의 관리자 API는 인메모리 저장소에서 503이다 — `pg_url`이 없으면 그 부분은 건너뛴다)
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import safe_next, with_auth_error
from app.core.config import Settings
from app.core.errors import (
    DomainError,
    InvalidInputError,
    LastAdminError,
    MemberChangedError,
    NotFoundError,
)
from app.core.guard import inspect_production_settings
from app.core.ratelimit import AUTH_LIMITED
from app.main import create_app
from app.repositories.auth import InMemoryAuthRepo
from app.services.auth_service import AuthService, normalize_email, username_seed
from app.services.member_admin_service import CannotModifySelfError, ProtectedAdminError
from app.services.oauth import (
    MAX_PENDING_STATES_PER_CLIENT,
    GoogleProvider,
    KakaoProvider,
    OAuthError,
    OAuthProfile,
    OAuthStateStore,
    OAuthStateStoreFullError,
    PendingLogin,
    build_oauth_providers,
    clean_email,
    pkce_challenge,
)
from app.tools import create_user

PW = "correct horse battery"
CONTACT = {"alias": "박지현", "relationship_type": "superior"}


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[call-arg]


def _client(**overrides: Any) -> TestClient:
    return TestClient(create_app(_settings(**overrides)))


def _state(client: TestClient):  # noqa: ANN202
    return client.app.state.container  # type: ignore[attr-defined]


def _service(client: TestClient) -> AuthService:
    return _state(client).auth_service


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _code(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, r.json()["code"]


class Log:
    """`admin_access_log` 자리에 끼우는 가짜 기록. `down`이면 기록에 실패한다."""

    def __init__(self, *, down: bool = False) -> None:
        self.lines: list[tuple[str, str]] = []
        self.down = down

    async def __call__(self, action: str, detail: str) -> None:
        if self.down:
            raise RuntimeError("log down")
        self.lines.append((action, detail))

    @property
    def actions(self) -> list[str]:
        return [a for a, _ in self.lines]


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


def _start_post(client: TestClient, provider: str, **body: Any) -> str:
    r = client.post(f"/v1/auth/oauth/{provider}/start", json=body)
    assert r.status_code == 200, r.text
    return parse_qs(urlparse(r.json()["authorize_url"]).query)["state"][0]


def _callback(client: TestClient, provider: str, **params: str) -> httpx.Response:
    return client.get(f"/v1/auth/oauth/{provider}/callback", params=params, follow_redirects=False)


def _social_login(client: TestClient, provider: str, next_path: str | None = None) -> httpx.Response:
    _, state = _start(client, provider, next_path)
    return _callback(client, provider, code="good-code", state=state)


def _has_session_cookie(r: httpx.Response) -> bool:
    return any(c.startswith("pi_session=") for c in r.headers.get_list("set-cookie"))


# ---------- 기본값: 예전 동작 그대로 ----------


def test_defaults_keep_todays_behaviour_approval_off_and_social_off() -> None:
    s = _settings()
    assert s.auth_signup_requires_approval is False
    assert (s.google_client_id, s.google_client_secret, s.kakao_client_id, s.kakao_client_secret) == ("",) * 4
    assert s.oauth_provider_names == () and build_oauth_providers(s) == {}

    client = _client()
    assert client.get("/v1/auth/providers").json() == {
        "signup_open": True,
        "requires_approval": False,
        "invite_required": False,
        "require_consent": False,
        "google": False,
        "kakao": False,
    }
    # 설정이 없으면 소셜 로그인은 없는 기능이다 (시작·콜백 모두 404, state 쿠키도 심지 않는다)
    for method, path in (
        ("GET", "/v1/auth/oauth/google/start"),
        ("POST", "/v1/auth/oauth/google/start"),
        ("GET", "/v1/auth/oauth/kakao/start"),
        ("GET", "/v1/auth/oauth/kakao/callback?code=x&state=y"),
        ("GET", "/v1/auth/oauth/naver/start"),
    ):
        r = client.request(method, path, follow_redirects=False)
        assert _code(r) == (404, "not_found"), path
        assert "set-cookie" not in r.headers
    # 가입은 예전처럼 201 + 세션
    r = client.post("/v1/auth/register", json={"username": "minsu_kim", "password": PW})
    assert r.status_code == 201 and r.cookies.get("pi_session")
    assert (r.json()["role"], r.json()["status"], r.json()["email"]) == ("user", "active", None)


def test_provider_needs_both_id_and_secret() -> None:
    assert _settings(google_client_id="gid").oauth_provider_names == ()
    assert _settings(google_client_secret="s").oauth_provider_names == ()
    assert _settings(kakao_client_id="kid").oauth_provider_names == ()  # 카카오도 시크릿까지 있어야 한다
    assert _settings(kakao_client_id=" ", kakao_client_secret=" ").oauth_provider_names == ()
    both = _settings(
        google_client_id="gid", google_client_secret="gs", kakao_client_id="kid", kakao_client_secret="ks"
    )
    assert both.oauth_provider_names == ("google", "kakao")
    configured = TestClient(create_app(both.model_copy(update={"auth_signup_requires_approval": True})))
    body = configured.get("/v1/auth/providers").json()
    assert (body["google"], body["kakao"], body["requires_approval"]) == (True, True, True)
    assert "gs" not in configured.get("/v1/auth/providers").text


def test_production_guard_requires_https_public_base_url_for_social_login() -> None:
    base = {"google_client_id": "gid", "google_client_secret": "gs"}
    missing = inspect_production_settings(_settings(**base))
    assert any("PUBLIC_BASE_URL" in e for e in missing.errors)
    plain = inspect_production_settings(_settings(**base, public_base_url="http://persona.example.com"))
    assert any("PUBLIC_BASE_URL" in e for e in plain.errors)
    ok = inspect_production_settings(_settings(**base, public_base_url="https://persona.example.com"))
    assert not any("PUBLIC_BASE_URL" in e for e in ok.errors)
    # 소셜 로그인이 꺼져 있으면 이 검사는 없다 (예전 설정은 그대로 통과한다)
    assert not any("PUBLIC_BASE_URL" in e for e in inspect_production_settings(_settings()).errors)
    half = inspect_production_settings(_settings(kakao_client_id="kid"))
    assert any("KAKAO" in w for w in half.warnings)


def test_empty_admin_usernames_warning_does_not_claim_nobody_can_be_admin() -> None:
    """관리자는 권한(role)으로도 된다 — 이름 목록이 비었다고 "아무도 못 들어간다"고 단정하지 않는다."""
    warning = next(w for w in inspect_production_settings(_settings()).warnings if "ADMIN_USERNAMES" in w)
    assert "권한(role)이 admin인 계정도 없으면" in warning and "app.tools.create_user" in warning
    assert "비어 있습니다 — 관리자 화면(/admin)에 아무도" not in warning  # 예전의 단정하는 문장
    named = inspect_production_settings(_settings(admin_usernames="boss"))
    assert not any("ADMIN_USERNAMES가 비어" in w for w in named.warnings)


def test_proxy_access_log_redacts_oauth_code_and_state() -> None:
    """Caddy 접근 로그에 소셜 로그인 콜백의 인가 코드·state가 남지 않게 쿼리 값을 가린다 (Caddyfile).

    문법과 실제 가림은 `caddy:2-alpine`(v2.11.9)으로 확인했다(2026-11-17).
    여기서는 그 설정이 빠지지 않는지만 본다.
    """
    caddyfile = Path(__file__).resolve().parents[2] / "docker" / "caddy" / "Caddyfile"
    if not caddyfile.is_file():
        pytest.skip("저장소의 docker/ 폴더가 없는 곳(테스트 이미지)에서는 건너뛴다.")
    text = caddyfile.read_text(encoding="utf-8")
    log_block = text[text.rindex("	log {") :]
    uri = re.search(r"request>uri query \{(.*?)\}", log_block, re.S)
    assert uri is not None, "접근 로그의 request>uri 쿼리 필터가 없다"
    assert re.findall(r"replace (\w+) REDACTED", uri.group(1)) == ["code", "state"]
    location = re.search(r"resp_headers>Location query \{(.*?)\}", log_block, re.S)
    assert location is not None and "replace state REDACTED" in location.group(1)
    # 예전부터 있던 헤더 가림도 그대로다
    assert "request>headers>X-Csrf-Token delete" in log_block
    assert "request>headers>X-User-Id delete" in log_block


# ---------- 이메일 가입·이메일 로그인 ----------


def test_email_helpers() -> None:
    assert normalize_email("  Minsu.Kim@Example.COM ") == "minsu.kim@example.com"
    for bad in ("not-an-email", "a@b", "two@@example.com", "sp ace@example.com", "a@b..com", "@example.com"):
        with pytest.raises(InvalidInputError):
            normalize_email(bad)
    with pytest.raises(InvalidInputError):
        normalize_email("x" * 250 + "@example.com")
    # 눈에는 같아 보여도 다른 주소가 되는 값·IP 주소 형식은 받지 않는다 (2026-11-17 보안 검토)
    for bad in (
        "min\u200bsu@example.com",  # 폭 없는 공백
        "minsu@exam\u200dple.com",  # 폭 없는 결합자
        "\ufeffminsu@example.com",  # BOM
        "ｍｉｎｓｕ@example.com",  # 전각 영문
        "minsu@ｅｘａｍｐｌｅ.com",
        "minsu@example。com",  # 전각 마침표
        "민수@example.com",
        "minsu@한국.kr",
        "minsu@127.0.0.1",
        "minsu@[127.0.0.1]",
        "minsu@[IPv6:::1]",
        "minsu@example.c",  # 한 글자 TLD
        "minsu@example.123",
        "minsu@-example.com",
        "minsu@example-.com",
        "minsu@exa_mple.com",
        "minsu@example.com.",
        ".minsu@example.com",
        "minsu.@example.com",
        "min..su@example.com",
        '"min su"@example.com',
        "min\tsu@example.com",
        "minsu@example.com\nbcc:x@example.com",
        "a" * 65 + "@example.com",  # local part 64자 초과
        "minsu@" + "a" * 64 + ".com",  # 라벨 63자 초과
    ):
        with pytest.raises(InvalidInputError):
            normalize_email(bad)
        assert clean_email(bad) is None, bad  # 소셜 로그인도 같은 규칙이다
    for good in (
        "minsu.kim+work@mail.example.co.kr",
        "o'neil_99@sub-domain.example.org",
        "a@b.io",
        "a" * 64 + "@example.com",
    ):
        assert normalize_email(good.upper()) == good and clean_email(f" {good} ") == good
    assert clean_email(None) is None and clean_email(123) is None
    assert username_seed("Minsu.Kim") == "minsu_kim"
    assert username_seed("가") == "user" and username_seed("__") == "user" and username_seed("ab") == "user"
    assert len(username_seed("a" * 80)) == 24


def test_email_signup_creates_active_user_and_login_by_email_or_username() -> None:
    client = _client()
    r = client.post(
        "/v1/auth/signup",
        json={"email": "  Minsu.Kim@Example.com ", "password": PW, "display_name": " 김   민수 "},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["email"], body["display_name"], body["role"], body["status"], body["is_admin"]) == (
        "minsu.kim@example.com",  # 소문자로 저장한다
        "김 민수",
        "user",
        "active",
        False,
    )
    assert body["username"] == "minsu_kim" and body["csrf_token"] and r.cookies.get("pi_session")
    assert client.get("/v1/contacts").status_code == 200
    me = client.get("/v1/auth/me").json()
    assert me["providers"] == ["password"] and me["email"] == "minsu.kim@example.com"

    # 같은 이메일(대소문자 달라도) 409, 같은 앞부분의 다른 이메일은 아이디에 꼬리가 붙는다.
    dup = TestClient(client.app).post(
        "/v1/auth/signup", json={"email": "MINSU.KIM@example.com", "password": PW}
    )
    assert _code(dup) == (409, "conflict")
    other = TestClient(client.app).post(
        "/v1/auth/signup", json={"email": "minsu.kim@other.org", "password": PW}
    )
    assert other.status_code == 201 and other.json()["username"].startswith("minsu_kim_")

    # 로그인: 이메일(대소문자 무시)·아이디 모두. 쿠키 로그인도 토큰 발급도.
    fresh = TestClient(client.app)
    by_email = fresh.post("/v1/auth/login", json={"username": "Minsu.Kim@EXAMPLE.com", "password": PW})
    assert by_email.status_code == 200 and by_email.json()["user_id"] == body["user_id"]
    assert fresh.post("/v1/auth/login", json={"username": "minsu_kim", "password": PW}).status_code == 200
    token = fresh.post("/v1/auth/token", json={"username": "minsu.kim@example.com", "password": PW})
    assert token.status_code == 200 and token.json()["username"] == "minsu_kim"
    # 틀린 비밀번호와 없는 이메일은 본문까지 같은 401 (이메일이 있는지 알려 주지 않는다)
    wrong = fresh.post(
        "/v1/auth/login", json={"username": "minsu.kim@example.com", "password": "wrong-pass-1"}
    )
    unknown = fresh.post(
        "/v1/auth/login", json={"username": "nobody@example.com", "password": "wrong-pass-1"}
    )
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()


def test_email_signup_validation() -> None:
    client = _client()
    for bad in ("not-an-email", "a@b", "two@@example.com", "sp ace@example.com", "x" * 250 + "@example.com"):
        assert client.post("/v1/auth/signup", json={"email": bad, "password": PW}).status_code == 422, bad
    short = client.post("/v1/auth/signup", json={"email": "ok@example.com", "password": "short"})
    assert short.status_code == 422
    long_name = client.post(
        "/v1/auth/signup", json={"email": "ok@example.com", "password": PW, "display_name": "가" * 41}
    )
    assert long_name.status_code == 422
    assert asyncio.run(_state(client).auth.list_users())[0] == 0  # 아무 계정도 만들어지지 않았다


def test_email_signup_honours_closed_invite_and_consent_gates() -> None:
    closed = _client(auth_allow_register=False, register_invite_code="come-in-2026")
    r = closed.post(
        "/v1/auth/signup", json={"email": "ok@example.com", "password": PW, "invite_code": "come-in-2026"}
    )
    assert _code(r) == (403, "forbidden")  # 닫혀 있으면 초대 코드가 맞는지조차 알려 주지 않는다

    strict = _client(register_invite_code="come-in-2026", require_consent=True, consent_version="2026-12-01")
    body = {"email": "ok@example.com", "password": PW}
    assert _code(strict.post("/v1/auth/signup", json=body)) == (403, "invalid_invite")
    assert _code(strict.post("/v1/auth/signup", json={**body, "invite_code": "nope"})) == (
        403,
        "invalid_invite",
    )
    no_consent = strict.post("/v1/auth/signup", json={**body, "invite_code": "come-in-2026"})
    assert _code(no_consent) == (422, "consent_required")
    ok = strict.post("/v1/auth/signup", json={**body, "invite_code": "come-in-2026", "consent": True})
    assert (
        ok.status_code == 201
        and ok.json()["consent_version"] == "2026-12-01"
        and ok.json()["consent_current"]
    )

    # 초대 코드 추측은 아이디 가입과 같은 실패 제한에 걸린다
    guess = _client(register_invite_code="come-in-2026", auth_max_failures=2)
    codes = [
        guess.post("/v1/auth/signup", json={**body, "invite_code": f"guess-{i}"}).status_code
        for i in range(3)
    ]
    assert codes == [403, 403, 429]


def test_signup_and_oauth_paths_are_rate_limited_like_login() -> None:
    for entry in (
        ("POST", "/v1/auth/signup"),
        ("GET", "/v1/auth/oauth/google/start"),
        ("POST", "/v1/auth/oauth/google/start"),
        ("GET", "/v1/auth/oauth/google/callback"),
        ("GET", "/v1/auth/oauth/kakao/start"),
        ("POST", "/v1/auth/oauth/kakao/start"),
        ("GET", "/v1/auth/oauth/kakao/callback"),
    ):
        assert entry in AUTH_LIMITED, entry
    client = _client(rate_limit_auth_per_minute=2)
    _fake(client, "kakao", OAuthProfile(subject="k-1"))
    statuses = [
        client.post("/v1/auth/signup", json={"email": f"u{i}@example.com", "password": PW}).status_code
        for i in range(3)
    ]
    assert statuses == [201, 201, 429]
    blocked = client.get("/v1/auth/oauth/kakao/start", follow_redirects=False)
    assert _code(blocked) == (429, "rate_limited")
    assert _code(client.get("/v1/auth/oauth/kakao/callback", follow_redirects=False)) == (429, "rate_limited")


def test_generated_username_never_takes_an_admin_name() -> None:
    client = _client(admin_usernames="Boss,ops_lead")
    r = client.post("/v1/auth/signup", json={"email": "boss@example.com", "password": PW})
    assert r.status_code == 201 and r.json()["username"].lower() != "boss" and r.json()["is_admin"] is False
    assert r.json()["username"].startswith("boss_")
    assert client.get("/v1/admin/overview").status_code == 403
    ops = TestClient(client.app).post(
        "/v1/auth/signup", json={"email": "OPS_LEAD@example.com", "password": PW}
    )
    assert ops.json()["username"] != "ops_lead" and ops.json()["is_admin"] is False
    # 짧거나 기호뿐인 앞부분은 기본 이름으로 (한글 앞부분은 이메일 검사에서 422 — test_email_helpers)
    for email in ("k@example.com", "+_+@example.com"):
        short = TestClient(client.app).post("/v1/auth/signup", json={"email": email, "password": PW})
        assert short.status_code == 201 and short.json()["username"].startswith("user"), email


def test_login_by_email_shares_the_throttle_and_does_not_reveal_existence() -> None:
    client = _client(auth_max_failures=3)
    client.post("/v1/auth/signup", json={"email": "real@example.com", "password": PW})
    client.cookies.clear()

    def attempts(identifier: str, path: str = "/v1/auth/login") -> list[tuple[int, str]]:
        return [
            _code(client.post(path, json={"username": identifier, "password": "wrong-pass-1"}))
            for _ in range(4)
        ]

    expected = [(401, "unauthorized")] * 3 + [(429, "too_many_attempts")]
    assert attempts("real@example.com") == expected  # 있는 이메일
    assert attempts("ghost@example.com") == expected  # 없는 이메일도 똑같이 잠긴다
    # 잠긴 뒤에는 맞는 비밀번호도 429 — 쿠키 로그인과 토큰 발급이 같은 기록을 쓴다
    assert (
        client.post("/v1/auth/login", json={"username": "real@example.com", "password": PW}).status_code
        == 429
    )
    assert (
        client.post("/v1/auth/token", json={"username": "REAL@example.com", "password": PW}).status_code
        == 429
    )
    # 토큰 발급으로 틀려도 같은 한도
    assert attempts("other@example.com", "/v1/auth/token") == expected


@pytest.mark.anyio
async def test_email_login_spends_one_hash_for_missing_and_passwordless_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import auth_service as module

    repo = InMemoryAuthRepo()
    service = AuthService(_settings(), repo)
    await service.signup_email("real@example.com", PW)
    await repo.create_user("social_only", None, email="social@example.com", email_verified=True)
    calls: list[str] = []
    real = module.verify_password_async

    async def counting(password: str, stored: str) -> bool:
        calls.append(stored)
        return await real(password, stored)

    monkeypatch.setattr(module, "verify_password_async", counting)
    for identifier in ("real@example.com", "ghost@example.com", "social@example.com", "social_only"):
        with pytest.raises(DomainError) as raised:
            await service.login(identifier, "wrong-pass-1")
        assert raised.value.status_code == 401
    assert len(calls) == 4 and all(c.startswith("scrypt$") for c in calls)  # 경우마다 해시 한 번


# ---------- 승인제 ----------


def test_approval_gate_creates_pending_account_without_a_session() -> None:
    client = _client(auth_signup_requires_approval=True)
    for path, body in (
        ("/v1/auth/register", {"username": "old_style", "password": PW}),
        ("/v1/auth/signup", {"email": "wait@example.com", "password": PW}),
    ):
        fresh = TestClient(client.app)
        r = fresh.post(path, json=body)
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "pending" and set(r.json()) == {"status", "username", "email"}
        assert "set-cookie" not in r.headers and fresh.get("/v1/auth/me").status_code == 401
    state = _state(client)
    total, users = asyncio.run(state.auth.list_users(status="pending"))
    assert total == 2 and {u.username for u in users} == {"old_style", "wait"}

    # 대기 계정은 로그인할 수 없다. 비밀번호가 틀리면 대기 중인지도 알려 주지 않는다.
    for identifier in ("old_style", "wait@example.com"):
        ok_pw = client.post("/v1/auth/login", json={"username": identifier, "password": PW})
        assert _code(ok_pw) == (403, "account_pending") and "set-cookie" not in ok_pw.headers
        assert _code(client.post("/v1/auth/token", json={"username": identifier, "password": PW})) == (
            403,
            "account_pending",
        )
        bad_pw = client.post("/v1/auth/login", json={"username": identifier, "password": "wrong-pass-1"})
        assert _code(bad_pw) == (401, "unauthorized")

    # 승인하면 로그인된다
    target = next(u for u in users if u.username == "wait")
    asyncio.run(state.member_admin.update_member(Log(), uuid.uuid4(), target.id, status="active"))
    ok = client.post("/v1/auth/login", json={"username": "wait@example.com", "password": PW})
    assert ok.status_code == 200 and ok.json()["status"] == "active"
    assert client.get("/v1/contacts").status_code == 200


def test_server_side_accounts_skip_the_approval_gate() -> None:
    client = _client(auth_signup_requires_approval=True, auth_allow_register=False)
    service = _service(client)
    made = asyncio.run(service.create_account("operator", PW))
    assert (made.status, made.role) == ("active", "user")
    assert client.post("/v1/auth/login", json={"username": "operator", "password": PW}).status_code == 200


# ---------- 정지: 로그인 차단, 세션·Bearer 즉시 차단 ----------


def _logged_in_everywhere(
    client: TestClient, email: str
) -> tuple[str, TestClient, dict[str, str], dict[str, str]]:
    """가입(쿠키 세션) + 토큰 발급(Bearer). (user_id, 쿠키 클라이언트, CSRF 헤더, Bearer 헤더)."""
    browser = TestClient(client.app)
    signed = browser.post("/v1/auth/signup", json={"email": email, "password": PW})
    assert signed.status_code == 201, signed.text
    token = TestClient(client.app).post("/v1/auth/token", json={"username": email, "password": PW})
    return (
        signed.json()["user_id"],
        browser,
        {"X-CSRF-Token": signed.json()["csrf_token"]},
        _bearer(token.json()["access_token"]),
    )


def _all_dead(client: TestClient, browser: TestClient, csrf: dict[str, str], bearer: dict[str, str]) -> None:
    api = TestClient(client.app)
    for r in (
        browser.get("/v1/auth/me"),
        browser.get("/v1/contacts"),
        browser.post("/v1/contacts", json=CONTACT, headers=csrf),
        browser.get("/v1/auth/sessions"),
        api.get("/v1/auth/me", headers=bearer),
        api.get("/v1/contacts", headers=bearer),
        api.post("/v1/contacts", json=CONTACT, headers=bearer),
        api.get("/v1/admin/overview", headers=bearer),
    ):
        assert _code(r) == (401, "unauthorized"), r.request.url


def test_suspension_cuts_cookie_sessions_and_bearer_tokens_at_once() -> None:
    client = _client()
    uid, browser, csrf, bearer = _logged_in_everywhere(client, "bad@example.com")
    assert browser.post("/v1/contacts", json=CONTACT, headers=csrf).status_code == 201
    assert TestClient(client.app).get("/v1/contacts", headers=bearer).status_code == 200
    state = _state(client)

    log = Log()
    out = asyncio.run(state.member_admin.update_member(log, uuid.uuid4(), UUID(uid), status="suspended"))
    assert out.status == "suspended" and log.actions == ["member_suspend", "member_suspend_done"]
    assert log.lines[1][1].endswith("sessions=2")  # 결과 줄: 가입 때 열린 세션 + Bearer 토큰을 끊었다
    _all_dead(client, browser, csrf, bearer)

    # 정지 계정은 로그인할 수 없다 (쿠키 로그인·토큰 발급 모두). 비밀번호가 틀리면 정지 여부를 알리지 않는다.
    for path in ("/v1/auth/login", "/v1/auth/token"):
        denied = client.post(path, json={"username": "bad@example.com", "password": PW})
        assert _code(denied) == (403, "account_suspended") and "set-cookie" not in denied.headers
        wrong = client.post(path, json={"username": "bad@example.com", "password": "nope-nope-1"})
        assert _code(wrong) == (401, "unauthorized")

    # 정지를 풀어도 예전 세션·토큰은 살아나지 않는다 (폐기됐다). 새 로그인은 된다.
    asyncio.run(state.member_admin.update_member(log, uuid.uuid4(), UUID(uid), status="active"))
    _all_dead(client, browser, csrf, bearer)
    assert (
        client.post("/v1/auth/login", json={"username": "bad@example.com", "password": PW}).status_code == 200
    )


def test_non_active_status_alone_kills_sessions_even_without_revocation() -> None:
    """세션 폐기를 건너뛰고 상태만 바뀌어도(다른 경로·경합) 쿠키와 Bearer가 다음 요청부터 끊긴다."""
    client = _client()
    uid, browser, csrf, bearer = _logged_in_everywhere(client, "raced@example.com")
    repo = _state(client).auth
    for status in ("suspended", "pending"):
        asyncio.run(repo.update_user(UUID(uid), status=status))
        _all_dead(client, browser, csrf, bearer)
        asyncio.run(repo.update_user(UUID(uid), status="active"))
        assert browser.get("/v1/auth/me").status_code == 200  # 폐기된 적이 없으니 되살아난다
        assert TestClient(client.app).get("/v1/contacts", headers=bearer).status_code == 200


# ---------- 소셜 로그인: 시작 ----------


def test_oauth_start_redirects_to_provider_with_state_cookie_and_pkce() -> None:
    client = _client(
        google_client_id="gid.apps",
        google_client_secret="gsecret",
        kakao_client_id="kakao-rest-key",
        kakao_client_secret="ksecret",
        public_base_url="https://persona.example.com/",
    )
    r, state = _start(client, "google", "/admin")
    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert (q["client_id"], q["response_type"], q["scope"]) == ("gid.apps", "code", "openid email profile")
    assert q["redirect_uri"] == "https://persona.example.com/v1/auth/oauth/google/callback"
    assert len(state) >= 43 and q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert "gsecret" not in r.headers["location"]
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"pi_oauth={state}") and "HttpOnly" in cookie and "Max-Age=600" in cookie
    assert "samesite=lax" in cookie.lower() and "Path=/v1/auth/oauth" in cookie
    # 쿠키에는 state뿐이다 — verifier·돌아갈 경로는 서버에만 있다
    assert "/admin" not in cookie and "verifier" not in cookie.lower()

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
    assert k_state != state and "ksecret" not in k.headers["location"]
    assert len({_start(client, "kakao")[1] for _ in range(20)}) == 20  # 매번 다른 state

    assert _code(client.get("/v1/auth/oauth/naver/start", follow_redirects=False)) == (404, "not_found")
    secure = _client(kakao_client_id="k", kakao_client_secret="s", session_cookie_secure=True)
    assert "Secure" in secure.get("/v1/auth/oauth/kakao/start", follow_redirects=False).headers["set-cookie"]
    # 공개 주소가 없으면 요청 주소로 콜백을 만든다 (로컬 개발).
    local = _client(kakao_client_id="k", kakao_client_secret="s")
    location = local.get("/v1/auth/oauth/kakao/start", follow_redirects=False).headers["location"]
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fv1%2Fauth%2Foauth%2Fkakao%2Fcallback" in location

    # POST 시작: 주소를 본문으로, 같은 쿠키
    posted = client.post("/v1/auth/oauth/google/start", json={"next": "/admin", "consent": True})
    assert posted.status_code == 200 and set(posted.json()) == {"authorize_url"}
    assert posted.json()["authorize_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert posted.headers["set-cookie"].startswith("pi_oauth=")
    assert client.post("/v1/auth/oauth/google/start").status_code == 200  # 본문 없이도


def test_state_store_is_single_use_and_short_lived() -> None:
    now = [1000.0]
    store = OAuthStateStore(ttl_seconds=600, max_entries=3, clock=lambda: now[0])
    pending = PendingLogin(provider="google", next_path="/", code_verifier="v")
    state = store.issue(pending)
    assert len(state) >= 43 and state not in repr(store._entries)  # 원문이 아니라 해시로 보관한다
    assert store.consume("wrong") is None and store.consume(None) is None and store.consume("") is None
    assert store.consume(state) == pending
    assert store.consume(state) is None  # 일회용

    late = store.issue(pending)
    now[0] += 599
    fresh = store.issue(pending)
    now[0] += 1  # late는 정확히 600초 = 만료
    assert store.consume(late) is None and store.consume(fresh) == pending

    assert store.consume("x" * 5000) is None and len(store) == 0


def test_state_store_never_evicts_another_clients_pending_login() -> None:
    """가득 찼을 때: 만료분을 치우고, 클라이언트는 자기 것만 밀어내고, 그래도 가득이면 새로 내주지 않는다."""
    now = [1000.0]
    store = OAuthStateStore(ttl_seconds=600, max_entries=5, clock=lambda: now[0], per_client=2)
    pending = PendingLogin(provider="google", next_path="/")
    victim = store.issue(pending, "198.51.100.7")

    # 한 곳에서 아무리 시작해도 차지하는 자리는 per_client개다 — 자기 것 중 오래된 것만 사라진다
    flood = [store.issue(pending, "203.0.113.9") for _ in range(50)]
    assert len(store) == 3
    assert [store.consume(s) is not None for s in flood[-3:]] == [False, True, True]
    assert store.consume(flood[0]) is None

    # 주소를 바꿔 가며 전체 상한까지 채워도 먼저 시작한 사람의 로그인은 살아 있다. 새 state는 거절된다.
    for i in range(4):
        store.issue(pending, f"192.0.2.{i}")
    assert len(store) == 5
    with pytest.raises(OAuthStateStoreFullError) as full:
        store.issue(pending, "192.0.2.99")
    assert full.value.retry_after == 601  # 가장 먼저 만료되는 보관분이 사라질 때까지
    with pytest.raises(OAuthStateStoreFullError):
        store.issue(pending)  # 클라이언트 키 없이 불러도 같다
    assert len(store) == 5
    assert store.consume(victim) == pending

    # 만료된 것은 새로 넣기 전에 먼저 치운다 — 가득 차 있어도 만료분만큼 자리가 난다
    now[0] += 600
    assert store.consume(store.issue(pending, "192.0.2.99")) == pending and len(store) == 0


def test_oauth_start_flood_cannot_kill_someone_elses_login_and_full_store_refuses() -> None:
    client = _client()
    _fake(client, "google", OAuthProfile(subject="g-1", email="a@example.com", email_verified=True))
    state = _state(client)
    victim = TestClient(client.app, client=("198.51.100.7", 40000))
    _, victim_state = _start(victim, "google", "/admin")

    # 로그인하지 않은 한 주소가 시작만 계속 부른다 → 그 주소의 자리(20개)만 돈다
    attacker = TestClient(client.app, client=("203.0.113.9", 50000))
    for _ in range(MAX_PENDING_STATES_PER_CLIENT * 3):
        assert attacker.get("/v1/auth/oauth/google/start", follow_redirects=False).status_code == 302
    assert len(state.oauth_states) == MAX_PENDING_STATES_PER_CLIENT + 1
    done = _callback(victim, "google", code="good-code", state=victim_state)
    assert done.headers["location"] == "/admin" and _has_session_cookie(done)

    # 보관소가 가득 찬 서버: POST는 429, GET은 화면으로 돌려보낸다. 이미 시작한 로그인은 그대로 끝난다.
    state.oauth_states = OAuthStateStore(max_entries=1)
    late = TestClient(client.app, client=("198.51.100.8", 40000))
    _, kept = _start(late, "google")
    other = TestClient(client.app, client=("192.0.2.44", 40000))
    refused = other.post("/v1/auth/oauth/google/start", json={})
    assert _code(refused) == (429, "too_many_attempts") and int(refused.headers["retry-after"]) >= 1
    assert "set-cookie" not in refused.headers
    redirected = other.get("/v1/auth/oauth/google/start", params={"next": "/m"}, follow_redirects=False)
    assert redirected.status_code == 302 and redirected.headers["location"] == "/m?auth_error=oauth_failed"
    assert "set-cookie" not in redirected.headers  # 제공자로 보내지 않고 state 쿠키도 심지 않는다
    outside = other.get(
        "/v1/auth/oauth/google/start", params={"next": "//evil.example"}, follow_redirects=False
    )
    assert outside.headers["location"] == "/?auth_error=oauth_failed"
    assert len(state.oauth_states) == 1
    assert _callback(late, "google", code="good-code", state=kept).headers["location"] == "/"


# ---------- 소셜 로그인: 콜백 ----------


def test_oauth_callback_google_creates_user_sets_session_and_returns_to_next() -> None:
    client = _client(public_base_url="https://persona.example.com")
    google = _fake(
        client,
        "google",
        OAuthProfile(subject="g-1", email="minsu@example.com", email_verified=True, name="김민수"),
    )
    _, state = _start(client, "google", "/admin?tab=1")
    done = _callback(client, "google", code="good-code", state=state)
    assert done.status_code == 302 and done.headers["location"] == "/admin?tab=1"
    cookies = done.headers.get_list("set-cookie")
    assert any(c.startswith("pi_session=") and "HttpOnly" in c for c in cookies)
    assert any(c.startswith('pi_oauth=""') and "Max-Age=0" in c for c in cookies)  # 한 번 쓰고 지운다
    # 제공자에는 같은 redirect_uri와, 시작 때 만든 verifier가 간다 (challenge = S256(verifier)).
    call = google.calls[0]
    assert call["redirect_uri"] == "https://persona.example.com/v1/auth/oauth/google/callback"
    assert call["code_verifier"] and pkce_challenge(call["code_verifier"]) == google.challenge
    assert "good-code" not in done.headers["location"] and state not in done.headers["location"]

    me = client.get("/v1/auth/me").json()
    assert (me["email"], me["display_name"], me["status"], me["providers"]) == (
        "minsu@example.com",
        "김민수",
        "active",
        ["google"],
    )
    assert me["username"] == "minsu" and client.get("/v1/contacts").status_code == 200
    user = asyncio.run(_state(client).auth.get_user(UUID(me["user_id"])))
    assert user.email_verified is True and user.has_password is False and user.last_login_at is not None

    # 다시 로그인하면 같은 사용자 (이메일이 바뀌어도 subject로 찾는다). 가입이 닫혀도 기존 계정은 들어온다.
    google.profile = OAuthProfile(subject="g-1", email="changed@example.com", email_verified=True)
    _state(client).settings.auth_allow_register = False
    second = TestClient(client.app)
    assert _social_login(second, "google").headers["location"] == "/"
    assert second.get("/v1/auth/me").json()["user_id"] == me["user_id"]
    assert second.get("/v1/auth/me").json()["email"] == "minsu@example.com"  # 계정의 이메일은 그대로다
    assert asyncio.run(_state(client).auth.list_users())[0] == 1


def test_oauth_kakao_without_email_and_social_only_account_rules() -> None:
    client = _client()
    kakao = _fake(client, "kakao", OAuthProfile(subject="987654321", name="민수"))
    done = _social_login(client, "kakao")
    assert done.status_code == 302 and done.headers["location"] == "/"
    assert kakao.calls[0]["code_verifier"] is None  # 카카오는 PKCE를 쓰지 않는다
    me = client.get("/v1/auth/me").json()
    assert (me["email"], me["display_name"], me["status"], me["providers"]) == (
        None,
        "민수",
        "active",
        ["kakao"],
    )
    assert me["username"] == "kakao"
    # 이메일 없는 계정이 여럿이어도 된다 (아이디만 겹치지 않게).
    kakao.profile = OAuthProfile(subject="111")
    other = TestClient(client.app)
    assert _social_login(other, "kakao").headers["location"] == "/"
    assert other.get("/v1/auth/me").json()["username"].startswith("kakao_")

    # 소셜 전용 계정: 비밀번호 로그인·비밀번호 변경은 없는 계정과 같은 401, 탈퇴는 세션+CSRF로.
    csrf = {"X-CSRF-Token": me["csrf_token"]}
    login = TestClient(client.app).post("/v1/auth/login", json={"username": "kakao", "password": PW})
    ghost = TestClient(client.app).post("/v1/auth/login", json={"username": "nobody_x", "password": PW})
    assert login.status_code == 401 and login.json() == ghost.json()
    change = client.post(
        "/v1/auth/password", json={"current_password": "", "new_password": "brand-new-pw-1"}, headers=csrf
    )
    assert change.status_code in (401, 422)
    change = client.post(
        "/v1/auth/password", json={"current_password": "x", "new_password": "brand-new-pw-1"}, headers=csrf
    )
    assert _code(change) == (401, "unauthorized")
    assert client.request("DELETE", "/v1/auth/me", json={"password": ""}).status_code == 403  # CSRF 없음
    assert client.request("DELETE", "/v1/auth/me", json={"password": ""}, headers=csrf).status_code == 200
    assert client.get("/v1/auth/me").status_code == 401
    assert asyncio.run(_state(client).auth.find_identity("kakao", "987654321")) is None  # 연결도 지워졌다


def test_password_account_still_needs_its_password_to_delete() -> None:
    client = _client()
    r = client.post("/v1/auth/signup", json={"email": "keep@example.com", "password": PW})
    csrf = {"X-CSRF-Token": r.json()["csrf_token"]}
    for body in ({"password": ""}, {}, {"password": "wrong-pass-1"}):
        assert client.request("DELETE", "/v1/auth/me", json=body, headers=csrf).status_code == 401
    assert client.get("/v1/auth/me").status_code == 200


def test_oauth_callback_rejects_forgery_replay_expiry_denial_and_provider_failure() -> None:
    client = _client()
    google = _fake(client, "google", OAuthProfile(subject="g-1", email="a@example.com", email_verified=True))
    _fake(client, "kakao", OAuthProfile(subject="k-1"))

    def failed(r: httpx.Response, code: str, next_path: str = "/") -> bool:
        cleared = any(c.startswith('pi_oauth=""') for c in r.headers.get_list("set-cookie"))
        return (
            r.status_code == 302
            and r.headers["location"] == with_auth_error(next_path, code)
            and cleared
            and not _has_session_cookie(r)
        )

    # state 불일치(위조) → 실패. 보관분은 건드리지 않지만 쿠키가 지워져 이 브라우저에서는 끝난다.
    _, state = _start(client, "google", "/admin")
    assert failed(_callback(client, "google", code="good-code", state="forged-state"), "oauth_failed")
    assert failed(_callback(client, "google", code="good-code", state=state), "oauth_failed")
    # 쿠키 없는 브라우저(공격자가 만든 링크를 피해자가 연 경우) — 맞는 state여도 실패
    _, state = _start(client, "google")
    victim = TestClient(client.app)
    assert failed(_callback(victim, "google", code="good-code", state=state), "oauth_failed")
    assert victim.get("/v1/auth/me").status_code == 401
    # state 없음
    _start(client, "google")
    assert failed(_callback(client, "google", code="good-code"), "oauth_failed")
    # google로 시작한 state를 kakao 콜백에 쓰면 실패 (그리고 그 state는 소모된다)
    _, state = _start(client, "google")
    assert failed(_callback(client, "kakao", code="good-code", state=state), "oauth_failed")
    assert google.calls == []  # 여기까지 제공자를 한 번도 부르지 않았다

    # 재사용: 성공한 state를 쿠키까지 되살려 다시 보내도 실패한다 (서버에서 한 번만 꺼낸다)
    _, state = _start(client, "google", "/admin")
    assert _callback(client, "google", code="good-code", state=state).headers["location"] == "/admin"
    client.cookies.set("pi_oauth", state, domain="testserver.local", path="/v1/auth/oauth")
    replay = _callback(client, "google", code="good-code", state=state)
    assert failed(replay, "oauth_failed") and len(google.calls) == 1
    client.cookies.clear()

    # 만료: 10분이 지난 state
    now = [0.0]
    _state(client).oauth_states = OAuthStateStore(clock=lambda: now[0])
    _, state = _start(client, "google")
    now[0] += 601
    assert failed(_callback(client, "google", code="good-code", state=state), "oauth_failed")
    assert len(google.calls) == 1

    # 사용자가 동의를 취소, 코드 없음, 제공자 교환 실패 — 시작할 때의 next로 돌아간다
    _, state = _start(client, "google", "/admin")
    denied = _callback(
        client, "google", error="access_denied", error_description="ZQPROVIDERTEXT 사용자가 거부", state=state
    )
    assert failed(denied, "oauth_denied", "/admin") and "ZQPROVIDERTEXT" not in denied.headers["location"]
    _, state = _start(client, "google", "/admin")
    assert failed(_callback(client, "google", state=state), "oauth_failed", "/admin")
    _, state = _start(client, "google", "/admin?tab=1")
    assert failed(_callback(client, "google", code="bad-code", state=state), "oauth_failed", "/admin?tab=1")
    _, state = _start(client, "google")
    assert failed(_callback(client, "google", code="c" * 3000, state=state), "oauth_failed")
    assert client.get("/v1/auth/me").status_code == 401
    # state 없이 온 error는 신뢰하지 않는다 (oauth_denied가 아니라 oauth_failed)
    assert failed(_callback(TestClient(client.app), "google", error="access_denied"), "oauth_failed")


def test_oauth_links_only_verified_email_to_verified_account() -> None:
    client = _client()
    repo = _state(client).auth

    # 구글로 만든 계정(이메일 확인됨)에 같은 확인된 이메일의 카카오가 연결된다.
    owner = TestClient(client.app)
    _fake(client, "google", OAuthProfile(subject="g-owner", email="Owner@Example.com", email_verified=True))
    assert _social_login(owner, "google").headers["location"] == "/"
    owner_id = owner.get("/v1/auth/me").json()["user_id"]
    _fake(client, "kakao", OAuthProfile(subject="k-owner", email="owner@example.com", email_verified=True))
    linked = TestClient(client.app)
    assert _social_login(linked, "kakao").headers["location"] == "/"
    me = linked.get("/v1/auth/me").json()
    assert me["user_id"] == owner_id and me["providers"] == ["google", "kakao"]

    # 확인되지 않은 이메일은 연결하지 않는다 → 이메일 없는 새 계정 (남의 주소를 적어 계정을 가로챌 수 없다).
    attacker = TestClient(client.app)
    _fake(
        client, "kakao", OAuthProfile(subject="k-attacker", email="owner@example.com", email_verified=False)
    )
    assert _social_login(attacker, "kakao").headers["location"] == "/"
    stranger = attacker.get("/v1/auth/me").json()
    assert stranger["user_id"] != owner_id and (stranger["email"], stranger["providers"]) == (None, ["kakao"])
    assert owner.get("/v1/auth/me").json()["providers"] == ["google", "kakao"]

    # 한 소셜 계정(provider+subject)은 한 사용자에게만 묶인다
    from app.core.errors import VersionConflictError

    with pytest.raises(VersionConflictError):
        asyncio.run(repo.add_identity(UUID(stranger["user_id"]), "google", "g-owner"))
    with pytest.raises(VersionConflictError):  # 한 사용자가 같은 제공자의 계정 둘을 묶지도 못한다
        asyncio.run(repo.add_identity(UUID(owner_id), "google", "g-second"))
    assert asyncio.run(repo.find_identity("google", "g-owner")).user_id == UUID(owner_id)


def test_oauth_never_links_to_an_account_whose_email_was_not_verified() -> None:
    """이메일 가입은 주소 소유를 확인하지 않는다 — 남이 먼저 가입해 둔 계정에 주인이 딸려 들어가면 안 된다."""
    client = _client()
    squatter = TestClient(client.app)
    signed = squatter.post("/v1/auth/signup", json={"email": "victim@example.com", "password": PW}).json()

    victim = TestClient(client.app)
    _fake(client, "google", OAuthProfile(subject="g-victim", email="victim@example.com", email_verified=True))
    r = _social_login(victim, "google", "/admin")
    assert r.headers["location"] == "/admin?auth_error=email_in_use" and not _has_session_cookie(r)
    assert victim.get("/v1/auth/me").status_code == 401
    assert squatter.get("/v1/auth/me").json()["providers"] == ["password"]  # 아무것도 묶이지 않았다
    assert asyncio.run(_state(client).auth.find_identity("google", "g-victim")) is None
    assert asyncio.run(_state(client).auth.list_users())[0] == 1 and signed["email"] == "victim@example.com"


def test_oauth_new_account_honours_closed_invite_consent_and_approval_gates() -> None:
    profile = OAuthProfile(subject="g-new", email="new@example.com", email_verified=True)

    def users(c: TestClient) -> int:
        return asyncio.run(_state(c).auth.list_users())[0]

    # 가입 닫힘: 새 계정은 못 만든다. 서버에서 만든 계정은 영향 없다.
    closed = _client(auth_allow_register=False)
    _fake(closed, "google", profile)
    r = _social_login(closed, "google", "/admin")
    assert r.headers["location"] == "/admin?auth_error=signup_closed" and not _has_session_cookie(r)
    assert users(closed) == 0 and closed.get("/v1/auth/me").status_code == 401

    # 초대 코드: 코드 없이 시작한 새 사용자는 못 들어온다. 틀린 코드는 시작에서 403, 맞는 코드는 가입된다.
    invited = _client(register_invite_code="come-in-2026", auth_max_failures=2)
    _fake(invited, "google", profile)
    assert _social_login(invited, "google").headers["location"] == "/?auth_error=invalid_invite"
    state = _start_post(invited, "google", next="/admin")  # 코드를 안 보냄
    assert _callback(invited, "google", code="good-code", state=state).headers["location"] == (
        "/admin?auth_error=invalid_invite"
    )
    assert users(invited) == 0
    wrong = [
        invited.post("/v1/auth/oauth/google/start", json={"invite_code": f"guess-{i}"}) for i in range(3)
    ]
    assert [_code(w) for w in wrong] == [
        (403, "invalid_invite"),
        (403, "invalid_invite"),
        (429, "too_many_attempts"),
    ]
    assert all("set-cookie" not in w.headers for w in wrong)
    ok = TestClient(invited.app, client=("203.0.113.9", 50000))  # 잠기지 않은 다른 주소에서
    _fake(invited, "google", profile)
    state = _start_post(ok, "google", invite_code="come-in-2026")
    assert _callback(ok, "google", code="good-code", state=state).headers["location"] == "/"
    assert users(invited) == 1 and ok.get("/v1/auth/me").json()["email"] == "new@example.com"
    # 이미 가입한 사용자는 초대 코드 없이 로그인된다
    again = TestClient(invited.app)
    assert _social_login(again, "google").headers["location"] == "/"

    # 동의: 동의 없이 시작한 새 사용자는 못 들어온다. 동의하고 시작하면 버전이 기록된다.
    consent = _client(require_consent=True, consent_version="2026-12-01")
    _fake(consent, "google", profile)
    assert _social_login(consent, "google").headers["location"] == "/?auth_error=consent_required"
    assert users(consent) == 0
    state = _start_post(consent, "google", consent=True)
    assert _callback(consent, "google", code="good-code", state=state).headers["location"] == "/"
    me = consent.get("/v1/auth/me").json()
    assert (me["consent_version"], me["consent_current"]) == ("2026-12-01", True)

    # 승인제: 계정은 pending으로 만들어지고 세션은 없다. 승인 뒤에 들어온다.
    gated = _client(auth_signup_requires_approval=True)
    _fake(gated, "google", profile)
    r = _social_login(gated, "google", "/admin")
    assert r.headers["location"] == "/admin?auth_error=account_pending" and not _has_session_cookie(r)
    assert gated.get("/v1/auth/me").status_code == 401
    total, pending = asyncio.run(_state(gated).auth.list_users(status="pending"))
    assert total == 1 and pending[0].email == "new@example.com"
    assert _social_login(gated, "google").headers["location"] == "/?auth_error=account_pending"  # 다시 와도
    assert users(gated) == 1
    asyncio.run(_state(gated).member_admin.update_member(Log(), uuid.uuid4(), pending[0].id, status="active"))
    assert _social_login(gated, "google").headers["location"] == "/"
    assert gated.get("/v1/auth/me").json()["status"] == "active"


def test_oauth_suspended_user_is_redirected_without_a_session() -> None:
    client = _client()
    _fake(client, "kakao", OAuthProfile(subject="k-1"))
    member = TestClient(client.app)
    _social_login(member, "kakao")
    uid = member.get("/v1/auth/me").json()["user_id"]
    asyncio.run(_state(client).member_admin.update_member(Log(), uuid.uuid4(), UUID(uid), status="suspended"))
    assert member.get("/v1/auth/me").status_code == 401
    r = _social_login(member, "kakao")
    assert r.headers["location"] == "/?auth_error=account_suspended" and not _has_session_cookie(r)
    assert member.get("/v1/auth/me").status_code == 401


def test_safe_next_allows_only_same_origin_paths() -> None:
    assert safe_next("/admin?tab=1#x") == "/admin?tab=1#x"
    for bad in (
        None,
        "",
        "admin",
        "//evil.example",
        "/\\evil.example",
        "\\\\evil.example",
        "https://evil.example",
        "javascript:alert(1)",
        "/a\nb",
        "/a\rb",
        "/a\tb",
        "/" + "a" * 600,
    ):
        assert safe_next(bad) == "/", bad
    assert with_auth_error("/", "oauth_denied") == "/?auth_error=oauth_denied"
    assert with_auth_error("/admin?tab=1", "oauth_failed") == "/admin?tab=1&auth_error=oauth_failed"
    assert with_auth_error("/admin#users", "signup_closed") == "/admin?auth_error=signup_closed#users"

    client = _client()
    _fake(client, "kakao", OAuthProfile(subject="k-1"))
    for evil in ("//evil.example/x", "https://evil.example", "/\\evil.example"):
        assert _social_login(client, "kakao", evil).headers["location"] == "/"
        state = _start_post(client, "kakao", next=evil)
        assert _callback(client, "kakao", code="good-code", state=state).headers["location"] == "/"
        # 실패해서 돌아갈 때도 같은 출처다
        _, state = _start(client, "kakao", evil)
        denied = _callback(client, "kakao", error="access_denied", state=state)
        assert denied.headers["location"] == "/?auth_error=oauth_denied"


# ---------- 제공자 HTTP (MockTransport — 네트워크 없음) ----------


@pytest.mark.anyio
async def test_google_provider_exchanges_code_and_reads_userinfo(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": "at-SECRET-123", "id_token": "x"})
        assert str(request.url) == "https://openidconnect.googleapis.com/v1/userinfo"
        return httpx.Response(
            200, json={"sub": "1098", "email": "Minsu@Example.com", "email_verified": True, "name": "김 민수"}
        )

    caplog.set_level(logging.DEBUG)
    provider = GoogleProvider("gid", "gsecret", transport=httpx.MockTransport(handler))
    profile = await provider.fetch_profile(
        code="c0de", redirect_uri="https://p.example/cb", code_verifier="verifier"
    )
    assert profile == OAuthProfile(
        subject="1098", email="minsu@example.com", email_verified=True, name="김 민수"
    )
    form = {k: v[0] for k, v in parse_qs(seen[0].content.decode()).items()}
    assert form == {
        "grant_type": "authorization_code",
        "code": "c0de",
        "client_id": "gid",
        "client_secret": "gsecret",
        "redirect_uri": "https://p.example/cb",
        "code_verifier": "verifier",
    }
    assert seen[0].method == "POST" and seen[1].headers["authorization"] == "Bearer at-SECRET-123"
    # 토큰·코드·시크릿은 우리 로그에 남지 않는다
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert "at-SECRET-123" not in ours and "c0de" not in ours and "gsecret" not in ours

    # email_verified가 문자열 "true"처럼 와도 참으로 보지 않는다.
    def unverified(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"sub": "1", "email": "a@example.com", "email_verified": "true"})

    got = await GoogleProvider("gid", "s", transport=httpx.MockTransport(unverified)).fetch_profile(
        code="c", redirect_uri="r", code_verifier=None
    )
    assert got.email_verified is False


@pytest.mark.anyio
async def test_provider_failures_become_oauth_error_without_provider_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "ZQPROVIDERBODY invalid_grant: code was already redeemed"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"{marker} connection refused", request=request)

    def token_only(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(500, text=marker)

    handlers = {
        "token 400": lambda _: httpx.Response(
            400, json={"error": "invalid_grant", "error_description": marker}
        ),
        "token not json": lambda _: httpx.Response(200, text=marker),
        "token json list": lambda _: httpx.Response(200, json=[marker]),
        "no access_token": lambda _: httpx.Response(200, json={"error_description": marker}),
        "userinfo 500": token_only,
        "network": boom,
    }
    caplog.set_level(logging.DEBUG)
    for name, handler in handlers.items():
        provider = GoogleProvider("gid", "gsecret", transport=httpx.MockTransport(handler))
        with pytest.raises(OAuthError) as raised:
            await provider.fetch_profile(code="c0de", redirect_uri="r", code_verifier=None)
        assert marker not in str(raised.value) and "ZQ" not in str(raised.value), name
        assert raised.value.__cause__ is None, name  # 원래 예외(본문·주소)를 달고 가지 않는다
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert "ZQPROVIDERBODY" not in ours and "gsecret" not in ours and "c0de" not in ours

    # 라우트까지: 실패 응답에는 우리가 정한 코드만 있다
    client = _client()
    state_obj = _state(client)
    state_obj.oauth_providers["google"] = GoogleProvider(
        "gid", "gsecret", transport=httpx.MockTransport(handlers["token 400"])
    )
    _, state = _start(client, "google")
    r = _callback(client, "google", code="c0de", state=state)
    assert r.headers["location"] == "/?auth_error=oauth_failed" and "ZQ" not in r.text


@pytest.mark.anyio
async def test_kakao_provider_reads_nested_account_and_sends_secret() -> None:
    forms: list[dict[str, str]] = []
    me: dict[str, Any] = {
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

    kakao = KakaoProvider("rest-key", "ksecret", transport=httpx.MockTransport(handler))

    async def fetch() -> OAuthProfile:
        return await kakao.fetch_profile(code="c", redirect_uri="https://p/cb", code_verifier=None)

    assert await fetch() == OAuthProfile(
        subject="4242", email="minsu@kakao.com", email_verified=True, name="민수"
    )
    assert forms[0] == {
        "grant_type": "authorization_code",
        "code": "c",
        "client_id": "rest-key",
        "client_secret": "ksecret",
        "redirect_uri": "https://p/cb",
    }
    # 이메일 동의가 없거나 유효하지 않으면 확인되지 않은 것으로 본다.
    me["kakao_account"] = {"email": "minsu@kakao.com", "is_email_valid": False, "is_email_verified": True}
    assert (await fetch()).email_verified is False
    me["kakao_account"] = {"email": "minsu@kakao.com", "is_email_valid": True}
    assert (await fetch()).email_verified is False
    me.pop("kakao_account")
    assert await fetch() == OAuthProfile(subject="4242")
    me["id"] = True  # 불리언은 식별자가 아니다
    with pytest.raises(OAuthError):
        await fetch()
    me.pop("id")
    with pytest.raises(OAuthError):
        await fetch()


# ---------- 회원관리 규칙 (인메모리) ----------


def _seed_members(client: TestClient) -> dict[str, UUID]:
    service = _service(client)

    async def _run() -> dict[str, UUID]:
        boss = await service.create_account("boss", PW)  # ADMIN_USERNAMES의 관리자
        chief = await service.create_account("chief", PW, role="admin")  # 권한으로 관리자
        member = (await service.signup_email("jihyun@example.com", PW, display_name="박지현")).user
        return {"boss": boss.id, "chief": chief.id, "member": member.id}

    return asyncio.run(_run())


def test_member_admin_rules_self_protection_and_protected_config_admin() -> None:
    client = _client(admin_usernames="boss")
    ids = _seed_members(client)
    svc = _state(client).member_admin
    log = Log()

    def run(coro):  # noqa: ANN001, ANN202
        return asyncio.run(coro)

    # 자기 자신: 정지·권한 내리기·삭제 불가
    for kwargs in ({"status": "suspended"}, {"role": "user"}, {"status": "suspended", "role": "user"}):
        with pytest.raises(CannotModifySelfError) as raised:
            run(svc.update_member(log, ids["chief"], ids["chief"], **kwargs))
        assert (raised.value.status_code, raised.value.code) == (409, "cannot_modify_self")
    with pytest.raises(CannotModifySelfError):
        run(svc.delete_member(log, ids["chief"], ids["chief"]))
    with pytest.raises(CannotModifySelfError):
        run(svc.update_member(log, ids["boss"], ids["boss"], status="suspended"))
    # 설정상의 관리자(ADMIN_USERNAMES)는 다른 관리자도 정지·삭제할 수 없다
    with pytest.raises(ProtectedAdminError) as raised:
        run(svc.update_member(log, ids["chief"], ids["boss"], status="suspended"))
    assert (raised.value.status_code, raised.value.code) == (409, "protected_admin")
    with pytest.raises(ProtectedAdminError):
        run(svc.delete_member(log, ids["chief"], ids["boss"]))
    # 잘못된 요청
    with pytest.raises(InvalidInputError):
        run(svc.update_member(log, ids["boss"], ids["member"]))
    with pytest.raises(InvalidInputError):
        run(svc.update_member(log, ids["boss"], ids["member"], status="pending"))
    with pytest.raises(NotFoundError):
        run(svc.update_member(log, ids["boss"], uuid.uuid4(), status="suspended"))
    # 서버 규칙에 걸린 시도(409)는 사유 코드와 함께 남는다. 형식이 틀린 요청·없는 회원은 남기지 않는다.
    assert log.actions == ["member_refused"] * 7
    reasons = [detail.rpartition(" reason=")[2] for _, detail in log.lines]
    assert reasons == ["cannot_modify_self"] * 5 + ["protected_admin"] * 2
    assert log.lines[0][1].endswith("op=update status=suspended role=- reason=cannot_modify_self")
    assert log.lines[2][1].endswith("op=update status=suspended role=user reason=cannot_modify_self")
    assert log.lines[3][1].endswith("op=delete reason=cannot_modify_self")
    assert log.lines[6][1].endswith("op=delete reason=protected_admin")
    assert all(detail.startswith("target=u_") for _, detail in log.lines)
    log.lines.clear()
    repo = _state(client).auth
    assert {u.username: (u.role, u.status) for u in run(repo.list_users())[1]} == {
        "boss": ("user", "active"),
        "chief": ("admin", "active"),
        "jihyun": ("user", "active"),
    }

    # 권한 올리기·내리기 (다른 관리자가 내리는 것은 된다). 바뀌는 것이 없으면 기록도 없다.
    promoted = run(svc.update_member(log, ids["boss"], ids["member"], role="admin"))
    assert (promoted.role, promoted.is_admin, promoted.config_admin) == ("admin", True, False)
    run(svc.update_member(log, ids["boss"], ids["member"], role="admin", status="active"))
    demoted = run(svc.update_member(log, ids["boss"], ids["chief"], role="user"))
    assert (demoted.role, demoted.is_admin) == ("user", False)
    # 설정상의 관리자는 권한 열이 user여도 관리자다
    boss_view = run(svc.update_member(log, ids["member"], ids["boss"], role="admin"))
    boss_view = run(svc.update_member(log, ids["member"], ids["boss"], role="user"))
    assert (boss_view.role, boss_view.is_admin, boss_view.config_admin) == ("user", True, True)
    assert log.actions == ["member_role", "member_role_done"] * 4
    assert log.lines[0][1].endswith(" user→admin") and log.lines[0][1].startswith("target=u_")
    # 기록에는 줄인 id만 — 전체 id도, 아이디(이메일 가입이면 이메일 앞부분)도 없다
    text = " ".join(detail for _, detail in log.lines)
    assert str(ids["member"]) not in text and "jihyun" not in text and "name=" not in text


def test_last_active_admin_cannot_be_demoted_suspended_deleted_or_delete_itself() -> None:
    """활성 관리자 = active + (권한 admin 또는 ADMIN_USERNAMES의 이름), 실제로 있는 계정만 센다."""
    client = _client(admin_usernames="boss,ghost")  # ghost는 이름만 있고 계정이 없다 — 세지 않는다
    state = _state(client)
    service, repo, names = state.auth_service, state.auth, ("boss", "ghost")

    def run(coro):  # noqa: ANN001, ANN202
        return asyncio.run(coro)

    chief = run(service.create_account("chief", PW, role="admin"))
    member = run(service.create_account("member", PW))
    # 저장소: 하나뿐인 활성 관리자는 내리지도 정지하지도 지우지도 못한다 (검사에 걸리면 아무것도 안 바뀐다)
    for change in ({"role": "user"}, {"status": "suspended"}, {"role": "user", "status": "suspended"}):
        with pytest.raises(LastAdminError) as last:
            run(repo.update_user(chief.id, admin_names=names, **change))
        assert (last.value.status_code, last.value.code) == (409, "last_admin")
    with pytest.raises(LastAdminError):
        run(repo.delete_user(chief.id, admin_names=names))
    with pytest.raises(LastAdminError):
        run(repo.ensure_not_last_admin(chief.id, names))
    still = run(repo.get_user(chief.id))
    assert (still.role, still.status) == ("admin", "active")
    # 관리자가 아닌 회원에게는 걸리지 않는다. 운영 도구(admin_names 없음)는 검사하지 않는다.
    assert run(repo.update_user(member.id, status="suspended", admin_names=names)).status == "suspended"
    run(repo.ensure_not_last_admin(member.id, names))
    run(repo.ensure_not_last_admin(uuid.uuid4(), names))  # 없는 사용자는 통과 (삭제가 404를 낸다)

    # 본인 탈퇴(DELETE /v1/auth/me)도 같은 규칙 — 409이고 자료·계정·세션이 그대로다
    browser = TestClient(client.app)
    login = browser.post("/v1/auth/login", json={"username": "chief", "password": PW})
    csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
    assert browser.post("/v1/contacts", json=CONTACT, headers=csrf).status_code == 201

    def leave(password: str = PW) -> httpx.Response:
        return browser.request("DELETE", "/v1/auth/me", json={"password": password}, headers=csrf)

    refused = leave()
    assert _code(refused) == (409, "last_admin") and "다른 관리자" in refused.json()["message"]
    assert _code(leave("wrong-password-1")) == (401, "unauthorized")  # 비밀번호가 맞은 뒤에만 알린다
    assert len(browser.get("/v1/contacts").json()) == 1 and browser.get("/v1/auth/me").status_code == 200

    # 다른 관리자가 "있다"로 치지 않는 경우: 정지된 관리자, 승인 대기 관리자, 계정 없는 설정상의 이름
    other = run(service.create_account("other", PW, role="admin"))
    for status in ("suspended", "pending"):
        run(repo.update_user(other.id, status=status))
        assert _code(leave()) == (409, "last_admin"), status
    # 설정상의 관리자(ADMIN_USERNAMES)가 실제로 있고 active면 센다 — 권한 열이 user여도
    boss = run(service.create_account("boss", PW))
    assert run(repo.update_user(chief.id, role="user", admin_names=names)).role == "user"
    run(repo.update_user(chief.id, role="admin"))
    # 반대로 설정상의 관리자도 혼자면 탈퇴할 수 없다 (chief를 내리면 boss만 남는다)
    run(repo.update_user(chief.id, role="user", admin_names=names))
    boss_browser = TestClient(client.app)
    boss_login = boss_browser.post("/v1/auth/login", json={"username": "boss", "password": PW})
    boss_csrf = {"X-CSRF-Token": boss_login.json()["csrf_token"]}
    alone = boss_browser.request("DELETE", "/v1/auth/me", json={"password": PW}, headers=boss_csrf)
    assert _code(alone) == (409, "last_admin")
    # 설정상의 관리자는 권한 열을 내려도 관리자다 — 마지막 관리자여도 그 변경은 막지 않는다
    run(repo.update_user(boss.id, role="admin"))
    assert run(repo.update_user(boss.id, role="user", admin_names=names)).role == "user"

    # 다른 활성 관리자가 생기면 탈퇴된다. 관리자가 아닌 회원은 언제나 탈퇴할 수 있다.
    run(repo.update_user(chief.id, role="admin"))
    done = boss_browser.request("DELETE", "/v1/auth/me", json={"password": PW}, headers=boss_csrf)
    assert done.status_code == 200
    run(repo.update_user(chief.id, role="user"))  # 관리자 0명(운영 도구로만 생기는 상태)에서도
    assert leave().status_code == 200  # 일반 회원의 탈퇴는 막지 않는다


def test_stale_approve_with_expected_status_does_not_lift_a_suspension() -> None:
    client = _client(admin_usernames="boss", auth_signup_requires_approval=True)
    ids = _seed_members(client)  # jihyun은 승인 대기
    state = _state(client)
    svc, repo = state.member_admin, state.auth
    member = ids["member"]

    def run(coro):  # noqa: ANN001, ANN202
        return asyncio.run(coro)

    # 두 관리자가 같은 "승인 대기" 줄을 보고 있다. chief가 승인했다가 곧 정지했다.
    log = Log()
    run(svc.update_member(log, ids["chief"], member, status="active", expected_status="pending"))
    run(svc.update_member(log, ids["chief"], member, status="suspended", expected_status="active"))
    log.lines.clear()
    # boss의 화면은 아직 "승인 대기"다 — 그 줄의 승인은 정지를 풀지 않는다
    with pytest.raises(MemberChangedError) as changed:
        run(svc.update_member(log, ids["boss"], member, status="active", expected_status="pending"))
    assert (changed.value.status_code, changed.value.code) == (409, "member_changed")
    assert run(repo.get_user(member)).status == "suspended"
    assert log.actions == ["member_refused"]
    assert log.lines[0][1].endswith("op=update status=active role=- reason=member_changed")
    # 권한만 바꾸는 요청도 기대 상태가 다르면 바꾸지 않는다
    with pytest.raises(MemberChangedError):
        run(svc.update_member(log, ids["boss"], member, role="admin", expected_status="pending"))
    assert run(repo.get_user(member)).role == "user"

    # 서비스가 읽은 뒤에 상태가 바뀐 경우(동시 요청)는 저장소가 같은 트랜잭션에서 잡는다 → 결과 줄에 남는다
    log.lines.clear()
    real_get = repo.get_user

    async def stale_get(user_id: UUID):  # noqa: ANN202 - 서비스에게만 예전 상태를 보여 준다
        user = await real_get(user_id)
        return user.model_copy(update={"status": "pending"}) if user_id == member else user

    repo.get_user = stale_get
    try:
        with pytest.raises(MemberChangedError):
            run(svc.update_member(log, ids["boss"], member, status="active", expected_status="pending"))
    finally:
        repo.get_user = real_get
    assert log.actions == ["member_approve", "member_approve_failed"]
    assert log.lines[1][1].endswith("reason=member_changed")
    assert run(repo.get_user(member)).status == "suspended"

    # 지금 상태를 알고 보낸 요청은 된다. expected_status를 안 보내면 예전처럼 무조건 바꾼다.
    lifted = run(svc.update_member(log, ids["boss"], member, status="active", expected_status="suspended"))
    assert lifted.status == "active"
    run(svc.update_member(log, ids["boss"], member, status="suspended"))
    assert run(svc.update_member(log, ids["boss"], member, status="active")).status == "active"


def test_member_admin_records_before_changing_and_changes_nothing_if_the_record_fails() -> None:
    client = _client(admin_usernames="boss")
    ids = _seed_members(client)
    state = _state(client)
    svc, repo = state.member_admin, state.auth
    browser = TestClient(client.app)
    assert browser.post("/v1/auth/login", json={"username": "jihyun", "password": PW}).status_code == 200

    down = Log(down=True)
    for call in (
        svc.update_member(down, ids["boss"], ids["member"], status="suspended"),
        svc.update_member(down, ids["boss"], ids["member"], role="admin"),
        svc.revoke_sessions(down, ids["member"]),
        svc.delete_member(down, ids["boss"], ids["member"]),
        svc.list_members(down),
    ):
        with pytest.raises(RuntimeError, match="log down"):
            asyncio.run(call)
    user = asyncio.run(repo.get_user(ids["member"]))
    assert (user.status, user.role) == ("active", "user")
    assert browser.get("/v1/auth/me").status_code == 200  # 세션도 그대로

    # 기록이 되면: 세션 폐기 → 삭제. 삭제는 결과를 한 줄 더 남긴다.
    log = Log()
    csrf = {"X-CSRF-Token": browser.get("/v1/auth/me").json()["csrf_token"]}
    assert browser.post("/v1/contacts", json=CONTACT, headers=csrf).status_code == 201
    assert asyncio.run(svc.revoke_sessions(log, ids["member"])) == 2  # 가입 때 열린 세션 + 로그인
    assert browser.get("/v1/auth/me").status_code == 401
    deleted = asyncio.run(svc.delete_member(log, ids["boss"], ids["member"]))
    assert (deleted.contacts, deleted.conversations, deleted.messages) == (1, 0, 0)
    assert log.actions == [
        "member_revoke_sessions",
        "member_revoke_sessions_done",
        "member_delete",
        "member_delete_done",
    ]
    assert log.lines[1][1].endswith("sessions=2") and "contacts=1" in log.lines[3][1]
    # 지운 뒤에도 기록에 개인정보가 남지 않는다: 표시 이름·이메일은 물론 아이디(이메일 앞부분)도 없다
    assert all(
        "박지현" not in detail and "example.com" not in detail and "jihyun" not in detail
        for _, detail in log.lines
    )
    with pytest.raises(NotFoundError):
        asyncio.run(svc.delete_member(log, ids["boss"], ids["member"]))
    assert _code(browser.post("/v1/auth/login", json={"username": "jihyun", "password": PW})) == (
        401,
        "unauthorized",
    )


def test_member_list_search_filters_and_counts() -> None:
    client = _client(admin_usernames="boss", auth_signup_requires_approval=True)
    ids = _seed_members(client)  # jihyun은 승인제라 pending
    _fake(client, "kakao", OAuthProfile(subject="k-9", name="카카오 회원"))
    _social_login(TestClient(client.app), "kakao")  # pending 소셜 계정
    svc = _state(client).member_admin
    log = Log()

    def names(**kw: Any) -> list[str]:
        return [m.username for m in asyncio.run(svc.list_members(log, **kw)).items]

    page = asyncio.run(svc.list_members(log))
    assert page.total == 4 and page.counts.model_dump() == {"active": 2, "pending": 2, "suspended": 0}
    assert [m.username for m in page.items] == ["kakao", "jihyun", "chief", "boss"]  # 최근 가입 순
    by_name = {m.username: m for m in page.items}
    assert by_name["jihyun"].user_id == str(ids["member"])  # 회원관리 호출에 쓸 전체 id
    assert (by_name["jihyun"].email, by_name["jihyun"].email_verified) == ("jihyun@example.com", False)
    assert (by_name["jihyun"].providers, by_name["kakao"].providers) == (["password"], ["kakao"])
    assert (by_name["boss"].is_admin, by_name["boss"].config_admin, by_name["boss"].role) == (
        True,
        True,
        "user",
    )
    assert (by_name["chief"].is_admin, by_name["chief"].config_admin) == (True, False)
    assert set(by_name["kakao"].model_dump()) == {
        "user_id",
        "username",
        "email",
        "email_verified",
        "display_name",
        "role",
        "status",
        "is_admin",
        "config_admin",
        "providers",
        "created_at",
        "last_login_at",
        "consent_version",
    }
    assert names(query="JIHYUN@") == ["jihyun"]  # 이메일 부분 일치, 대소문자 무시
    assert names(query="카카오") == ["kakao"]  # 표시 이름
    assert names(query="os") == ["boss"]
    assert names(query="%") == [] and names(query="_") == []  # 와일드카드로 쓰이지 않는다
    assert names(status="pending") == ["kakao", "jihyun"] and names(role="admin") == ["chief"]
    assert names(limit=1, offset=1) == ["jihyun"]
    assert asyncio.run(svc.list_members(log, status="pending", limit=1)).total == 2
    # 검색어는 기록에 남기지 않는다 (이메일일 수 있다)
    assert all(action == "members" for action in log.actions)
    assert all("JIHYUN" not in detail and "카카오" not in detail for _, detail in log.lines)


def test_role_admin_passes_the_admin_check_and_admin_writes_need_csrf() -> None:
    client = _client(admin_usernames="boss")
    ids = _seed_members(client)
    target = f"/v1/admin/members/{ids['member']}"
    writes = (
        ("PATCH", target, {"status": "suspended"}),
        ("POST", f"{target}/revoke-sessions", None),
        ("DELETE", target, None),
    )

    def call(c: TestClient, method: str, path: str, body: Any, headers: dict[str, str] | None = None):  # noqa: ANN202
        return c.request(method, path, json=body, headers=headers)

    # 로그인 없음 401 (헤더 스텁도), 일반 회원 403
    anon = TestClient(client.app)
    for method, path, body in (("GET", "/v1/admin/members", None), *writes):
        assert _code(call(anon, method, path, body)) == (401, "unauthorized"), path
        assert _code(call(anon, method, path, body, {"X-User-Id": "boss"})) == (401, "unauthorized"), path
    member = TestClient(client.app)
    member_csrf = {
        "X-CSRF-Token": member.post("/v1/auth/login", json={"username": "jihyun", "password": PW}).json()[
            "csrf_token"
        ]
    }
    for method, path, body in (("GET", "/v1/admin/members", None), *writes):
        assert _code(call(member, method, path, body, member_csrf)) == (403, "forbidden"), path

    # 권한이 admin인 사용자(ADMIN_USERNAMES에 없음)와 설정상의 관리자 모두 관리자 검사를 통과한다
    # (인메모리 저장소라 그 다음 단계에서 503 — main의 401 → 403 → 503 순서 그대로)
    for name in ("chief", "boss"):
        admin = TestClient(client.app)
        login = admin.post("/v1/auth/login", json={"username": name, "password": PW})
        assert login.json()["is_admin"] is True and admin.get("/v1/auth/me").json()["is_admin"] is True
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
        assert _code(admin.get("/v1/admin/members")) == (503, "admin_unavailable")
        assert _code(admin.get("/v1/admin/overview")) == (503, "admin_unavailable")
        for method, path, body in writes:
            no_csrf = call(admin, method, path, body)
            assert _code(no_csrf) == (403, "forbidden") and "CSRF" in no_csrf.json()["message"], path
            assert _code(call(admin, method, path, body, {"X-CSRF-Token": "nope"})) == (403, "forbidden")
            assert _code(call(admin, method, path, body, csrf)) == (503, "admin_unavailable"), path
    # 아무것도 바뀌지 않았다
    user = asyncio.run(_state(client).auth.get_user(ids["member"]))
    assert (user.status, user.role) == ("active", "user")

    # 권한을 내리면 다음 요청부터 관리자가 아니다
    chief = TestClient(client.app)
    chief.post("/v1/auth/login", json={"username": "chief", "password": PW})
    asyncio.run(_state(client).member_admin.update_member(Log(), ids["boss"], ids["chief"], role="user"))
    assert _code(chief.get("/v1/admin/members")) == (403, "forbidden")
    assert chief.get("/v1/auth/me").json()["is_admin"] is False


# ---------- 운영자 도구 (app.tools.create_user) ----------


def test_create_user_tool_creates_promotes_and_resets() -> None:
    client = _client(
        auth_allow_register=False, auth_allow_header_stub=False, auth_signup_requires_approval=True
    )
    state = _state(client)
    service, repo = state.auth_service, state.auth

    def apply(username: str, password: str | None, **kw: bool) -> str:
        return asyncio.run(create_user.apply(service, repo, username, password, **kw))

    line = apply("root_admin", PW, admin=True)
    assert "root_admin" in line and "role=admin" in line and "status=active" in line and PW not in line
    login = client.post("/v1/auth/login", json={"username": "root_admin", "password": PW})
    assert login.status_code == 200 and login.json()["is_admin"] is True
    assert client.get("/v1/admin/overview").status_code == 503  # ADMIN_USERNAMES 없이 role만으로 통과

    assert "role=user" in apply("plain_user", PW)
    device = TestClient(client.app)
    device.post("/v1/auth/login", json={"username": "plain_user", "password": PW})
    # 이미 있는 아이디: --admin이면 비밀번호를 건드리지 않고 권한만 올린다
    assert asyncio.run(create_user.needs_password(repo, "plain_user", reset_password=False)) is False
    assert asyncio.run(create_user.needs_password(repo, "brand_new", reset_password=False)) is True
    assert asyncio.run(create_user.needs_password(repo, "plain_user", reset_password=True)) is True
    assert "관리자로 올렸습니다" in apply("plain_user", None, admin=True)
    assert device.get("/v1/auth/me").json()["is_admin"] is True  # 세션은 그대로
    with pytest.raises(DomainError):
        apply("plain_user", PW)  # 이미 있음 — 실수로 덮어쓰지 않는다

    # --reset-password: 세션 전부 폐기, 예전 비밀번호는 더 이상 안 된다
    out = apply("plain_user", "brand-new-password-9", reset_password=True)
    assert "로그아웃된 세션 1개" in out and "brand-new-password-9" not in out
    assert device.get("/v1/auth/me").status_code == 401
    assert device.post("/v1/auth/login", json={"username": "plain_user", "password": PW}).status_code == 401
    ok = device.post("/v1/auth/login", json={"username": "plain_user", "password": "brand-new-password-9"})
    assert ok.status_code == 200
    with pytest.raises(NotFoundError):
        apply("nobody_here", "long-enough-pass", reset_password=True)
    with pytest.raises(InvalidInputError):
        apply("plain_user", "short", reset_password=True)
    with pytest.raises(InvalidInputError):
        apply("bad name", PW)

    args = create_user.parse_args(["someone", "--admin", "--reset-password"])
    assert (args.username, args.admin, args.reset_password) == ("someone", True, True)
    plain = create_user.parse_args(["someone"])
    assert (plain.admin, plain.reset_password) == (False, False)


def test_create_user_tool_reads_password_without_echo_or_argv() -> None:
    asked: list[str] = []

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    def prompt(label: str) -> str:
        asked.append(label)
        return "typed-password-1"

    assert create_user.read_password(Tty(), prompt) == "typed-password-1" and len(asked) == 2
    assert create_user.read_password(io.StringIO("piped-password-2\nrest\n")) == "piped-password-2"
    with pytest.raises(create_user.InputError):
        create_user.read_password(io.StringIO("\n"))
    answers = iter(["one-password-1", "two-password-2"])
    with pytest.raises(create_user.InputError):
        create_user.read_password(Tty(), lambda _: next(answers))
    with pytest.raises(SystemExit):  # 비밀번호를 인자로 받는 옵션은 없다
        create_user.parse_args(["someone", "--password", "x"])

    # Git Bash(mintty)처럼 콘솔 없이 터미널을 흉내 내는 창: 치는 글자가 화면에 보이므로 읽지 않고 끝낸다
    typed = io.StringIO("visible-password-3\n")
    with pytest.raises(create_user.InputError, match="가릴 수 없어"):
        create_user.read_password(typed, terminal_pipe=lambda _: True)
    assert typed.tell() == 0  # 한 글자도 읽지 않았다
    # 진짜 파이프·파일로 판단되면 읽는다. 판단은 핸들을 보고 한다 — 핸들이 없는 스트림은 파이프로 본다.
    assert create_user.is_terminal_pipe(io.StringIO("x")) is False
    assert create_user.read_password(typed, terminal_pipe=lambda _: False) == "visible-password-3"
    # 환경변수로도 받지 않는다 (비밀번호처럼 보이는 변수가 있어도 표준입력만 본다)
    import os
    from unittest import mock

    with mock.patch.dict(
        os.environ, {"PASSWORD": "env-password-4", "CREATE_USER_PASSWORD": "env-password-4"}
    ):
        with pytest.raises(create_user.InputError):
            create_user.read_password(io.StringIO(""))


def test_create_user_tool_reads_a_real_pipe_and_does_not_wait_on_a_null_device() -> None:
    """실제 프로세스의 표준입력으로 본다: 파이프면 첫 줄을 읽고, 입력 없는 장치(NUL)면 기다리지 않는다."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "from app.tools import create_user\n"
        "try:\n"
        "    password = create_user.read_password()\n"
        "except create_user.InputError as exc:\n"
        "    print('input-error')\n"
        "    raise SystemExit(2) from exc\n"
        "print(len(password), create_user.is_console(sys.stdin), create_user.is_terminal_pipe(sys.stdin))\n"
    )

    def run(**stdin: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - 이 테스트가 만든 고정 코드만 돌린다
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False, **stdin
        )

    piped = run(input="piped-password-2\nsecond line is ignored\n")
    assert (piped.returncode, piped.stdout.split()) == (0, ["16", "False", "False"]), piped.stderr
    assert "piped-password-2" not in piped.stdout + piped.stderr  # 읽은 값은 어디에도 찍지 않는다
    # Windows의 NUL은 isatty()가 참이다 — 콘솔로 착각해 getpass가 끝없이 기다리면 여기서 시간 초과가 난다
    empty = run(stdin=subprocess.DEVNULL)
    assert (empty.returncode, empty.stdout.strip()) == (2, "input-error"), empty.stderr


# ---------- PostgreSQL 통합 (DB가 없으면 건너뛴다) ----------


def _pg_name(prefix: str = "mb") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture(name="pg_url")
def _pg_url_cleaned(pg_url: str) -> Iterator[str]:
    """이 파일의 PostgreSQL 테스트가 쓰는 `pg_url` — 끝나면 그 테스트가 넣은 사용자와 접근 기록을 지운다.

    테스트 DB는 세션 동안 여러 파일이 함께 쓴다. 여기서 남긴 `admin_access_log` 행 때문에 뒤에 도는
    `test_admin.py`의 "접근 기록은 전부 그 관리자의 것" 검사가 깨졌다(2026-11-17) — 시작할 때 있던 행의
    id를 적어 두고, 끝나면 그 밖의 행을 지운다(사용자를 지우면 세션·소셜 연결은 CASCADE).
    """
    import asyncpg

    from tests.pg_support import dsn

    tables = ("users", "admin_access_log")

    async def _ids() -> dict[str, list[UUID]]:
        conn = await asyncpg.connect(dsn(pg_url))
        try:
            return {t: [r["id"] for r in await conn.fetch(f"SELECT id FROM {t}")] for t in tables}  # noqa: S608
        finally:
            await conn.close()

    async def _remove_new(before: dict[str, list[UUID]]) -> None:
        conn = await asyncpg.connect(dsn(pg_url))
        try:
            for table in tables:
                await conn.execute(f"DELETE FROM {table} WHERE id <> ALL($1::uuid[])", before[table])  # noqa: S608
        finally:
            await conn.close()

    before = asyncio.run(_ids())
    try:
        yield pg_url
    finally:
        asyncio.run(_remove_new(before))


def test_schema_defaults_constraints_and_identity_repo_on_postgres(pg_url: str) -> None:
    import asyncpg
    from sqlalchemy.exc import IntegrityError

    from app.core.errors import VersionConflictError
    from app.db.session import create_engine, create_session_factory
    from app.repositories.auth import EMAIL_TAKEN, USERNAME_TAKEN, PostgresAuthRepo
    from tests.pg_support import dsn

    async def _run() -> None:
        engine = create_engine(pg_url)
        repo = PostgresAuthRepo(create_session_factory(engine))
        conn = await asyncpg.connect(dsn(pg_url))
        try:
            # 예전 방식(새 열을 모르는 INSERT)으로 넣은 행은 role=user, status=active다
            legacy = uuid.uuid4()
            await conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES ($1, $2, 'x', now())",
                legacy,
                _pg_name("old"),
            )
            old = await repo.get_user(legacy)
            assert (old.role, old.status, old.email, old.email_verified, old.has_password) == (
                "user",
                "active",
                None,
                False,
                True,
            )
            # DB가 직접 막는 것: 대문자 이메일, 모르는 상태·권한, 같은 이메일
            mail = f"{_pg_name()}@example.com"
            for column, value in (("email", mail.upper()), ("status", "banned"), ("role", "owner")):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(f"UPDATE users SET {column} = $1 WHERE id = $2", value, legacy)  # noqa: S608
            await conn.execute("UPDATE users SET email = $1 WHERE id = $2", mail, legacy)
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO users (id, username, password_hash, created_at, email)"
                    " VALUES ($1, $2, NULL, now(), $3)",
                    uuid.uuid4(),
                    _pg_name("dup"),
                    mail,
                )

            # 저장소: 이메일·이름 중복을 가려서 알린다, 비밀번호 없는 계정, 상태·권한·마지막 로그인
            name, email = _pg_name(), f"{_pg_name()}@example.com"
            user = await repo.create_user(
                name, None, email=email, email_verified=True, display_name="김민수", status="pending"
            )
            assert (user.status, user.has_password, user.email_verified) == ("pending", False, True)
            with pytest.raises(VersionConflictError) as taken:
                await repo.create_user(_pg_name(), "x", email=email)
            assert taken.value.message == EMAIL_TAKEN
            with pytest.raises(VersionConflictError) as taken:
                await repo.create_user(name, "x")
            assert taken.value.message == USERNAME_TAKEN
            with pytest.raises(IntegrityError):  # 서비스가 소문자로 바꾸지 않고 넘기면 DB가 막는다
                await repo.create_user(_pg_name(), "x", email="Upper@Example.com")
            found = await repo.find_user_by_email(email)
            assert found is not None and found[0].id == user.id and found[1] is None
            assert await repo.find_user_by_email(email.upper()) is None  # 조회는 소문자 값으로만
            updated = await repo.update_user(user.id, status="active", role="admin")
            assert (updated.status, updated.role) == ("active", "admin")
            assert (await repo.update_user(user.id)).role == "admin"  # 바꿀 것이 없어도 읽어 온다
            with pytest.raises(NotFoundError):
                await repo.update_user(uuid.uuid4(), status="active")

            # 목록·검색 (다른 테스트의 행과 섞이므로 이 테스트의 이름으로 좁힌다)
            total, rows = await repo.list_users(query=name.upper())
            assert total == 1 and rows[0].id == user.id
            assert (await repo.list_users(query=email[:10]))[0] == 1
            assert (await repo.list_users(query="김민수", role="admin"))[0] >= 1
            assert (await repo.list_users(query=name, status="suspended"))[0] == 0
            assert (await repo.list_users(query="%"))[0] == 0  # 와일드카드로 쓰이지 않는다
            assert (await repo.count_by_status()).get("active", 0) >= 2

            # 소셜 연결: (provider, subject)는 한 사용자에게만, 사용자는 제공자마다 하나만
            subject = uuid.uuid4().hex
            await repo.add_identity(user.id, "google", subject)
            with pytest.raises(VersionConflictError):
                await repo.add_identity(legacy, "google", subject)
            with pytest.raises(VersionConflictError):
                await repo.add_identity(user.id, "google", uuid.uuid4().hex)
            await repo.add_identity(user.id, "kakao", subject)  # 다른 제공자의 같은 subject는 별개다
            identity = await repo.find_identity("google", subject)
            assert identity is not None and identity.user_id == user.id
            assert await repo.find_identity("google", "missing") is None
            assert await repo.list_providers([user.id, legacy]) == {user.id: ["google", "kakao"]}
            assert await repo.list_providers([]) == {}
            # 저장하는 것은 식별자뿐이다 (토큰·이메일 열이 없다)
            columns = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'user_identities'"
            )
            assert {c["column_name"] for c in columns} == {
                "id",
                "provider",
                "subject",
                "user_id",
                "created_at",
            }

            # 세션은 사용자 상태를 함께 싣는다 → 정지하면 폐기 없이도 죽은 세션이다
            from datetime import UTC, datetime, timedelta

            from app.domain.auth import Session

            now = datetime.now(UTC)
            token = uuid.uuid4().hex
            await repo.create_session(
                Session(
                    token_hash=token,
                    user_id=user.id,
                    csrf_token="c",
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                )
            )
            assert (await repo.get_session(token)).is_active(now)
            await repo.update_user(user.id, status="suspended")
            dead = await repo.get_session(token)
            assert dead.user_status == "suspended" and not dead.is_active(now) and dead.revoked_at is None

            # 계정을 지우면 소셜 연결·세션도 사라진다
            await repo.delete_user(user.id)
            assert await repo.find_identity("google", subject) is None
            assert await repo.get_session(token) is None
            await repo.delete_user(legacy)
        finally:
            await conn.close()
            await engine.dispose()

    asyncio.run(_run())


def test_member_admin_api_end_to_end_on_postgres(pg_url: str) -> None:
    boss, tag = _pg_name("boss"), uuid.uuid4().hex[:10]
    settings = _settings(database_url=pg_url, admin_usernames=boss, auth_signup_requires_approval=True)
    with TestClient(create_app(settings)) as client:  # 한 이벤트 루프에서 (asyncpg 연결은 루프에 묶인다)
        state = _state(client)
        # 관리자 계정은 서버에서 만든다(승인 없이 active). 쿠키로 로그인 → 쓰기에는 CSRF가 필요하다.
        client.portal.call(state.auth_service.create_account, boss, PW)
        login = client.post("/v1/auth/login", json={"username": boss, "password": PW})
        assert login.status_code == 200 and login.json()["is_admin"] is True
        boss_cookie, boss_id = client.cookies.get("pi_session"), login.json()["user_id"]
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}

        def as_boss() -> None:
            client.cookies.clear()
            client.cookies.set("pi_session", boss_cookie)

        # 회원 둘이 가입한다 (승인제 → 202, 세션 없음)
        client.cookies.clear()
        emails = [f"{tag}-a@example.com", f"{tag}-b@example.com"]
        for email in emails:
            r = client.post(
                "/v1/auth/signup", json={"email": email, "password": PW, "display_name": f"회원 {tag}"}
            )
            assert r.status_code == 202 and r.json()["status"] == "pending", r.text
        assert _code(client.post("/v1/auth/token", json={"username": emails[0], "password": PW})) == (
            403,
            "account_pending",
        )

        # 목록·검색·대기 수
        as_boss()
        listed = client.get("/v1/admin/members", params={"query": tag, "status": "pending"})
        assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
        page = listed.json()
        assert (
            page["total"] == 2
            and page["counts"]["pending"] >= 2
            and set(page) == {"total", "counts", "items"}
        )
        members = {m["email"]: m for m in page["items"]}
        assert set(members) == set(emails)
        a, b = members[emails[0]], members[emails[1]]
        assert (a["status"], a["role"], a["providers"], a["is_admin"]) == (
            "pending",
            "user",
            ["password"],
            False,
        )
        assert client.get("/v1/admin/members", params={"query": tag, "limit": 1}).json()["total"] == 2
        assert client.get("/v1/admin/members", params={"status": "nope"}).status_code == 422
        assert client.get("/v1/admin/members", params={"limit": 0}).status_code == 422
        # main의 기존 사용자 목록은 그대로다 (줄인 id, 예전 필드)
        assert set(client.get("/v1/admin/users", params={"limit": 1}).json()["items"][0]) == {
            "user_id",
            "username",
            "created_at",
            "consent_version",
            "contacts",
            "messages",
            "rewrites",
            "last_rewrite_at",
        }

        # 쓰기는 CSRF 필수, 잘못된 값·없는 사용자
        url_a, url_b = f"/v1/admin/members/{a['user_id']}", f"/v1/admin/members/{b['user_id']}"
        assert _code(client.patch(url_a, json={"status": "active"})) == (403, "forbidden")
        assert _code(client.post(f"{url_a}/revoke-sessions")) == (403, "forbidden")
        assert _code(client.delete(url_a)) == (403, "forbidden")
        assert client.patch(url_a, json={}, headers=csrf).status_code == 422
        assert client.patch(url_a, json={"status": "pending"}, headers=csrf).status_code == 422
        assert client.patch(url_a, json={"role": "owner"}, headers=csrf).status_code == 422
        assert client.patch(url_a, json={"status": "active", "x": 1}, headers=csrf).status_code == 422
        assert (
            client.patch("/v1/admin/members/not-a-uuid", json={"role": "admin"}, headers=csrf).status_code
            == 422
        )
        missing = f"/v1/admin/members/{uuid.uuid4()}"
        assert _code(client.patch(missing, json={"role": "admin"}, headers=csrf)) == (404, "not_found")
        assert _code(client.delete(missing, headers=csrf)) == (404, "not_found")

        # 승인 → 로그인된다 (쿠키 세션 + Bearer)
        approved = client.patch(url_a, json={"status": "active"}, headers=csrf)
        assert approved.status_code == 200 and approved.json()["status"] == "active"
        assert (
            client.patch(url_a, json={"status": "active"}, headers=csrf).status_code == 200
        )  # 그대로 → 기록 없음
        client.cookies.clear()
        a_login = client.post("/v1/auth/login", json={"username": emails[0], "password": PW})
        assert a_login.status_code == 200
        a_cookie, a_csrf = client.cookies.get("pi_session"), {"X-CSRF-Token": a_login.json()["csrf_token"]}
        assert client.post("/v1/contacts", json=CONTACT, headers=a_csrf).status_code == 201
        client.cookies.clear()
        a_bearer = _bearer(
            client.post("/v1/auth/token", json={"username": emails[0], "password": PW}).json()["access_token"]
        )
        assert client.get("/v1/contacts", headers=a_bearer).status_code == 200
        # 일반 회원은 회원관리를 못 쓴다 (스스로 올릴 수 없다)
        assert _code(client.get("/v1/admin/members", headers=a_bearer)) == (403, "forbidden")
        assert _code(client.patch(url_a, json={"role": "admin"}, headers=a_bearer)) == (403, "forbidden")

        # 정지 → 쿠키 세션과 Bearer가 그 자리에서 끊기고 로그인도 막힌다
        as_boss()
        suspended = client.patch(url_a, json={"status": "suspended"}, headers=csrf)
        assert suspended.status_code == 200 and suspended.json()["status"] == "suspended"
        client.cookies.clear()
        assert _code(client.get("/v1/contacts", headers=a_bearer)) == (401, "unauthorized")
        assert _code(client.get("/v1/auth/me", headers=a_bearer)) == (401, "unauthorized")
        client.cookies.set("pi_session", a_cookie)
        assert _code(client.get("/v1/auth/me")) == (401, "unauthorized")
        assert _code(client.post("/v1/contacts", json=CONTACT, headers=a_csrf)) == (401, "unauthorized")
        client.cookies.clear()
        for path in ("/v1/auth/login", "/v1/auth/token"):
            assert _code(client.post(path, json={"username": emails[0], "password": PW})) == (
                403,
                "account_suspended",
            )

        # 정지 해제 + 권한 올리기 → 그 회원도 관리자 API를 쓴다. 자기 보호·설정상의 관리자 보호.
        as_boss()
        both = client.patch(url_a, json={"status": "active", "role": "admin"}, headers=csrf)
        assert (both.json()["status"], both.json()["role"], both.json()["is_admin"]) == (
            "active",
            "admin",
            True,
        )
        assert _code(
            client.patch(f"/v1/admin/members/{boss_id}", json={"status": "suspended"}, headers=csrf)
        ) == (
            409,
            "cannot_modify_self",
        )
        assert _code(client.delete(f"/v1/admin/members/{boss_id}", headers=csrf)) == (
            409,
            "cannot_modify_self",
        )
        client.cookies.clear()
        a_bearer = _bearer(
            client.post("/v1/auth/token", json={"username": emails[0], "password": PW}).json()["access_token"]
        )
        assert client.get("/v1/admin/members", params={"query": tag}, headers=a_bearer).json()["total"] == 2
        for body in ({"status": "suspended"}, {"role": "user"}):
            assert _code(client.patch(url_a, json=body, headers=a_bearer)) == (409, "cannot_modify_self")
        assert _code(client.delete(url_a, headers=a_bearer)) == (409, "cannot_modify_self")
        boss_url = f"/v1/admin/members/{boss_id}"
        assert _code(client.patch(boss_url, json={"status": "suspended"}, headers=a_bearer)) == (
            409,
            "protected_admin",
        )
        assert _code(client.delete(boss_url, headers=a_bearer)) == (409, "protected_admin")
        as_boss()
        assert client.get("/v1/auth/me").status_code == 200  # 관리자는 그대로다

        # 세션 폐기
        revoked = client.post(f"{url_a}/revoke-sessions", headers=csrf)
        assert revoked.status_code == 200 and revoked.json() == {"revoked_sessions": 1}
        client.cookies.clear()
        assert _code(client.get("/v1/admin/members", headers=a_bearer)) == (401, "unauthorized")

        # 삭제: 그 회원의 자료까지. 승인 대기 회원을 지우는 것(가입 거절)도 된다.
        as_boss()
        deleted = client.delete(url_a, headers=csrf)
        assert deleted.status_code == 200 and deleted.json() == {
            "contacts": 1,
            "conversations": 0,
            "messages": 0,
            "graph_nodes_removed": 0,
            "graph_error": None,
        }
        assert _code(client.delete(url_a, headers=csrf)) == (404, "not_found")
        assert client.delete(url_b, headers=csrf).status_code == 200
        assert client.get("/v1/admin/members", params={"query": tag}).json()["total"] == 0
        client.cookies.clear()
        assert _code(client.post("/v1/auth/login", json={"username": emails[0], "password": PW})) == (
            401,
            "unauthorized",
        )

        # 접근 기록: 하려던 일·결과·거절이 관리자 이름·대상(줄인 id)과 함께 남았다
        as_boss()
        log = client.get("/v1/admin/access-log", params={"limit": 200}).json()
        short_a = f"target=u_{a['user_id'][:3]}…{a['user_id'][-2:]}"
        mine = [e for e in log if e["detail"] and short_a in e["detail"]]
        assert [e["action"] for e in reversed(mine)] == [
            "member_approve",
            "member_approve_done",
            "member_suspend",
            "member_suspend_done",
            "member_activate",
            "member_role",
            "member_activate_done",
            "member_role_done",
            "member_refused",  # a가 자기 자신을 정지
            "member_refused",  # a가 자기 권한을 내림
            "member_refused",  # a가 자기 자신을 삭제
            "member_revoke_sessions",
            "member_revoke_sessions_done",
            "member_delete",
            "member_delete_done",
        ]
        details = {e["action"]: e["detail"] for e in mine}
        assert details["member_approve"].endswith("pending→active")
        assert details["member_suspend"].endswith("active→suspended")
        assert details["member_suspend_done"].endswith("sessions=2")  # 쿠키 세션 + Bearer 토큰
        assert details["member_role"].endswith("user→admin")
        assert details["member_revoke_sessions_done"].endswith("sessions=1")
        assert "contacts=1" in details["member_delete_done"]
        refused = [e for e in mine if e["action"] == "member_refused"]
        assert {e["detail"].rpartition(" reason=")[2] for e in refused} == {"cannot_modify_self"}
        assert {e["admin_username"] for e in refused} == {a["username"]}  # 시도한 사람의 이름으로
        assert {e["admin_username"] for e in mine if e["action"] != "member_refused"} == {boss}
        # boss를 겨냥한 거절(자기 자신 2 + 설정상의 관리자 2)도 사유 코드와 함께 남았다
        short_boss = f"target=u_{boss_id[:3]}…{boss_id[-2:]}"
        on_boss = [e["detail"].rpartition(" reason=")[2] for e in log if short_boss in (e["detail"] or "")]
        assert sorted(on_boss) == ["cannot_modify_self"] * 2 + ["protected_admin"] * 2
        assert any(e["action"] == "members" for e in log)
        # 대상의 전체 id·이메일·아이디(이메일 앞부분)·표시 이름은 기록 어디에도 없다
        text = json.dumps([e["detail"] for e in log], ensure_ascii=False)
        assert a["user_id"] not in text and emails[0] not in text and PW not in text and "박지현" not in text
        assert a["username"] not in text and b["username"] not in text and "name=" not in text


def test_social_login_round_trip_on_postgres(pg_url: str) -> None:
    tag = uuid.uuid4().hex[:10]
    settings = _settings(database_url=pg_url, public_base_url="https://persona.example.com")
    with TestClient(create_app(settings)) as client:
        email = f"{tag}@example.com"
        _fake(
            client,
            "google",
            OAuthProfile(subject=f"g-{tag}", email=email, email_verified=True, name="김민수"),
        )
        done = _social_login(client, "google", "/admin")
        assert done.headers["location"] == "/admin" and _has_session_cookie(done)
        me = client.get("/v1/auth/me").json()
        assert (me["email"], me["providers"], me["status"]) == (email, ["google"], "active")
        # 같은 확인된 이메일의 카카오 → 같은 계정
        client.cookies.clear()
        _fake(client, "kakao", OAuthProfile(subject=f"k-{tag}", email=email, email_verified=True))
        assert _social_login(client, "kakao").headers["location"] == "/"
        again = client.get("/v1/auth/me").json()
        assert again["user_id"] == me["user_id"] and again["providers"] == ["google", "kakao"]
        # 이메일 가입 계정에는 묶지 않는다
        client.cookies.clear()
        other = f"{tag}-pw@example.com"
        assert client.post("/v1/auth/signup", json={"email": other, "password": PW}).status_code == 201
        client.cookies.clear()
        _fake(client, "google", OAuthProfile(subject=f"g2-{tag}", email=other, email_verified=True))
        assert _social_login(client, "google").headers["location"] == "/?auth_error=email_in_use"
        assert client.get("/v1/auth/me").status_code == 401
        # 소셜 전용 계정 탈퇴 (비밀번호 없이, CSRF는 필요)
        _fake(client, "kakao", OAuthProfile(subject=f"k-{tag}"))
        _social_login(client, "kakao")
        csrf = {"X-CSRF-Token": client.get("/v1/auth/me").json()["csrf_token"]}
        assert client.request("DELETE", "/v1/auth/me", json={"password": ""}, headers=csrf).status_code == 200
        assert client.get("/v1/auth/me").status_code == 401


def test_two_admins_racing_to_remove_each_other_leave_one_admin_on_postgres(pg_url: str) -> None:
    """두 권한 관리자가 **동시에** 서로를 내리기·정지·삭제해도 한 명은 남는다 (저장소의 한 트랜잭션 검사)."""
    import asyncpg

    from tests.pg_support import dsn

    async def _no_other_admins() -> None:
        # 다른 테스트가 남긴 권한 관리자가 있으면 "다른 관리자가 있다"로 세어져 이 검사가 헛돈다.
        conn = await asyncpg.connect(dsn(pg_url))
        try:
            await conn.execute("UPDATE users SET role = 'user' WHERE role = 'admin'")
        finally:
            await conn.close()

    asyncio.run(_no_other_admins())
    settings = _settings(database_url=pg_url)  # ADMIN_USERNAMES 없음 — 관리자는 권한으로만 있다
    with TestClient(create_app(settings)) as client:  # 한 이벤트 루프에서 (asyncpg 연결은 루프에 묶인다)
        state = _state(client)
        svc, repo, service = state.member_admin, state.auth, state.auth_service

        async def race(op: str) -> tuple[list[Any], Log, list[UUID]]:
            a = await service.create_account(_pg_name("adm"), PW, role="admin")
            b = await service.create_account(_pg_name("adm"), PW, role="admin")
            log = Log()

            def remove(actor: UUID, target: UUID):  # noqa: ANN202
                if op == "delete":
                    return svc.delete_member(log, actor, target)
                change = {"role": "user"} if op == "demote" else {"status": "suspended"}
                return svc.update_member(log, actor, target, **change)

            results = await asyncio.gather(remove(a.id, b.id), remove(b.id, a.id), return_exceptions=True)
            return results, log, [a.id, b.id]

        async def active_admins(ids: list[UUID]) -> list[UUID]:
            _, users = await repo.list_users(role="admin", status="active", limit=200)
            return [u.id for u in users if u.id in ids]

        for op, action in (
            ("demote", "member_role"),
            ("suspend", "member_suspend"),
            ("delete", "member_delete"),
        ):
            results, log, ids = client.portal.call(race, op)
            errors = [r for r in results if isinstance(r, BaseException)]
            assert len(errors) == 1 and isinstance(errors[0], LastAdminError), (op, results)
            survivors = client.portal.call(active_admins, ids)
            assert len(survivors) == 1, op  # 정확히 한 명이 남았다
            # 진 쪽의 시도는 사유와 함께 기록에 남는다
            failed = [detail for name, detail in log.lines if name == f"{action}_failed"]
            assert len(failed) == 1 and failed[0].endswith("reason=last_admin"), (op, log.lines)
            assert log.actions.count(f"{action}_done") == 1
            # 다음 판을 위해 남은 관리자를 내린다 (운영 도구의 길 — 검사 없음)
            client.portal.call(lambda sid=survivors[0]: repo.update_user(sid, role="user"))

        # 본인 탈퇴: 하나뿐인 활성 관리자는 409이고 자료·계정이 그대로다. 다른 관리자가 생기면 된다.
        solo = _pg_name("solo")
        client.portal.call(lambda: service.create_account(solo, PW, role="admin"))
        login = client.post("/v1/auth/login", json={"username": solo, "password": PW})
        assert login.status_code == 200 and login.json()["is_admin"] is True
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
        assert client.post("/v1/contacts", json=CONTACT, headers=csrf).status_code == 201
        refused = client.request("DELETE", "/v1/auth/me", json={"password": PW}, headers=csrf)
        assert _code(refused) == (409, "last_admin") and "다른 관리자" in refused.json()["message"]
        assert len(client.get("/v1/contacts").json()) == 1 and client.get("/v1/auth/me").status_code == 200
        client.portal.call(lambda: service.create_account(_pg_name("adm"), PW, role="admin"))
        gone = client.request("DELETE", "/v1/auth/me", json={"password": PW}, headers=csrf)
        assert gone.status_code == 200 and gone.json()["contacts"] == 1
        assert client.get("/v1/auth/me").status_code == 401


def test_stale_approve_does_not_lift_a_suspension_on_postgres(pg_url: str) -> None:
    boss, tag = _pg_name("boss"), uuid.uuid4().hex[:10]
    settings = _settings(database_url=pg_url, admin_usernames=boss, auth_signup_requires_approval=True)
    with TestClient(create_app(settings)) as client:
        state = _state(client)
        client.portal.call(state.auth_service.create_account, boss, PW)
        email = f"{tag}@example.com"
        assert client.post("/v1/auth/signup", json={"email": email, "password": PW}).status_code == 202
        login = client.post("/v1/auth/login", json={"username": boss, "password": PW})
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
        member = client.get("/v1/admin/members", params={"query": tag}).json()["items"][0]
        url = f"/v1/admin/members/{member['user_id']}"
        assert member["status"] == "pending"

        def patch(**body: str) -> httpx.Response:
            return client.patch(url, json=body, headers=csrf)

        # 다른 관리자가 그 사이 승인했다가 정지했다 (저장소에 직접 — 다른 관리자의 요청 자리)
        user_id = UUID(member["user_id"])
        client.portal.call(lambda: state.auth.update_user(user_id, status="suspended"))
        # 오래된 화면("승인 대기")에서 누른 승인: 409, 그대로 정지
        stale = patch(status="active", expected_status="pending")
        assert _code(stale) == (409, "member_changed") and "새로" in stale.json()["message"]
        assert client.get("/v1/admin/members", params={"query": tag}).json()["items"][0]["status"] == (
            "suspended"
        )
        assert patch(status="active", expected_status="banned").status_code == 422
        # 저장소의 검사(변경과 한 트랜잭션): 기대 상태가 다르면 바꾸지 않는다
        with pytest.raises(MemberChangedError):
            client.portal.call(
                lambda: state.auth.update_user(user_id, status="active", expected_status="pending")
            )
        with pytest.raises(NotFoundError):
            client.portal.call(
                lambda: state.auth.update_user(uuid.uuid4(), status="active", expected_status="pending")
            )
        # 목록을 새로 읽은 화면(정지)에서 누른 정지 해제는 된다
        lifted = patch(status="active", expected_status="suspended")
        assert lifted.status_code == 200 and lifted.json()["status"] == "active"
        log = client.get("/v1/admin/access-log", params={"limit": 50}).json()
        refused = [
            e["detail"] for e in log if e["action"] == "member_refused" and e["admin_username"] == boss
        ]
        assert len(refused) == 1 and refused[0].endswith("status=active role=- reason=member_changed")
        assert client.delete(url, headers=csrf).status_code == 200
