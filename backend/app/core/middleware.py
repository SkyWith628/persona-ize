"""플랫폼 미들웨어 (계약 §4.1). 전부 순수 ASGI다.

바깥 → 안 순서 (`build_middleware`):

    RequestContext(요청 id·접근 로그) → SecurityHeaders(보안·캐시 헤더) → CORS(설정이 있을 때)
    → ErrorBoundary(처리 안 된 예외 → 500 통일) → HostCheck → RateLimit → BodyLimit → 라우터

- `BaseHTTPMiddleware`를 쓰지 않는다. 그것은 응답을 가로채 다시 흘려보내는 구조라 백그라운드 작업
  (가져오기 뒤 그래프 수집)·contextvars와 어긋난다. 여기 클래스들은 `send`/`receive`만 얇게 감싼다.
- ErrorBoundary를 CORS 안쪽에 두어 500 응답에도 요청 id·보안 헤더·CORS 헤더가 똑같이 붙는다.
- `/healthz`는 Host 검사를 받지 않고, 쿼리가 없으면 요청 제한도 받지 않는다 (컨테이너·프록시 헬스체크가
  내부 주소로 부른다). DB·Neo4j에 붙는 `/healthz?deep=true`는 요청 제한에 들어간다.
- 접근 로그에는 메서드·경로·상태·ms·요청 id만 남긴다. 쿼리 문자열·본문·쿠키·Authorization은 남기지 않는다.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterable, Sequence

from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.http_errors import (
    INVALID_HOST,
    RATE_LIMITED,
    PayloadTooLargeError,
    internal_error_response,
    json_error,
    payload_too_large_response,
)
from app.core.logs import ACCESS_LOGGER, mark_reported, request_id_var
from app.core.ratelimit import RateLimiter
from app.core.safe_exc import format_exception_safely

log = logging.getLogger(__name__)
access_log = logging.getLogger(ACCESS_LOGGER)

REQUEST_ID_HEADER = "X-Request-ID"
HEALTH_PATH = "/healthz"
EXEMPT_PATHS: frozenset[str] = frozenset({HEALTH_PATH})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"
# includeSubDomains는 넣지 않는다 — 이 서버가 알지 못하는 다른 하위 도메인까지 https로 묶는다.
HSTS = "max-age=31536000"
# Swagger·ReDoc 화면은 CDN 스크립트와 인라인 스크립트를 쓴다. 개발 모드에서만 열리므로 그때만 CSP를 뺀다.
DOCS_PATHS: frozenset[str] = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})

PAGE_PATHS: frozenset[str] = frozenset({"/", "/admin", "/privacy", "/terms", "/favicon.ico"})
NO_STORE = "no-store"
NO_CACHE = "no-cache"  # 파일 이름에 해시가 없으므로 매번 재검증(ETag/Last-Modified → 304)

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,64}")
_HOST_RE = re.compile(r"[a-z0-9._-]+")
_MAX_LOGGED_PATH = 200


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _printable(text: str, limit: int) -> str:
    """로그에 넣을 문자열: 줄바꿈·제어 문자를 지워 로그 줄을 위조하지 못하게 하고 길이를 자른다."""
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text[: limit + 1])
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def cache_control_for(path: str) -> str | None:
    """경로별 Cache-Control. API·헬스체크는 저장 금지, 화면·정적 파일은 매번 재검증."""
    if path.startswith("/v1/") or path == HEALTH_PATH:
        return NO_STORE
    if path.startswith("/static/") or path in PAGE_PATHS:
        return NO_CACHE
    return None


class RequestContextMiddleware:
    """요청 id를 정하고(받은 `X-Request-ID`가 멀쩡하면 그것) 응답 헤더·로그에 싣는다. 접근 로그 한 줄."""

    def __init__(self, app: ASGIApp, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self.app = app
        self.clock = clock
        self._forwarded_checked = False

    def _warn_if_forwarded_ignored(self, scope: Scope) -> None:
        """프록시가 보낸 `X-Forwarded-For`를 서버(uvicorn)가 적용하지 않고 있으면 한 번 알린다.

        이 앱은 그 헤더를 믿지 않고 uvicorn이 채운 연결 주소만 쓴다. 프록시 뒤인데 적용되지 않으면 모든
        사용자가 프록시 주소 하나로 보여 요청 제한·로그인 실패 제한이 전체에 한꺼번에 걸린다.
        """
        forwarded = _header(scope, b"x-forwarded-for")
        client = scope.get("client")
        if not forwarded or not client:
            return
        if client[0] in {part.strip() for part in forwarded.split(",")}:
            self._forwarded_checked = True  # 적용되고 있다 — 더 볼 필요 없다
            return
        self._forwarded_checked = True
        log.warning(
            "X-Forwarded-For 헤더가 왔지만 적용되지 않았습니다 (연결 주소 %s). 프록시 뒤라면 uvicorn을 "
            "`--proxy-headers --forwarded-allow-ips=<프록시 주소>`로 띄우세요 — 지금은 요청 제한과 "
            "로그인 실패 제한이 프록시 주소 하나에 묶입니다. "
            "프록시 없이 직접 받는 서버라면 꾸며 낸 헤더이니 무시해도 됩니다.",
            _printable(str(client[0]), 64),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self._forwarded_checked:
            self._warn_if_forwarded_ignored(scope)
        incoming = _header(scope, b"x-request-id")
        request_id = incoming if incoming and _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id  # 라우트에서 request.state.request_id
        token = request_id_var.set(request_id)
        started = self.clock()
        status = 0

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                message.setdefault("headers", [])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            status = status or 500  # 응답 전에 터졌으면 서버가 500을 보낸다
            raise
        finally:
            elapsed_ms = int((self.clock() - started) * 1000)
            path = scope.get("path", "")
            quiet = path == HEALTH_PATH and 0 < status < 400  # 헬스체크는 몇 초마다 온다
            access_log.log(
                logging.DEBUG if quiet else logging.INFO,
                "%s %s %s %dms",
                _printable(str(scope.get("method", "-")), 16),
                _printable(path, _MAX_LOGGED_PATH),
                status or "-",  # 0 = 응답을 시작하기 전에 연결이 끊겼다
                elapsed_ms,
                extra={"request_id": request_id},  # 포맷이 달라도(JSON 로그 등) 레코드에 id가 남는다
            )
            request_id_var.reset(token)


class SecurityHeadersMiddleware:
    """모든 응답에 보안 헤더와 경로별 Cache-Control을 붙인다. 라우트가 직접 정한 값은 덮어쓰지 않는다."""

    def __init__(self, app: ASGIApp, *, hsts: bool, csp_exempt_paths: Iterable[str] = ()) -> None:
        self.app = app
        self.csp_exempt_paths = frozenset(csp_exempt_paths)
        self.base_headers: list[tuple[str, str]] = [
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "same-origin"),
            ("X-Frame-Options", "DENY"),
            ("Permissions-Policy", PERMISSIONS_POLICY),
        ]
        if hsts:
            self.base_headers.append(("Strict-Transport-Security", HSTS))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in self.base_headers:
                    headers.setdefault(name, value)
                if path not in self.csp_exempt_paths:
                    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
                cache_control = cache_control_for(path)
                if cache_control is not None:
                    headers.setdefault("Cache-Control", cache_control)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ErrorBoundaryMiddleware:
    """처리되지 않은 예외 → 500 `internal_error` + `request_id`. 스택은 서버 로그에만 남긴다.

    응답을 보낸 뒤 예외를 다시 던진다(Starlette 관례): 테스트 클라이언트는 기본값에서 그 예외를 그대로
    올려 버그를 숨기지 않고, 서버는 연결을 정리한다. 같은 스택을 서버가 한 번 더 찍지 않도록 표시해 둔다.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            request_id = request_id_var.get()
            # 스택은 값(사용자 입력)을 뺀 모양으로 메시지에 넣는다. exc_info로 넘기면 로그 포매터가 예외
            # 메시지를 그대로 찍는다 — DB 드라이버는 거기에 SQL 매개변수(대화 원문·초안)를 넣는다.
            log.error(
                "처리되지 않은 예외 %s: %s %s\n%s",
                type(exc).__name__,
                _printable(str(scope.get("method", "-")), 16),
                _printable(scope.get("path", ""), _MAX_LOGGED_PATH),
                format_exception_safely(exc),
                extra={"request_id": request_id},
            )
            mark_reported(exc)
            if not response_started:
                await internal_error_response(request_id)(scope, receive, send)
            raise


def _is_digits(text: str) -> bool:
    return text.isascii() and text.isdigit()


def _is_ipv6_literal(text: str) -> bool:
    if not (text.startswith("[") and text.endswith("]")):
        return False
    try:
        ipaddress.IPv6Address(text[1:-1])
    except ValueError:
        return False
    return True


def normalize_host_patterns(patterns: Sequence[str]) -> list[str]:
    """`TRUSTED_HOSTS` 항목 검사. `example.com`, `*.example.com`, `[::1]`, `*`(전부 허용)만 받는다."""
    out: list[str] = []
    for raw in patterns:
        pattern = raw.strip().lower()
        if not pattern:
            continue
        wildcard = pattern.startswith("*.")
        name = pattern[2:] if wildcard else pattern
        valid = pattern == "*" or bool(_HOST_RE.fullmatch(name)) or (not wildcard and _is_ipv6_literal(name))
        if not valid:
            raise ValueError(
                f"TRUSTED_HOSTS 항목 {raw!r}을(를) 읽을 수 없습니다. "
                "'example.com' 또는 '*.example.com' 꼴로 적으세요 (포트·경로 없이)."
            )
        out.append(pattern)
    return out


def host_without_port(value: str | None) -> str | None:
    """Host 헤더에서 포트를 뗀 이름(소문자). 모양이 이상하면 None. IPv6는 대괄호째 돌려준다."""
    if not value:
        return None
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        literal, rest = value[: end + 1], value[end + 1 :]
        if rest and not (rest.startswith(":") and _is_digits(rest[1:])):
            return None
        return literal if _is_ipv6_literal(literal) else None
    host, sep, port = value.rpartition(":")
    if sep:
        if not _is_digits(port):
            return None
    else:
        host = value
    return host if host and _HOST_RE.fullmatch(host) else None


class HostCheckMiddleware:
    """Host 헤더가 허용 목록에 없으면 400 `invalid_host` (Host 위조로 링크·캐시를 오염시키는 것을 막는다)."""

    def __init__(
        self, app: ASGIApp, *, allowed_hosts: Sequence[str], exempt_paths: Iterable[str] = EXEMPT_PATHS
    ) -> None:
        self.app = app
        self.patterns = normalize_host_patterns(allowed_hosts)
        self.allow_any = "*" in self.patterns or not self.patterns
        self.exempt_paths = frozenset(exempt_paths)

    def allowed(self, host: str | None) -> bool:
        if self.allow_any:
            return True
        if host is None:
            return False
        return any(
            host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:]))
            for pattern in self.patterns
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or self.allow_any
            or scope.get("path") in self.exempt_paths
            or self.allowed(host_without_port(_header(scope, b"host")))
        ):
            await self.app(scope, receive, send)
            return
        await json_error(400, INVALID_HOST, "허용되지 않은 주소(Host)로 온 요청입니다.")(scope, receive, send)


class RateLimitMiddleware:
    """IP 기준 요청 제한 → 429 `rate_limited` + `Retry-After`. 규칙은 `core/ratelimit.py`."""

    def __init__(self, app: ASGIApp, *, limiter: RateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            client = scope.get("client")
            retry_after = self.limiter.check(
                scope.get("method", "GET"),
                scope.get("path", ""),
                client[0] if client else None,
                scope.get("query_string", b""),
            )
            if retry_after is not None:
                response = json_error(
                    429,
                    RATE_LIMITED,
                    f"요청이 너무 많습니다. {retry_after}초 뒤에 다시 시도하세요.",
                    headers={"Retry-After": str(retry_after)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    """요청 본문 상한 → 413 `payload_too_large`.

    `Content-Length`가 상한을 넘으면 본문을 읽기 전에 거절한다. 길이를 밝히지 않은 요청(chunked)은 실제로
    받은 양을 세다가 넘는 순간 끊는다 — 그때는 본문을 읽던 쪽(FastAPI 본문 파서)에서 예외가 올라온다.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = (_header(scope, b"content-length") or "").strip()
        if _is_digits(declared) and int(declared) > self.max_bytes:  # 숫자만으로 된 값일 때만 믿는다
            await payload_too_large_response(self.max_bytes)(scope, receive, send)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise PayloadTooLargeError(self.max_bytes)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except PayloadTooLargeError:
            # 보통은 안쪽의 예외 처리기가 413으로 바꾼다. 여기까지 올라왔다면(처리기 없는 앱) 직접 답한다.
            if response_started:
                raise
            await payload_too_large_response(self.max_bytes)(scope, receive, send)


def build_middleware(settings: Settings, limiter: RateLimiter) -> list[Middleware]:
    """설정에 맞는 미들웨어 목록 (앞이 바깥). 기본 설정이면 Host 검사·요청 제한은 들어가지 않는다."""
    stack: list[Middleware] = [
        Middleware(RequestContextMiddleware),
        Middleware(
            SecurityHeadersMiddleware,
            hsts=settings.session_cookie_secure,
            csp_exempt_paths=() if settings.is_production else DOCS_PATHS,
        ),
    ]
    origins = settings.cors_origin_list
    if origins:
        allow_any = "*" in origins
        stack.append(
            Middleware(
                CORSMiddleware,
                allow_origins=["*"] if allow_any else origins,
                # 모든 출처 허용이면 쿠키(자격 증명)는 절대 함께 허용하지 않는다.
                allow_credentials=not allow_any,
                allow_methods=["*"],
                allow_headers=["*"],
                expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
            )
        )
    stack.append(Middleware(ErrorBoundaryMiddleware))
    # 미들웨어 객체는 첫 요청 때 만들어진다 — 잘못 적은 항목이 시작할 때 드러나도록 여기서 먼저 읽는다.
    hosts = normalize_host_patterns(settings.trusted_host_list)
    if hosts and "*" not in hosts:
        stack.append(Middleware(HostCheckMiddleware, allowed_hosts=hosts))
    if limiter.enabled:
        stack.append(Middleware(RateLimitMiddleware, limiter=limiter))
    stack.append(Middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes))
    return stack
