"""사용자·세션 (project-design §6 users, §7 인증).

세션 쿠키는 HttpOnly·SameSite=Lax. 쓰기 요청은 CSRF 토큰(세션에 묶인 값)을 `X-CSRF-Token` 헤더로 함께 보낸다.
비브라우저 클라이언트(모바일 키보드·PC 프로그램)는 같은 세션 토큰을 `Authorization: Bearer`로 보낸다 —
쿠키처럼 브라우저가 알아서 붙이는 값이 아니므로 CSRF 검사를 하지 않는다.
user_id는 users.id(UUID 문자열). 다른 모든 테이블의 user_id가 이 값을 가리킨다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

SESSION_ID_CHARS = 12  # 세션 목록에 보여 주는 id = 토큰 해시 앞 12자 (토큰을 되살릴 수 없다)

Role = Literal["user", "admin"]
UserStatus = Literal["active", "pending", "suspended"]
ProviderName = Literal["google", "kakao"]

MAX_EMAIL = 254
MAX_DISPLAY_NAME = 40


class User(BaseModel):
    id: UUID
    username: str = Field(min_length=3, max_length=32)
    created_at: datetime
    # 약관·개인정보 안내 동의 (0008). 동의한 적 없으면 둘 다 None.
    consented_at: datetime | None = None
    consent_version: str | None = None
    # 회원 상태·소셜 로그인 (0012). email은 소문자·유일. 소셜 전용 계정은 has_password가 False.
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


class Session(BaseModel):
    """DB에는 토큰의 해시만 둔다. 원문 토큰은 로그인 응답의 Set-Cookie(또는 토큰 발급 응답)에만 존재한다."""

    token_hash: str
    user_id: UUID
    csrf_token: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now

    @property
    def public_id(self) -> str:
        return self.token_hash[:SESSION_ID_CHARS]


class AuthResult(BaseModel):
    """로그인·회원가입 결과. 토큰 원문은 쿠키(또는 토큰 발급 응답)로만 나가고 직렬화에서는 빠진다."""

    user: User
    session_token: str = Field(exclude=True)
    csrf_token: str
    expires_at: datetime
