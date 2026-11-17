"""도메인 예외. API 계층에서 HTTP 상태로 번역한다 (project-design §7 오류 표).

응답 모양은 `core/http_errors.py`가 `{"code", "message"}`로 통일한다. `headers`가 있으면 응답 헤더에 싣는다
(예: 429의 `Retry-After`, Bearer 401의 `WWW-Authenticate`).
"""

from __future__ import annotations

from collections.abc import Mapping


class DomainError(Exception):
    """도메인 규칙 위반의 공통 부모."""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.headers: dict[str, str] | None = dict(headers) if headers else None


class NotFoundError(DomainError):
    """소유권이 다르거나 없는 자원. 존재 여부를 노출하지 않기 위해 둘을 구분하지 않는다."""

    status_code = 404
    code = "not_found"


class VersionConflictError(DomainError):
    """기대 버전 불일치 또는 같은 Idempotency-Key에 다른 본문."""

    status_code = 409
    code = "conflict"


class InvalidInputError(DomainError):
    """입력 값 규칙 위반 (빈 원문, 길이 초과 등)."""

    status_code = 422
    code = "invalid_input"


class ConsentRequiredError(InvalidInputError):
    """`REQUIRE_CONSENT`인데 가입 요청에 consent=true가 없다."""

    code = "consent_required"


class ModelUnavailableError(DomainError):
    """모델 호출 실패·타임아웃. 워크플로는 원문 유지로 축소 동작한다."""

    status_code = 503
    code = "model_unavailable"


class ModelRefusalError(ModelUnavailableError):
    """모델(안전 분류기)이 생성을 거절했다. 장애가 아니므로 워크플로가 재생성 1회를 시도한다 (11-08 E003)."""

    code = "model_refusal"


class GraphUnavailableError(DomainError):
    """그래프 저장소에 닿지 못해 요청을 끝내지 못했다. 바꾼 것이 없으니 나중에 다시 하면 된다."""

    status_code = 503
    code = "graph_unavailable"


class UsageLimitError(DomainError):
    """하루 사용량 한도 초과. 자정(KST) 이후 다시 가능."""

    status_code = 429
    code = "usage_exceeded"


class TooManyAttemptsError(DomainError):
    """로그인 실패가 잦아 잠시 막혔다. `retry_after`(초)를 주면 `Retry-After` 헤더로도 알린다."""

    status_code = 429
    code = "too_many_attempts"

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        headers = {"Retry-After": str(max(int(retry_after), 1))} if retry_after is not None else None
        super().__init__(message, headers=headers)


class UnauthorizedError(DomainError):
    """로그인이 없거나 세션·토큰이 만료·폐기됐다."""

    status_code = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    """로그인은 했지만 허용되지 않는다 (CSRF 불일치, 가입 닫힘, 관리자 아님)."""

    status_code = 403
    code = "forbidden"


class InvalidInviteError(ForbiddenError):
    """`REGISTER_INVITE_CODE`가 설정돼 있는데 가입 요청의 초대 코드가 없거나 다르다."""

    code = "invalid_invite"


class SignupClosedError(ForbiddenError):
    """`AUTH_ALLOW_REGISTER=false`. 응답 코드는 예전과 같은 `forbidden`이다 (소셜 콜백만 따로 구분해 쓴다)."""


class AccountPendingError(ForbiddenError):
    """승인 대기 계정 (`AUTH_SIGNUP_REQUIRES_APPROVAL`). 관리자가 승인하기 전에는 로그인할 수 없다."""

    code = "account_pending"


class AccountSuspendedError(ForbiddenError):
    """정지된 계정. 비밀번호(또는 소셜 로그인)가 맞은 뒤에만 이 응답이 나간다."""

    code = "account_suspended"


class EmailInUseError(VersionConflictError):
    """소셜 로그인의 이메일이 **확인되지 않은** 이메일로 가입한 기존 계정과 같다 — 자동으로 묶지 않는다."""

    code = "email_in_use"


class OAuthStateError(DomainError):
    """소셜 로그인 state가 없거나 맞지 않거나 이미 썼다."""

    status_code = 400
    code = "oauth_failed"
