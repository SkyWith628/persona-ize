"""도메인 예외. API 계층에서 HTTP 상태로 번역한다 (project-design §7 오류 표)."""

from __future__ import annotations


class DomainError(Exception):
    """도메인 규칙 위반의 공통 부모."""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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


class ModelUnavailableError(DomainError):
    """모델 호출 실패·타임아웃. 워크플로는 원문 유지로 축소 동작한다."""

    status_code = 503
    code = "model_unavailable"


class ModelRefusalError(ModelUnavailableError):
    """모델(안전 분류기)이 생성을 거절했다. 장애가 아니므로 워크플로가 재생성 1회를 시도한다 (11-08 E003)."""

    code = "model_refusal"


class UsageLimitError(DomainError):
    """하루 사용량 한도 초과. 자정(KST) 이후 다시 가능."""

    status_code = 429
    code = "usage_exceeded"


class TooManyAttemptsError(DomainError):
    """로그인 실패가 잦아 잠시 막혔다."""

    status_code = 429
    code = "too_many_attempts"


class BadRequestError(DomainError):
    """요청 자체가 규칙에 어긋난다 (자기 자신 정지·삭제 등). 422(본문 검증)와 달리 400으로 나간다."""

    status_code = 400
    code = "invalid_input"


class SignupClosedError(DomainError):
    """가입이 닫혀 있다 (`AUTH_ALLOW_REGISTER=false`). 기존 계정 로그인은 된다."""

    status_code = 403
    code = "signup_closed"


class AccountPendingError(DomainError):
    """승인 대기 계정. 로그인은 되지만 `/v1/auth/*` 밖의 API는 쓸 수 없다."""

    status_code = 403
    code = "account_pending"


class AccountSuspendedError(DomainError):
    """정지된 계정. 로그인할 수 없다."""

    status_code = 403
    code = "account_suspended"
