"""운영 모드 검사 (계약 §4.1). `APP_ENV=production`이면 위험한 설정으로는 서버를 시작하지 않는다.

`create_app`이 저장소를 만들기 전에 부른다. 개발 모드(기본값)에서는 아무 일도 하지 않는다.
거부 사유는 한 번에 전부 보여 준다 — 하나 고치고 다시 띄우기를 반복하지 않게.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.core.config import Settings

log = logging.getLogger(__name__)

DEV_DB_PASSWORD = "persona-dev-only"  # .env.example·docker-compose.yml의 개발 기본값
DEV_NEO4J_PASSWORD = "neo4j-dev-only"


class UnsafeConfigError(RuntimeError):
    """운영 모드에서 받아들일 수 없는 설정. 메시지에 고칠 항목이 줄마다 적혀 있다."""


@dataclass(frozen=True)
class GuardReport:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _url_password(url: str) -> str:
    try:
        return unquote(urlsplit(url).password or "")
    except ValueError:
        return ""


def inspect_production_settings(settings: Settings) -> GuardReport:
    """운영 기준으로 설정을 살핀다 (모드와 무관하게 호출 가능 — 판단만 하고 아무것도 바꾸지 않는다)."""
    errors: list[str] = []
    warnings: list[str] = []

    if settings.auth_allow_header_stub:
        errors.append(
            "AUTH_ALLOW_HEADER_STUB=true — X-User-Id 헤더만으로 누구든 될 수 있습니다. false로 바꾸세요."
        )
    if not settings.session_cookie_secure:
        errors.append(
            "SESSION_COOKIE_SECURE=false — 세션 쿠키가 http로도 전송됩니다. https 뒤에 두고 true로 바꾸세요."
        )
    if not settings.database_url:
        errors.append("DATABASE_URL이 비어 있습니다 — 인메모리 저장소는 재시작하면 모든 데이터가 사라집니다.")
    elif _url_password(settings.database_url) == DEV_DB_PASSWORD or DEV_DB_PASSWORD in settings.database_url:
        errors.append(
            f"DATABASE_URL의 비밀번호가 개발 기본값({DEV_DB_PASSWORD})입니다. "
            "POSTGRES_PASSWORD를 새 값으로 바꾸세요."
        )
    if settings.neo4j_password == DEV_NEO4J_PASSWORD:
        errors.append(
            f"NEO4J_PASSWORD가 개발 기본값({DEV_NEO4J_PASSWORD})입니다. 새 값으로 바꾸세요 "
            "(그래프를 쓰지 않으면 NEO4J_URI와 함께 비워 두세요)."
        )

    if settings.llm_provider == "fake":
        warnings.append("LLM_PROVIDER=fake — 첨삭이 원문을 그대로 돌려줍니다 (실제 모델 호출 없음).")
    elif not settings.anthropic_api_key:
        warnings.append(
            "LLM_PROVIDER=anthropic인데 ANTHROPIC_API_KEY가 비어 있습니다 — 모델 호출이 실패하면 "
            "첨삭이 원문 유지로만 끝납니다."
        )
    if not settings.admin_username_set:
        warnings.append("ADMIN_USERNAMES가 비어 있습니다 — 관리자 화면(/admin)에 아무도 들어갈 수 없습니다.")
    if not settings.trusted_host_list:
        warnings.append(
            "TRUSTED_HOSTS가 비어 있습니다 — Host 헤더를 검사하지 않습니다. 서비스 도메인을 적으세요."
        )
    if settings.auth_allow_register and not settings.register_invite_code:
        warnings.append(
            "가입이 열려 있는데 REGISTER_INVITE_CODE가 비어 있습니다 — 주소를 아는 누구나 가입할 수 있습니다."
        )
    if settings.rate_limit_per_minute == 0 or settings.rate_limit_auth_per_minute == 0:
        warnings.append(
            "RATE_LIMIT_PER_MINUTE 또는 RATE_LIMIT_AUTH_PER_MINUTE가 0입니다 — 그 요청 제한은 꺼져 있습니다."
        )
    origins = settings.cors_origin_list
    if "*" in origins:
        warnings.append(
            "CORS_ALLOW_ORIGINS에 *가 있습니다 — 모든 사이트가 API를 부를 수 있습니다(쿠키는 제외)."
        )
    elif any("localhost" in origin or "127.0.0.1" in origin for origin in origins):
        warnings.append("CORS_ALLOW_ORIGINS에 localhost가 남아 있습니다 — 같은 출처 화면만 쓰면 비워 두세요.")
    return GuardReport(errors=tuple(errors), warnings=tuple(warnings))


def enforce_production_guard(settings: Settings) -> GuardReport:
    """운영 모드면 검사한다. 거부 사유가 있으면 로그에 남기고 `UnsafeConfigError`. 경고는 로그만."""
    if not settings.is_production:
        return GuardReport()
    report = inspect_production_settings(settings)
    for warning in report.warnings:
        log.warning("운영 설정 경고: %s", warning)
    if report.errors:
        message = "APP_ENV=production인데 안전하지 않은 설정이 있어 서버를 시작하지 않습니다:\n" + "\n".join(
            f"  - {error}" for error in report.errors
        )
        log.critical(message)
        raise UnsafeConfigError(message)
    log.info("운영 설정 검사 통과 (경고 %d건)", len(report.warnings))
    return report
