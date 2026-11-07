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
