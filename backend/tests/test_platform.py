"""플랫폼(계약 §4.1): 운영 모드 검사, 보안·캐시 헤더, 오류 모양 통일, 요청 id, 접근 로그, 주기 정리.

요청 제한·본문 상한·Host 검사·CORS는 `test_platform_limits.py`.
"""

from __future__ import annotations

import asyncio
import logging
import re

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app.api.deps import SettingsDep
from app.core.config import Settings
from app.core.guard import (
    DEV_DB_PASSWORD,
    DEV_NEO4J_PASSWORD,
    GuardReport,
    UnsafeConfigError,
    enforce_production_guard,
    inspect_production_settings,
)
from app.core.middleware import CONTENT_SECURITY_POLICY
from app.main import create_app

# 계약서(§4.1)에 적힌 문자열 그대로 — 화면(web·admin)이 이 정책에 맞춰 만들어진다.
CONTRACT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
CRED = {"username": "minsu_kim", "password": "correct horse battery"}
# 운영 모드 검사를 통과하는 최소 설정. 127.0.0.1:1 은 곧바로 접속이 거절되는 주소(엔진은 쓸 때만 붙는다).
PROD_URL = "postgresql+asyncpg://persona:Str0ng-Pass-2026@127.0.0.1:1/persona_ize"
PROD = {
    "app_env": "production",
    "auth_allow_header_stub": False,
    "session_cookie_secure": True,
    "database_url": PROD_URL,
}


def _settings(**overrides) -> Settings:  # noqa: ANN003
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[call-arg]


def _client(**overrides) -> TestClient:  # noqa: ANN003
    return TestClient(create_app(_settings(**overrides)))


# ---------- 운영 모드 검사 ----------


def test_guard_does_nothing_in_development() -> None:
    """개발 기본값은 운영 기준으로는 위험하지만(헤더 스텁·쿠키) 개발 모드에서는 막지도 경고하지도 않는다."""
    dev = _settings()
    assert inspect_production_settings(dev).errors  # 운영 기준으로 보면 문제가 있다
    report = enforce_production_guard(dev)
    assert (report.errors, report.warnings) == ((), ())
    assert _client().get("/healthz").status_code == 200


def test_guard_refuses_each_unsafe_setting_and_lists_all_at_once() -> None:
    def errors(**overrides) -> str:  # noqa: ANN003
        return "\n".join(inspect_production_settings(_settings(**{**PROD, **overrides})).errors)

    assert errors() == ""
    assert "AUTH_ALLOW_HEADER_STUB" in errors(auth_allow_header_stub=True)
    assert "SESSION_COOKIE_SECURE" in errors(session_cookie_secure=False)
    assert "DATABASE_URL" in errors(database_url="")
    dev_url = f"postgresql+asyncpg://persona:{DEV_DB_PASSWORD}@postgres:5432/persona_ize"
    assert DEV_DB_PASSWORD in errors(database_url=dev_url)
    assert DEV_NEO4J_PASSWORD in errors(neo4j_password=DEV_NEO4J_PASSWORD)

    # 개발 기본값 그대로 APP_ENV만 production으로 → 한 번에 전부 알려 주고 시작하지 않는다
    with pytest.raises(UnsafeConfigError) as raised:
        create_app(_settings(app_env="production"))
    message = str(raised.value)
    assert "서버를 시작하지 않습니다" in message
    for name in ("AUTH_ALLOW_HEADER_STUB", "SESSION_COOKIE_SECURE", "DATABASE_URL"):
        assert name in message


def test_guard_logs_refusal_clearly(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.guard"), pytest.raises(UnsafeConfigError):
        create_app(_settings(**{**PROD, "session_cookie_secure": False}))
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1 and "SESSION_COOKIE_SECURE=false" in critical[0].getMessage()
    assert "AUTH_ALLOW_HEADER_STUB" not in critical[0].getMessage()  # 문제없는 항목은 적지 않는다


def test_guard_warns_but_starts(caplog: pytest.LogCaptureFixture) -> None:
    pytest.importorskip("sqlalchemy")
    with caplog.at_level(logging.WARNING, logger="app.core.guard"):
        app = create_app(_settings(**PROD))  # 가짜 모델, 관리자·허용 Host·초대 코드·요청 제한 없음
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    for expected in (
        "LLM_PROVIDER=fake",
        "ADMIN_USERNAMES",
        "TRUSTED_HOSTS",
        "REGISTER_INVITE_CODE",
        "RATE_LIMIT",
    ):
        assert expected in warnings
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert TestClient(app).get("/healthz").status_code == 200


def test_guard_passes_quietly_when_everything_is_set() -> None:
    quiet = _settings(
        **PROD,
        admin_usernames="admin",
        trusted_hosts="persona.example.com",
        register_invite_code="come-in-2026",
        rate_limit_per_minute=120,
        rate_limit_auth_per_minute=10,
        cors_allow_origins="",
    )
    report = inspect_production_settings(quiet)
    assert report.errors == ()
    assert [w for w in report.warnings if "LLM_PROVIDER" not in w] == []  # 테스트라 모델만 가짜
    # 실제 모델 설정이면 경고가 하나도 없다. 키가 비면 알려 준다 (판단만 한다 — 모델을 부르지 않는다)
    real = quiet.model_copy(update={"llm_provider": "anthropic", "anthropic_api_key": "not-a-real-key"})
    assert inspect_production_settings(real) == GuardReport()
    no_key = real.model_copy(update={"anthropic_api_key": ""})
    assert ["ANTHROPIC_API_KEY" in w for w in inspect_production_settings(no_key).warnings] == [True]
    # 가입을 닫으면 초대 코드가 없어도 경고하지 않는다
    closed = _settings(**PROD, auth_allow_register=False)
    assert not any("REGISTER_INVITE_CODE" in w for w in inspect_production_settings(closed).warnings)
    # CORS: 전체 허용·localhost는 경고
    assert any(
        "*" in w for w in inspect_production_settings(_settings(**PROD, cors_allow_origins="*")).warnings
    )
    assert any("localhost" in w for w in inspect_production_settings(_settings(**PROD)).warnings)


def test_production_hides_docs_and_health_details() -> None:
    pytest.importorskip("sqlalchemy")
    client = _client(**PROD)
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["code"] == "not_found", path
        assert r.headers["content-security-policy"] == CONTRACT_CSP  # 운영에서는 예외 없는 CSP
    assert client.get("/healthz").json() == {"status": "ok"}  # 모델 이름·저장소 종류를 내보내지 않는다
    deep = client.get("/healthz?deep=true")  # DB에 닿지 않는 주소 → degraded, 그래도 설정 정보는 없다
    assert deep.status_code == 503
    assert set(deep.json()) == {"status", "checks"} and deep.json()["status"] == "degraded"
    assert deep.json()["checks"]["neo4j"] == "skipped" and deep.json()["checks"]["postgres"].startswith(
        "error"
    )
    assert client.get("/v1/auth/config").json()["app_env"] == "production"
    assert "max-age=" in client.get("/healthz").headers["strict-transport-security"]


def test_development_keeps_docs_and_health_details() -> None:
    client = _client()
    docs = client.get("/docs")
    assert docs.status_code == 200 and "swagger" in docs.text.lower()
    assert "content-security-policy" not in docs.headers  # Swagger 화면은 CDN·인라인 스크립트를 쓴다
    assert docs.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in client.get("/redoc").headers
    spec = client.get("/openapi.json")
    assert spec.status_code == 200 and "/v1/auth/token" in spec.json()["paths"]
    assert spec.headers["content-security-policy"] == CONTRACT_CSP
    assert client.get("/healthz").json()["llm_provider"] == "fake"


# ---------- 보안 헤더·캐시 헤더 ----------


def test_security_headers_on_every_kind_of_response() -> None:
    assert CONTENT_SECURITY_POLICY == CONTRACT_CSP
    client = _client()
    responses = {
        "api": client.get("/v1/auth/config"),
        "page": client.get("/"),
        "static": client.get("/static/proto.js"),
        "unauthorized": client.get("/v1/contacts"),
        "not_found": client.get("/no-such-path"),
        "invalid": client.post("/v1/auth/login", json={}),
    }
    for name, r in responses.items():
        assert r.headers["content-security-policy"] == CONTRACT_CSP, name
        assert r.headers["x-content-type-options"] == "nosniff", name
        assert r.headers["referrer-policy"] == "same-origin", name
        assert r.headers["x-frame-options"] == "DENY", name
        assert r.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()", name
        assert "strict-transport-security" not in r.headers, name  # 쿠키가 Secure가 아니면 HSTS도 없다
        assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"]), name
    assert responses["page"].status_code == 200 and responses["static"].status_code == 200


def test_hsts_only_when_cookie_is_secure() -> None:
    r = _client(session_cookie_secure=True).get("/healthz")
    assert r.headers["strict-transport-security"] == "max-age=31536000"


def test_cache_control_by_path() -> None:
    client = _client()
    assert client.get("/v1/auth/config").headers["cache-control"] == "no-store"
    assert client.get("/v1/contacts").headers["cache-control"] == "no-store"  # 401도
    assert client.get("/healthz").headers["cache-control"] == "no-store"
    for path in ("/", "/static/proto.html", "/static/proto.js", "/static/css/tokens.css", "/favicon.ico"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    assert client.get("/admin").headers["cache-control"] == "no-cache"  # 파일이 아직 없어 404여도 같다
    # 재검증: 바뀌지 않았으면 본문 없이 304, 그 응답에도 같은 헤더
    first = client.get("/static/proto.js")
    again = client.get("/static/proto.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/openapi.json").headers


def test_route_can_override_default_headers() -> None:
    """라우트가 직접 정한 헤더는 덮어쓰지 않는다 (예: 내려받기 응답의 Cache-Control)."""
    from fastapi.responses import JSONResponse

    app = create_app(_settings())
    own_headers = {"Cache-Control": "private, max-age=5", "X-Frame-Options": "SAMEORIGIN"}

    @app.get("/v1/own-headers")
    async def own() -> JSONResponse:
        return JSONResponse({}, headers=own_headers)

    r = TestClient(app).get("/v1/own-headers")
    assert r.headers["cache-control"] == "private, max-age=5"
    assert r.headers.get_list("x-frame-options") == ["SAMEORIGIN"]  # 겹쳐 붙지도 않는다
    assert r.headers["x-content-type-options"] == "nosniff"  # 정하지 않은 것은 기본값


def test_favicon_is_svg_when_present_and_204_otherwise(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("app.main.WEB_DIR", tmp_path)
    (tmp_path / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    client = _client()
    missing = client.get("/favicon.ico")
    assert missing.status_code == 204 and missing.content == b""
    (tmp_path / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    found = client.get("/favicon.ico")
    assert found.status_code == 200 and found.headers["content-type"].startswith("image/svg+xml")
    assert found.text.startswith("<svg")


def test_static_serves_screen_files_but_not_dotfiles_or_unit_tests(
    tmp_path,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.WEB_DIR", tmp_path)
    for name in ("index.html", "js/app.js", "tests/app.test.js", ".env", ".cache/x.js", "js/.hidden.js"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// file", encoding="utf-8")
    client = _client()
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/static/index.html").status_code == 200
    for hidden in (
        "/static/tests/app.test.js",
        "/static/.env",
        "/static/.cache/x.js",
        "/static/js/.hidden.js",
    ):
        r = client.get(hidden)
        assert r.status_code == 404 and r.json()["code"] == "not_found", hidden
    assert client.get("/static/js/../.env").status_code == 404  # 경로를 꼬아도 같다
    assert client.get("/static/").status_code == 404  # 폴더 목록은 없다


def test_settings_dependency_returns_this_apps_settings() -> None:
    """`SettingsDep`은 전역 설정이 아니라 `create_app`에 넘긴 설정을 준다."""
    app = create_app(_settings(consent_version="only-in-this-app"))

    @app.get("/v1/consent-version")
    async def version(settings: SettingsDep) -> dict[str, str]:
        return {"version": settings.consent_version}

    assert TestClient(app).get("/v1/consent-version").json() == {"version": "only-in-this-app"}


# ---------- 오류 모양 ----------


def _assert_error(r, status: int, code: str) -> dict:  # noqa: ANN001
    assert r.status_code == status, r.text
    body = r.json()
    assert body["code"] == code, body
    assert isinstance(body["message"], str) and re.search("[가-힣]", body["message"]), body
    return body


def test_error_shape_401_403_404_405_409() -> None:
    client = _client()
    # 401 (로그인 없음): 최상위 code·message + 예전 모양 detail도 그대로
    body = _assert_error(client.get("/v1/contacts"), 401, "unauthorized")
    assert body["detail"] == {"code": "unauthorized", "message": body["message"]}
    # 401 (도메인 예외): 원래부터 최상위였다 — detail 없이 그대로
    bad_login = client.post("/v1/auth/login", json={"username": "nobody_here", "password": "whatever-1"})
    assert set(_assert_error(bad_login, 401, "unauthorized")) == {"code", "message"}

    reg = client.post("/v1/auth/register", json=CRED)
    assert reg.status_code == 201
    # 403 (CSRF 없음)
    no_csrf = client.post("/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"})
    assert set(_assert_error(no_csrf, 403, "forbidden")) == {"code", "message"}
    # 404: 없는 주소(예전 {"detail":"Not Found"}), 남의 것·없는 자원(도메인 예외), 없는 정적 파일
    assert _assert_error(client.get("/v1/nope"), 404, "not_found")["detail"] == "Not Found"
    missing = client.get("/v1/contacts/00000000-0000-0000-0000-000000000000")
    assert set(_assert_error(missing, 404, "not_found")) == {"code", "message"}
    assert _assert_error(client.get("/static/no-such-file.js"), 404, "not_found")["detail"] == "Not Found"
    # 405: Allow 헤더를 잃지 않는다
    wrong_method = client.put("/v1/auth/login", json=CRED)
    assert _assert_error(wrong_method, 405, "method_not_allowed")["detail"] == "Method Not Allowed"
    assert wrong_method.headers["allow"] == "POST"
    # 409
    assert set(_assert_error(client.post("/v1/auth/register", json=CRED), 409, "conflict")) == {
        "code",
        "message",
    }


def test_missing_page_is_404_with_both_shapes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """화면 파일이 아직 없을 때(/admin·/privacy·/terms): 라우트가 올린 detail(dict)을 최상위로도 올린다."""
    monkeypatch.setattr("app.main.WEB_DIR", tmp_path)
    (tmp_path / "terms.html").write_text("<!doctype html><title>약관</title>", encoding="utf-8")
    client = _client()
    page = _assert_error(client.get("/admin"), 404, "not_found")
    assert page == {
        "code": "not_found",
        "message": "없는 화면입니다.",
        "detail": {"code": "not_found", "message": "없는 화면입니다."},
    }
    terms = client.get("/terms")
    assert terms.status_code == 200 and terms.headers["cache-control"] == "no-cache"


def test_error_shape_422_keeps_fastapi_detail_and_adds_summary() -> None:
    client = _client()
    r = client.post("/v1/auth/login", json={"username": "ab"})
    body = _assert_error(r, 422, "invalid_input")
    assert body["message"] == "username: 3자 이상이어야 합니다. (외 1건)"
    assert body["details"] == [
        {"loc": "body.username", "msg": "3자 이상이어야 합니다"},
        {"loc": "body.password", "msg": "필수 항목입니다"},
    ]
    # 예전 모양(FastAPI 목록)도 그대로 — 옛 화면이 detail[].msg를 읽는다
    assert [d["type"] for d in body["detail"]] == ["string_too_short", "missing"]
    assert body["detail"][0]["loc"] == ["body", "username"] and "msg" in body["detail"][0]
    assert body["detail"][0]["input"] == "ab"

    # 깨진 JSON, 본문 없음, 쿼리·경로 값
    broken = client.post("/v1/auth/login", content=b"{not json", headers={"Content-Type": "application/json"})
    assert _assert_error(broken, 422, "invalid_input")["message"] == "본문: JSON 형식이 올바르지 않습니다."
    assert _assert_error(client.post("/v1/auth/login"), 422, "invalid_input")["message"].startswith(
        "본문: 필수"
    )
    h = {"X-User-Id": "user-a"}
    limit = _assert_error(client.get("/v1/rewrites?limit=0", headers=h), 422, "invalid_input")
    assert limit["details"] == [{"loc": "query.limit", "msg": "1 이상이어야 합니다"}]
    bad_id = _assert_error(client.get("/v1/contacts/not-a-uuid", headers=h), 422, "invalid_input")
    assert bad_id["details"][0] == {"loc": "path.contact_id", "msg": "올바른 id 형식이 아닙니다"}
    kind = client.post("/v1/contacts", json={"alias": "박지현", "relationship_type": "boss"}, headers=h)
    assert "허용되는 값이 아닙니다" in _assert_error(kind, 422, "invalid_input")["message"]
    # 도메인 규칙 위반 422는 원래 모양 그대로
    short = client.post("/v1/auth/register", json={"username": "fine_name", "password": "short"})
    assert set(_assert_error(short, 422, "invalid_input")) == {"code", "message"}


def test_error_shape_400_when_body_is_not_utf8() -> None:
    """Windows 콘솔에서 curl로 한글 본문을 보내면 CP949 바이트가 온다 — 원인을 알 수 있는 문구로 답한다."""
    cp949 = '{"username": "민수", "password": "x"}'.encode("cp949")
    r = _client().post("/v1/auth/login", content=cp949, headers={"Content-Type": "application/json"})
    body = _assert_error(r, 400, "bad_request")
    assert "UTF-8" in body["message"] and body["detail"] == "There was an error parsing the body"


def test_validation_error_never_echoes_secrets_or_whole_body() -> None:
    """FastAPI 기본값은 빠진 필드가 있으면 본문 전체를, 틀린 값이면 그 값을 `input`으로 되돌려 준다."""
    client = _client()
    secret = "my-very-secret-password"
    missing_name = client.post("/v1/auth/login", json={"password": secret})
    assert missing_name.status_code == 422 and secret not in missing_name.text
    wrong_type = client.post(
        "/v1/auth/password", json={"current_password": [secret], "new_password": secret * 10}
    )
    assert wrong_type.status_code == 422 and secret not in wrong_type.text
    invite = client.post("/v1/auth/register", json={**CRED, "invite_code": "x" * 200})
    assert invite.status_code == 422 and "xxxx" not in invite.text
    long_value = client.post("/v1/auth/login", json={"username": "u" * 500, "password": "p"})
    assert long_value.status_code == 422 and "u" * 101 not in long_value.text


def test_error_shape_500_has_request_id_and_hides_the_cause(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(_settings())

    @app.get("/v1/boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter2")

    @app.get("/v1/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="찻주전자입니다")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="app.core.middleware"):
        r = client.get("/v1/boom", headers={"Origin": "http://localhost:5173"})
    body = _assert_error(r, 500, "internal_error")
    assert set(body) == {"code", "message", "request_id"}
    assert body["request_id"] == r.headers["x-request-id"]
    assert "hunter2" not in r.text and "RuntimeError" not in r.text
    # 500에도 보안 헤더·캐시 헤더·CORS 헤더가 똑같이 붙는다
    assert r.headers["content-security-policy"] == CONTRACT_CSP and r.headers["cache-control"] == "no-store"
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    # 원인은 서버 로그에만: 스택과 요청 id. 스택은 값 없는 모양으로 메시지에 들어 있고 예외 객체는 달지 않는다
    # (포매터가 예외 메시지를 그대로 찍지 못하게 — test_platform_review.py의 C2)
    errors = [rec for rec in caplog.records if rec.name == "app.core.middleware"]
    assert len(errors) == 1 and errors[0].exc_info is None
    assert errors[0].request_id == body["request_id"]
    logged = errors[0].getMessage()
    assert "RuntimeError" in logged and "/v1/boom" in logged
    assert "Traceback (most recent call last):" in logged and "in boom" in logged

    # 모르는 상태 코드의 HTTPException도 같은 모양 (한국어 detail은 메시지로 쓴다)
    tea = client.get("/v1/teapot")
    assert tea.json() == {"code": "http_error", "message": "찻주전자입니다", "detail": "찻주전자입니다"}

    # 기본 테스트 클라이언트는 예외를 그대로 올린다 — 서버 버그가 테스트에서 숨지 않는다
    with pytest.raises(RuntimeError, match="hunter2"):
        TestClient(app).get("/v1/boom")


def test_server_does_not_log_the_same_traceback_twice() -> None:
    """uvicorn은 다시 올라온 예외를 "Exception in ASGI application"으로 찍는다 — 이미 남긴 것은 거른다."""
    from app.core.logs import configure_logging, mark_reported

    configure_logging("INFO")
    server_log = logging.getLogger("uvicorn.error")
    seen: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = seen.append  # type: ignore[method-assign]
    server_log.addHandler(handler)
    try:
        reported, fresh = RuntimeError("a"), RuntimeError("b")
        mark_reported(reported)
        server_log.error("Exception in ASGI application\n", exc_info=reported)
        server_log.error("Exception in ASGI application\n", exc_info=fresh)
        server_log.error("no exception attached")
    finally:
        server_log.removeHandler(handler)
    # 앱이 남긴 것은 버려지고, 남기지 않은 것은 (값을 뺀 모양으로) 메시지에 들어간다
    assert [r.getMessage() for r in seen] == [
        "Exception in ASGI application\nRuntimeError: b",
        "no exception attached",
    ]
    assert logging.getLogger("uvicorn.access").disabled  # 접근 로그는 앱이 한 줄로 남긴다


# ---------- 요청 id·접근 로그·로깅 수준 ----------


def test_request_id_is_issued_accepted_or_replaced() -> None:
    client = _client()
    issued = client.get("/healthz").headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", issued)
    assert client.get("/healthz").headers["x-request-id"] != issued
    given = "trace-2026.11.11_abc"
    assert client.get("/healthz", headers={"X-Request-ID": given}).headers["x-request-id"] == given
    for bad in ("short", "x" * 65, "has space inside", "semi;colon=1", "한글아이디입니다"):
        got = client.get("/healthz", headers={"X-Request-ID": bad.encode("utf-8")}).headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{32}", got), bad


def test_request_id_is_visible_to_routes_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    from app.core.logs import current_request_id

    app = create_app(_settings())
    seen: dict[str, str] = {}

    async def probe(request: Request) -> dict[str, str]:
        seen["state"] = request.state.request_id
        seen["context"] = current_request_id()
        return {}

    app.add_api_route("/v1/probe", probe, methods=["GET"])
    with caplog.at_level(logging.INFO, logger="app.access"):
        r = TestClient(app).get("/v1/probe", headers={"X-Request-ID": "probe-request-0001"})
    assert seen == {"state": "probe-request-0001", "context": "probe-request-0001"}
    assert r.headers["x-request-id"] == "probe-request-0001"
    assert [rec.request_id for rec in caplog.records if rec.name == "app.access"] == ["probe-request-0001"]
    assert current_request_id() == "-"  # 요청 밖


def test_access_log_has_method_path_status_but_no_query_cookie_or_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client()
    with caplog.at_level(logging.INFO, logger="app.access"):
        client.post(
            "/v1/auth/login?next=QUERY-SECRET",
            json={"username": "minsu_kim", "password": "BODY-SECRET-PASSWORD"},
            headers={"Authorization": "Bearer TOKEN-SECRET", "Cookie": "pi_session=COOKIE-SECRET"},
        )
        client.get("/healthz")  # 헬스체크는 몇 초마다 온다 → DEBUG
        client.get("/v1/line%0Abreak%0D%0AINFO%20forged")
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "app.access"]
    assert len(lines) == 2
    assert re.fullmatch(r"POST /v1/auth/login 401 \d+ms", lines[0])
    assert not any(s in "\n".join(lines) for s in ("SECRET", "next=", "pi_session", "Bearer"))
    assert (
        "\n" not in lines[1]
        and "\r" not in lines[1]
        and lines[1].startswith("GET /v1/line?break??INFO forged")
    )


def test_warns_once_when_forwarded_header_is_not_applied(caplog: pytest.LogCaptureFixture) -> None:
    """프록시 뒤인데 uvicorn이 X-Forwarded-For를 적용하지 않으면 모든 사용자가 프록시 주소 하나로 보인다."""
    behind_proxy = {"X-Forwarded-For": "198.51.100.7"}
    untrusted = TestClient(create_app(_settings()), client=("172.18.0.5", 40000))  # 연결 주소 = 프록시
    with caplog.at_level(logging.WARNING, logger="app.core.middleware"):
        assert untrusted.get("/healthz").status_code == 200  # 헤더가 없으면 볼 것도 없다
        assert caplog.records == []
        for _ in range(3):
            assert untrusted.get("/healthz", headers=behind_proxy).status_code == 200
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "--forwarded-allow-ips" in warnings[0] and "172.18.0.5" in warnings[0]
    assert "198.51.100.7" not in warnings[0]  # 헤더 값(사용자 주소)은 로그에 옮기지 않는다

    caplog.clear()
    # uvicorn이 적용했다면 연결 주소가 곧 헤더 속 주소다 → 조용하다
    trusted = TestClient(create_app(_settings()), client=("198.51.100.7", 0))
    with caplog.at_level(logging.WARNING, logger="app.core.middleware"):
        trusted.get("/healthz", headers={"X-Forwarded-For": "198.51.100.7, 172.18.0.5"})
    assert caplog.records == []


def test_log_level_setting(caplog: pytest.LogCaptureFixture) -> None:
    from app.core.logs import configure_logging, resolve_level

    root, app_logger = logging.getLogger(), logging.getLogger("app")
    before = (root.level, app_logger.level)
    try:
        # DEBUG는 앱 로거에만 건다. 루트를 내리면 모델 SDK·HTTP 클라이언트가 요청 본문을 로그에 쏟는다
        assert configure_logging("debug") == logging.DEBUG
        assert (app_logger.level, root.level) == (logging.DEBUG, logging.INFO)
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.INFO
        # INFO 이상이면 앱 로거는 따로 정하지 않고 루트를 따른다 (예전 동작 그대로)
        assert configure_logging("WARNING") == logging.WARNING
        assert (app_logger.level, root.level) == (logging.NOTSET, logging.WARNING)
        assert logging.getLogger("app.main").getEffectiveLevel() == logging.WARNING
        with caplog.at_level(logging.WARNING, logger="app.core.logs"):
            assert configure_logging("chatty") == logging.INFO  # 모르는 값 → INFO + 경고, 서버는 뜬다
        assert any("LOG_LEVEL" in r.getMessage() for r in caplog.records)
        assert resolve_level("notset") is None and resolve_level(" Error ") == logging.ERROR
        create_app(_settings(log_level="ERROR"))
        assert logging.getLogger("app.main").getEffectiveLevel() == logging.ERROR
    finally:
        root.setLevel(before[0])
        app_logger.setLevel(before[1])


# ---------- 주기 정리 ----------


@pytest.mark.anyio
async def test_housekeeping_wrapper_falls_back_to_class() -> None:
    """`services.housekeeping.run_housekeeping`이 없으면 기존 `Housekeeping` 클래스로 한 번 돈다."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from app.api.deps import build_state
    from app.core.periodic import run_housekeeping_once
    from app.domain.auth import Session
    from app.services import housekeeping

    if hasattr(housekeeping, "run_housekeeping"):
        pytest.skip("run_housekeeping이 이미 있다 — 대체 경로(Housekeeping 클래스 직접 호출)는 쓰이지 않는다")
    state = build_state(_settings())
    now = datetime.now(UTC)
    for key, expires in (("old", now - timedelta(days=30)), ("live", now + timedelta(days=1))):
        await state.auth.create_session(
            Session(
                token_hash=key,
                user_id=uuid4(),
                csrf_token="c",
                created_at=now - timedelta(days=40),
                expires_at=expires,
            )
        )
    removed = await run_housekeeping_once(state)
    assert removed == {"sessions": 1, "import_jobs": 0, "usage_rows": 0, "login_throttle_keys": 0}
    assert await state.auth.get_session("old") is None and await state.auth.get_session("live") is not None


@pytest.mark.anyio
async def test_housekeeping_wrapper_prefers_run_housekeeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """data 담당이 `run_housekeeping(state) -> dict`를 제공하면 그것을 쓴다 (계약 §4.5)."""
    from app.api.deps import build_state
    from app.core.periodic import run_housekeeping_once

    calls: list[object] = []

    async def fake_run(state) -> dict[str, int]:  # noqa: ANN001
        calls.append(state)
        return {"sessions": 2, "rewrites": 7}

    monkeypatch.setattr("app.services.housekeeping.run_housekeeping", fake_run, raising=False)
    state = build_state(_settings())
    removed = await run_housekeeping_once(state)
    assert calls == [state]
    assert removed == {"sessions": 2, "rewrites": 7, "login_throttle_keys": 0}


@pytest.mark.anyio
async def test_housekeeping_loop_survives_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.core import periodic

    sleeps: list[float] = []
    runs: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError  # lifespan이 서버를 내릴 때처럼

    async def flaky(_state) -> dict[str, int]:  # noqa: ANN001
        runs.append(len(runs))
        if len(runs) == 1:
            raise RuntimeError("db down")
        return {"sessions": len(runs)}

    monkeypatch.setattr(periodic, "run_housekeeping_once", flaky)
    with caplog.at_level(logging.INFO, logger="app.core.periodic"), pytest.raises(asyncio.CancelledError):
        await periodic.housekeeping_loop(object(), 7200.0, sleep=fake_sleep)
    assert sleeps == [60.0, 7200.0, 7200.0, 7200.0]  # 시작 1분 뒤 첫 정리, 그 뒤로 주기마다
    assert runs == [0, 1, 2]  # 첫 실패 뒤에도 계속 돈다
    messages = [r.getMessage() for r in caplog.records]
    assert any("housekeeping failed" in m for m in messages) and any("sessions=3" in m for m in messages)


def test_housekeeping_task_runs_only_when_interval_is_set() -> None:
    def task_names(**overrides) -> set[str]:  # noqa: ANN003
        app = create_app(_settings(**overrides))
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            return {t.get_name() for t in app.state.background_tasks}

    assert "housekeeping" not in task_names()
    assert "housekeeping" in task_names(housekeeping_interval_hours=6)


@pytest.mark.anyio
async def test_periodic_loop_also_resumes_pending_graph_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    """주기 작업은 정리 뒤에 그래프 미수집분도 이어서 넣는다 — 끄는 스위치는 GRAPH_RESUME_ON_START."""
    from types import SimpleNamespace

    from app.core import periodic

    class FakeIngest:
        def __init__(self) -> None:
            self.calls = 0

        async def resume_all(self) -> dict[str, int]:
            self.calls += 1
            return {"user-1": 3}

    ingest = FakeIngest()
    on = SimpleNamespace(graph_ingest=ingest, settings=_settings())
    assert await periodic.resume_graph_once(on) == {"user-1": 3}
    off = SimpleNamespace(graph_ingest=ingest, settings=_settings(graph_resume_on_start=False))
    assert await periodic.resume_graph_once(off) == {}
    assert await periodic.resume_graph_once(SimpleNamespace(graph_ingest=None, settings=_settings())) == {}
    assert ingest.calls == 1

    # 루프: 정리가 실패해도 수집은 돌고, 수집이 실패해도 루프는 계속된다
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    async def broken_housekeeping(_state) -> dict[str, int]:  # noqa: ANN001
        raise RuntimeError("db down")

    resumes: list[int] = []

    async def flaky_resume(_state) -> dict[str, int]:  # noqa: ANN001
        resumes.append(len(resumes))
        if len(resumes) == 1:
            raise RuntimeError("neo4j down")
        return {}

    monkeypatch.setattr(periodic, "run_housekeeping_once", broken_housekeeping)
    monkeypatch.setattr(periodic, "resume_graph_once", flaky_resume)
    with pytest.raises(asyncio.CancelledError):
        await periodic.housekeeping_loop(on, 3600.0, sleep=fake_sleep)
    assert resumes == [0, 1]


def test_import_message_text_is_exempt_from_the_unstorable_text_guard() -> None:
    """가져오기의 대화 본문만 예외다(가져오기 쪽이 깨진 글자를 바꿔 읽는다). 다른 칸은 그대로 막는다."""
    from app.core.text_guard import _import_text_exemption, find_unstorable

    bad = "앞\x00뒤"
    parse = _import_text_exemption("/v1/imports/parse")
    raw = _import_text_exemption("/v1/conversations/1dfd5ea0/imports/raw")
    messages = _import_text_exemption("/v1/conversations/1dfd5ea0/imports")
    assert parse is not None and raw is not None and messages is not None
    assert _import_text_exemption("/v1/contacts") is None
    assert _import_text_exemption("/v1/rewrites") is None

    assert find_unstorable({"format": "auto", "text": bad}, parse) is None
    assert find_unstorable({"format": bad, "text": "x"}, parse) == "format"
    assert find_unstorable({"text": bad, "speaker_map": {"김민수": "me"}}, raw) is None
    assert find_unstorable({"text": "x", "speaker_map": {"a": bad}}, raw) == "speaker_map.a"
    assert find_unstorable({"messages": [{"id": "m1", "text": bad}]}, messages) is None
    assert find_unstorable({"messages": [{"id": bad, "text": "x"}]}, messages) == "messages.0.id"
    assert find_unstorable({"draft_text": bad}, None) == "draft_text"
