"""플랫폼(계약 §4.1): 본문 상한, IP 기준 요청 제한, Host 검사, 설정 기반 CORS.

요청 제한은 시계를 바꿔 끼워 결정적으로 본다 (`app.state.rate_limiter.clock`).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.middleware import (
    BodyLimitMiddleware,
    HostCheckMiddleware,
    host_without_port,
    normalize_host_patterns,
)
from app.core.ratelimit import RateLimiter, SlidingWindowLimiter, client_key
from app.main import create_app

CRED = {"username": "minsu_kim", "password": "correct horse battery"}


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settings(**overrides) -> Settings:  # noqa: ANN003
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[call-arg]


def _app_with_clock(**overrides):  # noqa: ANN003, ANN202
    app = create_app(_settings(**overrides))
    clock = FakeClock()
    app.state.rate_limiter.clock = clock
    return app, clock


async def _call(app, *, method: str = "POST", path: str = "/", headers=(), chunks=(b"",)) -> list[dict]:  # noqa: ANN001
    """ASGI 앱을 직접 부른다 — 본문을 여러 조각(chunked)으로 흘려보낼 수 있다."""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": ("203.0.113.9", 40000),
        "server": ("testserver", 80),
    }
    pending = list(chunks)
    sent: list[dict] = []

    async def receive() -> dict:
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


# ---------- 본문 상한 ----------


def test_body_limit_by_content_length_and_by_actual_size() -> None:
    client = TestClient(create_app(_settings(max_body_bytes=300)))
    json_headers = {"Content-Type": "application/json"}

    ok = client.post("/v1/auth/register", json=CRED)
    assert ok.status_code == 201  # 상한 안쪽은 그대로

    big = b'{"username": "minsu_kim", "password": "' + b"x" * 400 + b'"}'
    declared = client.post("/v1/auth/login", content=big, headers=json_headers)
    assert declared.status_code == 413
    assert declared.json() == {
        "code": "payload_too_large",
        "message": "요청 본문이 너무 큽니다 (최대 300바이트).",
    }
    assert declared.headers["connection"] == "close"  # 남은 본문을 끝까지 받지 않는다
    assert declared.headers["x-request-id"] and declared.headers["cache-control"] == "no-store"

    # 길이를 밝히지 않은 요청(chunked): 실제로 받은 양으로 막는다
    def stream():  # noqa: ANN202
        yield big[:200]
        yield big[200:]

    streamed = client.post("/v1/auth/login", content=stream(), headers=json_headers)
    assert "content-length" not in streamed.request.headers
    assert streamed.status_code == 413 and streamed.json()["code"] == "payload_too_large"

    # 기본 상한(8MB)에서는 큰 가져오기 본문도 통과한다 — 여기서는 검증 오류(422)까지 간다
    default = TestClient(create_app(_settings()))
    assert default.post("/v1/auth/login", content=b"x" * 400_000, headers=json_headers).status_code == 422
    assert default.post("/v1/auth/login", content=b"x" * 3_000_001, headers=json_headers).status_code == 422
    too_big = b"x" * (_settings().max_body_bytes + 1)
    assert default.post("/v1/auth/login", content=too_big, headers=json_headers).status_code == 413


@pytest.mark.anyio
async def test_body_limit_counts_chunks_and_stops_reading() -> None:
    received: list[int] = []

    async def reader(scope, receive, send) -> None:  # noqa: ANN001
        while True:
            message = await receive()
            received.append(len(message.get("body", b"")))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limited = BodyLimitMiddleware(reader, max_bytes=10)
    assert _status(await _call(limited, chunks=[b"12345", b"67890"])) == 200  # 딱 상한까지는 받는다
    assert received == [5, 5]

    received.clear()
    sent = await _call(limited, chunks=[b"123456", b"7890A", b"never read"])
    assert _status(sent) == 413 and b"payload_too_large" in sent[1]["body"]
    assert received == [6]  # 넘는 조각에서 멈췄고 그 뒤는 읽지 않았다

    # Content-Length가 상한을 넘으면 본문을 읽기도 전에 거절
    received.clear()
    sent = await _call(limited, headers=[("Content-Length", "11")], chunks=[b"x" * 11])
    assert _status(sent) == 413 and received == []
    # 이상한 Content-Length는 믿지 않고 실제 양을 센다
    assert _status(await _call(limited, headers=[("Content-Length", "1_0")], chunks=[b"x" * 10])) == 200

    # 응답을 이미 시작한 뒤에 넘치면 응답을 덮어쓸 수 없다 → 예외를 그대로 올린다
    async def late_reader(scope, receive, send) -> None:  # noqa: ANN001
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()

    from app.core.http_errors import PayloadTooLargeError

    with pytest.raises(PayloadTooLargeError):
        await _call(BodyLimitMiddleware(late_reader, max_bytes=3), chunks=[b"too big"])


# ---------- 요청 제한 ----------


def test_sliding_window_limiter_is_exact_and_cleans_up() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(3, 60.0, clock=clock)
    assert [limiter.hit("a") for _ in range(3)] == [None, None, None]
    assert limiter.hit("a") == 60.0  # 가장 오래된 요청이 창을 벗어날 때까지
    assert limiter.hit("b") is None  # 키마다 따로
    clock.advance(20)
    assert limiter.hit("a") == 40.0
    assert limiter.hit("a") == 40.0  # 거절된 요청은 세지 않는다 (기다리면 풀린다)
    clock.advance(40)
    assert limiter.hit("a") is None  # 60초 전 요청 3건이 빠졌다
    assert [limiter.hit("a"), limiter.hit("a")] == [None, None]
    assert limiter.hit("a") == 60.0
    # 어느 60초 구간에도 허용은 3건을 넘지 않는다: 59초 뒤에도 아직 막힘, 60초 뒤 풀림
    clock.advance(59)
    assert limiter.hit("a") == 1.0
    clock.advance(1)
    assert limiter.hit("a") is None

    with pytest.raises(ValueError):
        SlidingWindowLimiter(0)


def test_rate_limiter_keys_do_not_pile_up() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(5, 60.0, clock=clock)
    for i in range(500):  # 주소를 바꿔 가며 한 번씩
        limiter.hit(f"198.51.100.{i}")
    assert len(limiter) == 500
    clock.advance(61)
    limiter.hit("fresh")  # 창이 지난 뒤의 첫 요청이 지난 키를 쓸어 낸다
    assert len(limiter) == 1

    # 한 창 안에 주소가 폭주해도 기억하는 키 수에는 상한이 있다 (오래된 키부터 버린다)
    capped = SlidingWindowLimiter(5, 60.0, clock=clock, max_keys=100)
    for i in range(1_000):
        capped.hit(f"k{i}")
    assert len(capped) <= 101
    assert capped.hit("k999") is None and capped.sweep() == 0


def test_client_key_groups_ipv6_by_64_and_handles_odd_values() -> None:
    assert client_key("203.0.113.7") == "203.0.113.7"
    assert client_key("2001:db8:1:2:aaaa::1") == client_key("2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:3::1") != client_key("2001:db8:1:2::1")
    assert client_key("::ffff:203.0.113.7") == "203.0.113.7"
    assert client_key("testclient") == "testclient"
    assert client_key(None) == client_key("") == "unknown"


def test_rate_limit_is_off_by_default() -> None:
    app = create_app(_settings())
    assert app.state.rate_limiter.enabled is False
    client = TestClient(app)
    assert {client.get("/v1/auth/config").status_code for _ in range(50)} == {200}
    assert RateLimiter(0, 0).check("POST", "/v1/auth/login", "203.0.113.7") is None


def test_rate_limit_general_per_ip_with_retry_after() -> None:
    app, clock = _app_with_clock(rate_limit_per_minute=3)
    client = TestClient(app, client=("203.0.113.7", 50000))
    other = TestClient(app, client=("203.0.113.8", 50000))

    assert [client.get("/v1/auth/config").status_code for _ in range(3)] == [200, 200, 200]
    blocked = client.get("/v1/auth/config", headers={"Origin": "http://localhost:5173"})
    assert blocked.status_code == 429
    assert blocked.json() == {
        "code": "rate_limited",
        "message": "요청이 너무 많습니다. 60초 뒤에 다시 시도하세요.",
    }
    assert blocked.headers["retry-after"] == "60"
    # 제한 응답에도 요청 id·보안 헤더·CORS 헤더가 붙는다 (다른 출처의 화면도 사유를 읽을 수 있다)
    assert blocked.headers["x-request-id"] and blocked.headers["x-content-type-options"] == "nosniff"
    assert blocked.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "retry-after" in blocked.headers["access-control-expose-headers"].lower()

    assert other.get("/v1/auth/config").status_code == 200  # 다른 IP는 따로 센다
    # /v1 밖(헬스체크·화면·정적 파일)은 세지도 막지도 않는다
    for path in ("/healthz", "/healthz?deep=true", "/", "/static/proto.js"):
        assert {client.get(path).status_code for _ in range(5)} == {200}, path

    clock.advance(30.2)
    assert client.get("/v1/auth/config").headers["retry-after"] == "30"  # 올림
    clock.advance(29.9)
    assert client.get("/v1/auth/config").status_code == 200  # 창이 지나면 풀린다


def test_rate_limit_auth_paths_have_their_own_budget() -> None:
    app, clock = _app_with_clock(rate_limit_auth_per_minute=2)
    client = TestClient(app, client=("203.0.113.7", 50000))
    bad = {"username": "nobody_here", "password": "whatever-1"}

    assert [client.post("/v1/auth/login", json=bad).status_code for _ in range(2)] == [401, 401]
    blocked = client.post("/v1/auth/login", json=bad)
    assert blocked.status_code == 429 and blocked.json()["code"] == "rate_limited"
    assert blocked.headers["retry-after"] == "60"
    # 같은 묶음: 가입·토큰 발급·비밀번호 변경·계정 삭제
    assert client.post("/v1/auth/register", json=CRED).status_code == 429
    assert client.post("/v1/auth/token", json=bad).status_code == 429
    assert client.post("/v1/auth/password", json={}).status_code == 429
    assert client.request("DELETE", "/v1/auth/me", json={}).status_code == 429
    # 인증 한도는 다른 API를 막지 않는다 (전체 한도는 꺼져 있다)
    assert {client.get("/v1/auth/config").status_code for _ in range(10)} == {200}
    assert client.get("/v1/auth/me").status_code == 401
    assert client.post("/v1/auth/logout").status_code == 204

    clock.advance(60.1)
    assert client.post("/v1/auth/register", json=CRED).status_code == 201


def test_rate_limit_both_budgets_together() -> None:
    app, _ = _app_with_clock(rate_limit_per_minute=4, rate_limit_auth_per_minute=2)
    client = TestClient(app, client=("203.0.113.7", 50000))
    bad = {"username": "nobody_here", "password": "whatever-1"}
    assert [client.post("/v1/auth/login", json=bad).status_code for _ in range(3)] == [401, 401, 429]
    # 인증 한도에 막힌 요청도 /v1 요청 1건이다 → 전체 한도 4건 중 3건을 썼다
    assert [client.get("/v1/auth/config").status_code for _ in range(2)] == [200, 429]


# ---------- Host 검사 ----------


def test_host_check_allows_listed_hosts_and_always_healthz() -> None:
    client = TestClient(create_app(_settings(trusted_hosts="persona.example.com, *.example.org,[::1]")))

    def get(path: str, host: str) -> int:
        return client.get(path, headers={"Host": host}).status_code

    assert get("/v1/auth/config", "persona.example.com") == 200
    assert get("/v1/auth/config", "PERSONA.example.com:8443") == 200  # 대소문자·포트 무시
    assert get("/v1/auth/config", "api.example.org") == 200
    assert get("/v1/auth/config", "[::1]:8000") == 200
    rejected = client.get("/v1/auth/config")  # 테스트 클라이언트의 Host는 testserver
    assert rejected.status_code == 400
    assert rejected.json() == {"code": "invalid_host", "message": "허용되지 않은 주소(Host)로 온 요청입니다."}
    assert rejected.headers["x-request-id"] and rejected.headers["x-frame-options"] == "DENY"
    for bad in ("example.org", "evilpersona.example.com", "persona.example.com.evil.io", "exa mple.com"):
        assert get("/", bad) == 400, bad
    # 헬스체크는 컨테이너·프록시가 내부 주소(localhost, 서비스 이름, IP)로 부른다
    for internal in ("localhost:8000", "backend:8000", "10.0.0.5", "testserver"):
        assert get("/healthz", internal) == 200, internal
    assert get("/healthz?deep=true", "10.0.0.5") == 200
    assert get("/healthz/", "10.0.0.5") == 400  # 정확히 그 경로만 예외


def test_host_check_is_off_by_default_and_rejects_bad_patterns() -> None:
    assert (
        TestClient(create_app(_settings())).get("/", headers={"Host": "anything.example"}).status_code == 200
    )
    assert normalize_host_patterns([" Example.COM ", "*.example.org", "", "*"]) == [
        "example.com",
        "*.example.org",
        "*",
    ]
    for bad in ("example.com:8000", "https://example.com", "*example.com", "ex*.com", "*.[::1]", "a/b"):
        with pytest.raises(ValueError, match="TRUSTED_HOSTS"):
            normalize_host_patterns([bad])
    with pytest.raises(ValueError, match="TRUSTED_HOSTS"):
        create_app(_settings(trusted_hosts="https://persona.example.com"))
    assert HostCheckMiddleware(None, allowed_hosts=["*"]).allowed("anything")  # type: ignore[arg-type]

    assert host_without_port("Example.com:443") == "example.com"
    assert host_without_port("[2001:DB8::1]:8000") == "[2001:db8::1]"
    for broken in (None, "", "example.com:port", "[::1", "[::1]x", "[not-ipv6]", "a b", "host:"):
        assert host_without_port(broken) is None, broken


# ---------- CORS ----------


def test_cors_follows_settings() -> None:
    dev_origin = {"Origin": "http://localhost:5173"}
    preflight = {**dev_origin, "Access-Control-Request-Method": "POST"}

    default = TestClient(create_app(_settings()))  # 개발 기본값: React 개발 서버 두 곳
    allowed = default.get("/v1/auth/config", headers=dev_origin)
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "x-request-id" in allowed.headers["access-control-expose-headers"].lower()
    pre = default.options(
        "/v1/auth/login", headers={**preflight, "Access-Control-Request-Headers": "x-csrf-token"}
    )
    assert pre.status_code == 200 and "POST" in pre.headers["access-control-allow-methods"]
    assert pre.headers["access-control-allow-headers"] == "x-csrf-token"
    stranger = default.get("/v1/auth/config", headers={"Origin": "https://evil.example"})
    assert stranger.status_code == 200 and "access-control-allow-origin" not in stranger.headers

    off = TestClient(create_app(_settings(cors_allow_origins="")))  # 같은 출처 화면만 → 미들웨어 없음
    same_site = off.get("/v1/auth/config", headers=dev_origin)
    assert same_site.status_code == 200
    assert (
        not [h for h in same_site.headers if h.startswith("access-control-")]
        and "vary" not in same_site.headers
    )
    assert off.options("/v1/auth/login", headers=preflight).status_code == 405

    custom = TestClient(
        create_app(_settings(cors_allow_origins="https://app.example.com , https://m.example.com"))
    )
    assert (
        custom.get("/healthz", headers={"Origin": "https://m.example.com"}).headers[
            "access-control-allow-origin"
        ]
        == "https://m.example.com"
    )
    assert "access-control-allow-origin" not in custom.get("/healthz", headers=dev_origin).headers

    # 전체 허용(*)이면 쿠키(자격 증명)는 함께 허용하지 않는다
    wide = TestClient(create_app(_settings(cors_allow_origins="*"))).get("/healthz", headers=dev_origin)
    assert wide.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in wide.headers
