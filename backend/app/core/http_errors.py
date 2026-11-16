"""오류 응답 모양 통일 (계약 §4): 최상위 `{"code": "...", "message": "사람이 읽는 한국어"}`.

- 도메인 예외(`DomainError`)는 원래 이 모양이다.
- `HTTPException`(라우트가 직접 올린 것, 없는 주소 404, 메서드 불일치 405 등)은 예전에
  `{"detail": ...}`로 나갔다. 최상위 `code`·`message`를 더하고 **`detail`은 그대로 남긴다**(하위 호환:
  옛 화면과 `proto.js`가 `detail`을 읽는다).
- 요청 검증 실패(422)는 `{"code":"invalid_input","message":"<첫 문제 요약>","details":[{"loc","msg"}],
  "detail":[…FastAPI 목록…]}`. `detail` 항목의 `input`(보낸 값 되돌려 주기)은 짧은 단일 값일 때만 남긴다 —
  FastAPI 기본값은 필드가 빠지면 본문 전체(비밀번호 포함)를 `input`으로 되돌려 준다.
- 처리되지 않은 예외는 `ErrorBoundaryMiddleware`가 500 `internal_error` + `request_id`로 바꾼다.
- 429는 모두 `Retry-After`(초)를 싣는다: 요청 제한·로그인 잠금은 창이 풀릴 때까지, 하루 한도
  (`usage_exceeded`·`budget_exceeded`)는 사용량 날짜가 바뀌는 자정까지.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import DomainError

INTERNAL_ERROR_MESSAGE = "서버에서 문제가 생겼습니다. 잠시 뒤에 다시 시도하세요."
PAYLOAD_TOO_LARGE = "payload_too_large"
RATE_LIMITED = "rate_limited"
INVALID_HOST = "invalid_host"
# 하루 단위 한도의 오류 코드 (429). 응답에 `Retry-After`(사용량 날짜가 바뀔 때까지 남은 초)를 붙인다.
DAILY_LIMIT_CODES = frozenset({"usage_exceeded", "budget_exceeded"})

# 상태 코드 → (code, 기본 메시지). HTTPException의 detail이 영어 기본 문구이거나 코드가 없을 때 쓴다.
_STATUS_DEFAULTS: dict[int, tuple[str, str]] = {
    400: ("bad_request", "요청을 이해하지 못했습니다."),
    401: ("unauthorized", "로그인이 필요합니다."),
    403: ("forbidden", "허용되지 않은 요청입니다."),
    404: ("not_found", "요청한 주소를 찾을 수 없습니다."),
    405: ("method_not_allowed", "이 주소에서 쓸 수 없는 요청 방식입니다."),
    406: ("not_acceptable", "요청한 응답 형식을 줄 수 없습니다."),
    408: ("request_timeout", "요청 시간이 초과되었습니다."),
    409: ("conflict", "지금 상태와 맞지 않는 요청입니다."),
    413: (PAYLOAD_TOO_LARGE, "요청 본문이 너무 큽니다."),
    415: ("unsupported_media_type", "지원하지 않는 본문 형식입니다."),
    422: ("invalid_input", "입력 값을 확인하세요."),
    429: (RATE_LIMITED, "요청이 너무 많습니다. 잠시 뒤에 다시 시도하세요."),
    500: ("internal_error", INTERNAL_ERROR_MESSAGE),
    501: ("not_implemented", "아직 지원하지 않는 기능입니다."),
    502: ("bad_gateway", "연결된 서비스가 응답하지 않습니다. 잠시 뒤에 다시 시도하세요."),
    503: ("service_unavailable", "지금은 처리할 수 없습니다. 잠시 뒤에 다시 시도하세요."),
    504: ("gateway_timeout", "연결된 서비스의 응답이 늦습니다. 잠시 뒤에 다시 시도하세요."),
}

# FastAPI가 영어 문구로 올리는 HTTPException 가운데 상태 코드만으로는 원인을 알기 어려운 것
_KNOWN_DETAILS: dict[str, str] = {
    "There was an error parsing the body": "요청 본문을 읽지 못했습니다. UTF-8 JSON인지 확인하세요.",
}

# pydantic 오류 종류 → 한국어 문구. {이름}은 오류의 ctx 값으로 채운다.
_VALIDATION_KO: dict[str, str] = {
    "missing": "필수 항목입니다",
    "extra_forbidden": "알 수 없는 항목입니다",
    "json_invalid": "JSON 형식이 올바르지 않습니다",
    "json_type": "JSON 형식이 올바르지 않습니다",
    "string_type": "문자열이어야 합니다",
    "string_too_short": "{min_length}자 이상이어야 합니다",
    "string_too_long": "{max_length}자 이하여야 합니다",
    "string_pattern_mismatch": "형식이 맞지 않습니다",
    "int_type": "정수여야 합니다",
    "int_parsing": "정수여야 합니다",
    "int_from_float": "정수여야 합니다",
    "float_type": "숫자여야 합니다",
    "float_parsing": "숫자여야 합니다",
    "bool_type": "true 또는 false여야 합니다",
    "bool_parsing": "true 또는 false여야 합니다",
    "greater_than": "{gt}보다 커야 합니다",
    "greater_than_equal": "{ge} 이상이어야 합니다",
    "less_than": "{lt}보다 작아야 합니다",
    "less_than_equal": "{le} 이하여야 합니다",
    "literal_error": "허용되는 값이 아닙니다 (가능한 값: {expected})",
    "enum": "허용되는 값이 아닙니다 (가능한 값: {expected})",
    "uuid_type": "올바른 id 형식이 아닙니다",
    "uuid_parsing": "올바른 id 형식이 아닙니다",
    "datetime_type": "날짜·시각 형식이 올바르지 않습니다 (예: 2026-11-11T09:00:00+09:00)",
    "datetime_parsing": "날짜·시각 형식이 올바르지 않습니다 (예: 2026-11-11T09:00:00+09:00)",
    "datetime_from_date_parsing": "날짜·시각 형식이 올바르지 않습니다 (예: 2026-11-11T09:00:00+09:00)",
    "datetime_object_invalid": "날짜·시각 형식이 올바르지 않습니다 (예: 2026-11-11T09:00:00+09:00)",
    "date_type": "날짜 형식이 올바르지 않습니다 (예: 2026-11-11)",
    "date_parsing": "날짜 형식이 올바르지 않습니다 (예: 2026-11-11)",
    "date_from_datetime_parsing": "날짜 형식이 올바르지 않습니다 (예: 2026-11-11)",
    "date_from_datetime_inexact": "날짜 형식이 올바르지 않습니다 (예: 2026-11-11)",
    "list_type": "목록(배열)이어야 합니다",
    "dict_type": "객체여야 합니다",
    "model_type": "객체여야 합니다",
    "model_attributes_type": "객체여야 합니다",
    "too_short": "{min_length}개 이상이어야 합니다",
    "too_long": "{max_length}개 이하여야 합니다",
}
# 검증기가 직접 올린 메시지(대개 한국어) 앞에 pydantic이 붙이는 말 — 떼고 그 메시지를 쓴다.
_RAISED_PREFIXES = ("Value error, ", "Assertion failed, ")
_LOCATIONS = {"body": "본문", "query": "쿼리", "path": "경로", "header": "헤더", "cookie": "쿠키"}

# 값을 되돌려 주면 안 되는 필드 이름(부분 일치)과 되돌려 줄 수 있는 최대 길이
_SENSITIVE_PARTS = ("password", "token", "secret", "invite", "authorization", "cookie")
_MAX_ECHO_CHARS = 100


class PayloadTooLargeError(StarletteHTTPException):
    """본문을 받는 도중 상한을 넘었다 (`BodyLimitMiddleware`가 감싼 receive가 올린다).

    HTTPException의 하위 클래스여야 한다 — FastAPI 본문 파서는 그 밖의 예외를 "본문 파싱 오류 400"으로 바꾼다.
    """

    def __init__(self, max_bytes: int) -> None:
        super().__init__(status_code=413, detail=_STATUS_DEFAULTS[413][1])
        self.max_bytes = max_bytes


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def json_error(
    status_code: int, code: str, message: str, *, headers: Mapping[str, str] | None = None, **extra: Any
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_body(code, message, **extra),
        headers=dict(headers) if headers else None,
    )


def internal_error_response(request_id: str) -> JSONResponse:
    """처리되지 않은 예외의 응답. 원인은 서버 로그에만 남기고 본문에는 찾을 때 쓸 요청 id만 준다."""
    return json_error(500, "internal_error", INTERNAL_ERROR_MESSAGE, request_id=request_id)


def payload_too_large_response(max_bytes: int) -> JSONResponse:
    # Connection: close — 서버가 남은 본문을 끝까지 받지 않고 연결을 닫게 한다.
    return json_error(
        413,
        PAYLOAD_TOO_LARGE,
        f"요청 본문이 너무 큽니다 (최대 {_human_bytes(max_bytes)}).",
        headers={"Connection": "close"},
    )


def _human_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "KB"
    return f"{n}바이트"


def _has_body(status_code: int) -> bool:
    return not (status_code < 200 or status_code in (204, 205, 304))


def _is_korean(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def seconds_until_usage_day_ends(offset_hours: int, now: datetime | None = None) -> int:
    """사용량 날짜(기본 한국 시간)가 바뀌는 자정까지 남은 초 — 하루 한도가 다시 열리는 때. 1 이상."""
    zone = timezone(timedelta(hours=offset_hours))
    local = (now or datetime.now(UTC)).astimezone(zone)
    next_day = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(math.ceil((next_day - local).total_seconds()), 1)


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    headers = dict(getattr(exc, "headers", None) or {})
    if exc.code in DAILY_LIMIT_CODES and "Retry-After" not in headers:
        # 하루 한도(사용자·서비스 전체)는 사용량 날짜가 바뀌면 풀린다 — 다른 429처럼 언제 다시 되는지 알린다.
        settings = getattr(getattr(request.app.state, "container", None), "settings", None)
        if settings is not None:
            headers["Retry-After"] = str(seconds_until_usage_day_ends(settings.usage_day_offset_hours))
    return json_error(exc.status_code, exc.code, exc.message, headers=headers or None)


async def payload_too_large_handler(_: Request, exc: PayloadTooLargeError) -> JSONResponse:
    return payload_too_large_response(exc.max_bytes)


async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> Response:
    headers = getattr(exc, "headers", None)
    if not _has_body(exc.status_code):
        return Response(status_code=exc.status_code, headers=headers)
    default_code, default_message = _STATUS_DEFAULTS.get(
        exc.status_code, ("http_error", _phrase_ko(exc.status_code))
    )
    detail = exc.detail
    if isinstance(detail, Mapping) and isinstance(detail.get("code"), str):
        code = detail["code"]
        message = detail["message"] if isinstance(detail.get("message"), str) else default_message
    elif isinstance(detail, str) and _is_korean(detail):
        code, message = default_code, detail  # 라우트가 한국어 문장을 직접 적어 올린 경우
    else:  # "Not Found" 같은 영어 기본 문구
        code = default_code
        message = _KNOWN_DETAILS.get(detail, default_message) if isinstance(detail, str) else default_message
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, message, detail=jsonable_encoder(detail)),
        headers=headers,
    )


def _phrase_ko(status_code: int) -> str:
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        phrase = "오류"
    return f"요청을 처리하지 못했습니다 ({status_code} {phrase})."


def _loc_text(loc: Sequence[Any]) -> str:
    return ".".join(str(part) for part in loc)


def _field_label(loc: Sequence[Any], kind: str) -> str:
    """요약 문장에 쓸 이름: 위치 접두(body·query…)를 떼고, 남는 게 없으면 위치 이름."""
    parts = [str(p) for p in loc]
    if not parts:
        return "입력"
    if kind.startswith("json_"):  # ("body", <오류 위치>) — 숫자는 사람에게 뜻이 없다
        return _LOCATIONS["body"]
    if parts[0] in _LOCATIONS:
        return ".".join(parts[1:]) or _LOCATIONS[parts[0]]
    return ".".join(parts)


def validation_message(err: Mapping[str, Any]) -> str:
    """pydantic 오류 한 건 → 한국어 한 마디 (마침표 없음)."""
    kind = str(err.get("type", ""))
    template = _VALIDATION_KO.get(kind)
    if template is not None:
        ctx = dict(err["ctx"]) if isinstance(err.get("ctx"), Mapping) else {}
        if "expected" in ctx:  # "'A', 'B' or 'C'" → "'A', 'B', 'C'"
            ctx["expected"] = str(ctx["expected"]).replace(" or ", ", ")
        try:
            return template.format(**ctx)
        except (KeyError, IndexError, ValueError):
            pass
    msg = str(err.get("msg") or "")
    for prefix in _RAISED_PREFIXES:
        if msg.startswith(prefix):
            return msg[len(prefix) :].rstrip(".") or "값이 올바르지 않습니다"
    return msg.rstrip(".") or "값이 올바르지 않습니다"


def _is_sensitive(loc: Sequence[Any]) -> bool:
    return any(part in str(p).lower() for p in loc for part in _SENSITIVE_PARTS)


def _safe_detail_item(err: Mapping[str, Any]) -> dict[str, Any]:
    """FastAPI 원래 항목에서 `input`만 조심한다: 민감 필드·묶음 값·긴 값은 되돌려 주지 않는다."""
    item = {k: v for k, v in err.items() if k != "input"}
    if "input" in err:
        value = err["input"]
        scalar = value is None or isinstance(value, str | int | float | bool)
        if scalar and not _is_sensitive(err.get("loc", ())) and len(str(value)) <= _MAX_ECHO_CHARS:
            item["input"] = value
    return item


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = list(exc.errors())
    details = [
        {"loc": _loc_text(e.get("loc", ())), "msg": validation_message(e)}
        for e in errors
        if isinstance(e, Mapping)
    ]
    if errors and isinstance(errors[0], Mapping):
        first = errors[0]
        label = _field_label(first.get("loc", ()), str(first.get("type", "")))
        message = f"{label}: {validation_message(first)}."
        if len(errors) > 1:
            message += f" (외 {len(errors) - 1}건)"
    else:
        message = _STATUS_DEFAULTS[422][1]
    detail = jsonable_encoder([_safe_detail_item(e) if isinstance(e, Mapping) else e for e in errors])
    return JSONResponse(
        status_code=422,
        content=error_body("invalid_input", message, details=details, detail=detail),
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore[arg-type]
    # 더 구체적인 클래스의 처리기가 먼저 잡힌다 (예외 클래스의 MRO 순서로 찾는다).
    app.add_exception_handler(PayloadTooLargeError, payload_too_large_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
