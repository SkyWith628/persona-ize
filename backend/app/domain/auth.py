"""사용자·세션 (project-design §6 users, §7 인증).

세션 쿠키는 HttpOnly·SameSite=Lax. 쓰기 요청은 CSRF 토큰(세션에 묶인 값)을 `X-CSRF-Token` 헤더로 함께 보낸다.
user_id는 users.id(UUID 문자열). 다른 모든 테이블의 user_id가 이 값을 가리킨다.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class User(BaseModel):
    id: UUID
    username: str = Field(min_length=3, max_length=32)
    created_at: datetime


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
