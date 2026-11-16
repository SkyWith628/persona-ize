"""사용자·세션 (project-design §6 users, §7 인증).

세션 쿠키는 HttpOnly·SameSite=Lax. 쓰기 요청은 CSRF 토큰(세션에 묶인 값)을 `X-CSRF-Token` 헤더로 함께 보낸다.
user_id는 users.id(UUID 문자열). 다른 모든 테이블의 user_id가 이 값을 가리킨다.

회원관리(0008): 이메일(소문자, 유일)·표시 이름·역할·상태·마지막 로그인. 소셜 계정은 `Identity`
(provider + subject)로 사용자에 묶인다. 비밀번호가 없는 계정(소셜 전용)도 있다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Role = Literal["user", "admin"]
UserStatus = Literal["active", "pending", "suspended"]
ProviderName = Literal["google", "kakao"]

MAX_EMAIL = 254
MAX_DISPLAY_NAME = 40


class User(BaseModel):
    id: UUID
    username: str = Field(min_length=3, max_length=32)
    created_at: datetime
    email: str | None = None
    display_name: str | None = None
    role: Role = "user"
    status: UserStatus = "active"
    last_login_at: datetime | None = None
    has_password: bool = True


class Identity(BaseModel):
    """소셜 계정 연결. (provider, subject)가 유일하다. email은 제공자가 알려 준 값(참고용)."""

    provider: ProviderName
    subject: str
    user_id: UUID
    email: str | None = None
    created_at: datetime


class UserCounts(BaseModel):
    total: int
    pending: int
    suspended: int
    new_in_range: int


class AuditEntry(BaseModel):
    """관리자 작업 기록. 이름은 그때 값을 그대로 적는다 — 계정이 지워져도 기록은 남는다."""

    id: int = 0  # 저장소가 매긴다
    at: datetime
    admin_user_id: UUID | None = None
    admin_username: str
    action: str
    target_user_id: UUID | None = None
    target_username: str | None = None
    detail: str = ""


class Session(BaseModel):
    """DB에는 토큰의 해시만 둔다. 원문 토큰은 로그인 응답의 Set-Cookie에만 존재한다."""

    token_hash: str
    user_id: UUID
    csrf_token: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now


class AuthResult(BaseModel):
    """로그인·회원가입 결과. 토큰 원문은 쿠키로만 나가고 응답 본문에는 CSRF 토큰만 둔다."""

    user: User
    session_token: str = Field(exclude=True)
    csrf_token: str
    expires_at: datetime
