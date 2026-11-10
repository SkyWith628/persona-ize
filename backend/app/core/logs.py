"""로깅 설정과 요청 id.

- 요청 id는 `RequestContextMiddleware`가 요청마다 정해 `request_id_var`에 넣는다. 로그 한 줄마다
  `[요청 id]`가 붙으므로 사용자가 알려 준 `request_id`(500 응답 본문, `X-Request-ID` 헤더)로 로그를 찾는다.
  요청 중에 만든 백그라운드 작업(`asyncio.create_task`, `asyncio.to_thread`)도 같은 id를 물려받는다.
- `LOG_LEVEL`이 INFO 이상이면 루트 수준으로 건다(전체). DEBUG는 앱 로거(`app.*`)에만 건다 — 루트를
  DEBUG로 내리면 모델 SDK·HTTP 클라이언트가 요청 본문(사용자 대화)을 로그에 쏟는다.
- uvicorn 기본 접근 로그는 쿼리 문자열과 클라이언트 주소를 그대로 찍는다. 접근 로그는 앱이 한 줄로
  남기므로(`app.access`: 메서드·경로·상태·ms) uvicorn 쪽은 끈다.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

NO_REQUEST = "-"
request_id_var: ContextVar[str] = ContextVar("pz_request_id", default=NO_REQUEST)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
APP_LOGGER = "app"
ACCESS_LOGGER = "app.access"
REPORTED_ATTR = "_pz_reported"

log = logging.getLogger(__name__)


def current_request_id() -> str:
    """지금 처리 중인 요청의 id. 요청 밖이면 "-"."""
    return request_id_var.get()


def mark_reported(exc: BaseException) -> None:
    """이 예외는 앱 로그에 스택까지 남겼다는 표시 (`_AlreadyReportedFilter`가 본다)."""
    try:
        setattr(exc, REPORTED_ATTR, True)
    except AttributeError:  # __slots__ 예외 등 — 표시를 못 하면 서버가 한 번 더 찍을 뿐이다
        pass


class RequestIdFilter(logging.Filter):
    """레코드에 `request_id`를 채운다 (포맷 문자열이 `%(request_id)s`를 쓴다)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class _AlreadyReportedFilter(logging.Filter):
    """앱이 스택과 함께 이미 남긴 예외를 서버(uvicorn)가 한 번 더 찍지 않게 한다.

    `ErrorBoundaryMiddleware`는 예외를 요청 id와 함께 기록한 뒤 다시 던진다(서버·테스트 클라이언트가 알 수
    있게). 그러면 uvicorn이 "Exception in ASGI application"으로 같은 스택을 또 남기므로 그 한 건만 거른다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not getattr(exc, REPORTED_ATTR, False)


def resolve_level(name: str) -> int | None:
    """수준 이름("info"·"WARNING" 등) → 로깅 수준 숫자. 모르는 이름이면 None."""
    value = logging.getLevelNamesMapping().get(name.strip().upper())
    return value if isinstance(value, int) and value > 0 else None


def configure_logging(level_name: str = "INFO") -> int:
    """루트 핸들러(없을 때만)와 수준을 맞춘다. 여러 번 불러도 결과가 같다. 적용한 수준을 돌려준다."""
    level = resolve_level(level_name)
    unknown = level is None
    if level is None:
        level = logging.INFO

    root = logging.getLogger()
    # uvicorn은 자기 로거만 설정한다. pytest·--log-config가 루트에 이미 달아 둔 핸들러는 건드리지 않는다.
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(RequestIdFilter())
        root.addHandler(handler)
    root.setLevel(max(level, logging.INFO))
    # INFO 이상이면 앱 로거는 루트를 따른다(따로 정하지 않는다). DEBUG일 때만 앱 로거를 루트보다 낮춘다.
    logging.getLogger(APP_LOGGER).setLevel(level if level < logging.INFO else logging.NOTSET)

    logging.getLogger("uvicorn.access").disabled = True
    server_log = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _AlreadyReportedFilter) for f in server_log.filters):
        server_log.addFilter(_AlreadyReportedFilter())

    if unknown:
        log.warning("LOG_LEVEL=%r 는 모르는 값이라 INFO로 둡니다 (DEBUG·INFO·WARNING·ERROR).", level_name)
    return level
