"""예외를 로그에 남길 때 값(사용자 입력)이 섞이지 않게 한다.

project-design §9: 로그에는 요청 ID·단계·토큰 수·지연·오류 코드만 남긴다 — 대화 원문·초안은 남기지 않는다.
그런데 예외 메시지에는 값이 들어온다. DB 드라이버는 `invalid input for query argument $2: '<값>'`,
PostgreSQL은 `invalid input syntax for type integer: "<값>"`, SQLAlchemy는 `[parameters: (...)]`를 붙인다
(뒤의 것은 엔진의 `hide_parameters=True`로도 막는다 — `db/session.py`).

그래서 스택(파일·줄·함수·코드 줄)과 예외 종류는 그대로 남기고, **메시지는 믿을 수 있는 종류만** 남긴다.
- 남긴다(길이 제한): 코드·시스템 상태를 말하는 내장 예외 — TypeError·AttributeError·NameError·ImportError·
  RuntimeError·OSError(연결 거부·시간 초과)·IndexError 등. 표준 라이브러리·웹 프레임워크·이 앱이 정의한 것만.
- 버린다: ValueError·KeyError·AssertionError·UnicodeError처럼 값을 메시지에 넣는 종류와, 다른 패키지
  (sqlalchemy·asyncpg·pydantic·neo4j·anthropic…)의 예외 전부. 대신 찾는 데 쓸 식별 정보만 적는다:
  SQLSTATE, 제약·테이블·열 이름, HTTP 상태 코드, SQL 문장의 앞부분(매개변수 자리표시자만 있다).
"""

from __future__ import annotations

import re
import traceback
from typing import Any

MAX_MESSAGE_CHARS = 300
MAX_STATEMENT_CHARS = 160
MAX_CHAIN = 8
MAX_GROUP_MEMBERS = 10
OMITTED = "(메시지 생략 — 값이 섞일 수 있음)"

# 메시지가 코드·시스템 상태를 말하는 내장 예외. 값을 그대로 옮기는 ValueError·KeyError 계열은 넣지 않는다.
_MESSAGE_OK: tuple[type[BaseException], ...] = (
    TypeError,
    AttributeError,
    NameError,
    ImportError,
    NotImplementedError,
    RuntimeError,
    OSError,  # ConnectionError·TimeoutError·FileNotFoundError 포함
    EOFError,
    MemoryError,
    ArithmeticError,
    IndexError,
    StopAsyncIteration,
)
# 위 종류라도 이 패키지들이 정의(또는 내장)한 예외만 메시지를 믿는다.
_TRUSTED_ROOTS = frozenset(
    {"builtins", "asyncio", "concurrent", "socket", "ssl", "anyio", "starlette", "fastapi", "uvicorn", "app"}
)
# 값이 아니라 이름·코드인 속성 (asyncpg·SQLAlchemy·neo4j·httpx·anthropic 예외가 갖고 있다)
_SAFE_ATTRS = (
    "sqlstate",
    "pgcode",
    "constraint_name",
    "schema_name",
    "table_name",
    "column_name",
    "data_type_name",
    "status_code",
    "code",
    "errno",
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:\-]{1,80}")
_SPACES = re.compile(r"\s+")
_CAUSE = "The above exception was the direct cause of the following exception:"
_CONTEXT = "During handling of the above exception, another exception occurred:"


def type_name(exc: BaseException) -> str:
    cls = type(exc)
    module = cls.__module__ or ""
    return cls.__qualname__ if module in ("builtins", "") else f"{module}.{cls.__qualname__}"


def _clean(text: str, limit: int) -> str:
    """한 줄로, 제어 문자 없이, 길이 제한."""
    flat = _SPACES.sub(" ", "".join(ch if ch.isprintable() or ch.isspace() else "?" for ch in text)).strip()
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _message_is_trusted(exc: BaseException) -> bool:
    root = (type(exc).__module__ or "").split(".", 1)[0]
    return isinstance(exc, _MESSAGE_OK) and root in _TRUSTED_ROOTS


def _identifiers(exc: BaseException) -> list[str]:
    found: list[str] = []
    for name in _SAFE_ATTRS:
        value: Any = getattr(exc, name, None)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int) or (isinstance(value, str) and _IDENTIFIER.fullmatch(value)):
            found.append(f"{name}={value}")
    statement = getattr(exc, "statement", None)  # SQLAlchemy: 실행하던 SQL (값은 자리표시자)
    if isinstance(statement, str) and statement.strip():
        found.append(f'sql="{_clean(statement, MAX_STATEMENT_CHARS)}"')
    return found


def safe_exception_message(exc: BaseException) -> str:
    """`종류: 설명` 한 줄. 값이 섞일 수 있는 예외는 메시지 대신 식별 정보만 적는다."""
    name = type_name(exc)
    if _message_is_trusted(exc):
        try:
            text = _clean(str(exc), MAX_MESSAGE_CHARS)
        except Exception:  # noqa: BLE001 - __str__이 깨진 예외
            text = ""
        return f"{name}: {text}" if text else name
    details = " ".join(_identifiers(exc))
    return f"{name}: {OMITTED}" + (f" {details}" if details else "")


def _chain(exc: BaseException) -> list[tuple[BaseException, str]]:
    """바깥 예외부터 원인 쪽으로. 각 항목의 두 번째 값은 '그 예외와 그 원인을 잇는 문장'."""
    out: list[tuple[BaseException, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(out) < MAX_CHAIN:
        seen.add(id(current))
        if current.__cause__ is not None:
            nxt, link = current.__cause__, _CAUSE
        elif current.__context__ is not None and not current.__suppress_context__:
            nxt, link = current.__context__, _CONTEXT
        else:
            nxt, link = None, ""
        out.append((current, link))
        current = nxt
    return out


def format_exception_safely(exc: BaseException) -> str:
    """`traceback.format_exception`과 같은 모양이되 예외 메시지에서 값을 뺀 여러 줄 문자열."""
    lines: list[str] = []
    chain = _chain(exc)
    for index in range(len(chain) - 1, -1, -1):  # 파이썬처럼 원인을 먼저 찍는다
        current, _ = chain[index]
        if current.__traceback__ is not None:
            lines.append("Traceback (most recent call last):\n")
            lines.extend(traceback.format_tb(current.__traceback__))
        lines.append(safe_exception_message(current) + "\n")
        if isinstance(current, BaseExceptionGroup):
            members = current.exceptions[:MAX_GROUP_MEMBERS]
            lines.extend(f"  + {safe_exception_message(member)}\n" for member in members)
            if len(current.exceptions) > len(members):
                lines.append(f"  + … 외 {len(current.exceptions) - len(members)}건\n")
        if index > 0:
            lines.append(f"\n{chain[index - 1][1]}\n\n")
    return "".join(lines).rstrip("\n")
