"""사용자·세션 (project-design §6 users, §7 인증).

세션 쿠키는 HttpOnly·SameSite=Lax. 쓰기 요청은 CSRF 토큰(세션에 묶인 값)을 `X-CSRF-Token` 헤더로 함께 보낸다.
비브라우저 클라이언트(모바일 키보드·PC 프로그램)는 같은 세션 토큰을 `Authorization: Bearer`로 보낸다 —
쿠키처럼 브라우저가 알아서 붙이는 값이 아니므로 CSRF 검사를 하지 않는다.
user_id는 users.id(UUID 문자열). 다른 모든 테이블의 user_id가 이 값을 가리킨다.

회원관리(0012): 이메일(소문자, 유일)·표시 이름·권한(role)·상태(status)·마지막 로그인. 소셜 계정은 `Identity`
(provider + subject)로 사용자에 묶이고, 비밀번호가 없는 계정(소셜 전용)도 있다.
- `email_verified`는 **제공자가 확인해 준** 이메일일 때만 참이다. 이메일 가입은 주소 소유를 확인하지 않으므로
  거짓이고, 그런 계정에는 소셜 계정을 자동으로 묶지 않는다(남의 주소로 먼저 가입해 두는 선점 방지).
- 상태가 active가 아닌 사용자는 로그인할 수 없고, 이미 열린 세션·Bearer 토큰도 `Session.is_active`에서 끊긴다.
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
MAX_SUBJECT = 128  # 제공자가 주는 사용자 식별자 길이 상한 (user_identities.subject)

SESSION_ID_CHARS = 12  # 세션 목록에 보여 주는 id = 토큰 해시 앞 12자 (토큰을 되살릴 수 없다)


class User(BaseModel):
    id: UUID
    username: str = Field(min_length=3, max_length=32)
    created_at: datetime
    # 약관·개인정보 안내 동의 (0008). 동의한 적 없으면 둘 다 None.
    consented_at: datetime | None = None
    consent_version: str | None = None
    # 회원관리 (0012). 예전 행은 role=user, status=active다.
    email: str | None = None  # 소문자로만 저장한다
    email_verified: bool = False
    display_name: str | None = None
    role: Role = "user"
    status: UserStatus = "active"
    last_login_at: datetime | None = None
    has_password: bool = True  # 소셜 전용 계정은 False (해시 자체는 도메인 모델에 싣지 않는다)


class Identity(BaseModel):
    """소셜 계정 연결. (provider, subject)는 유일하다 — 한 소셜 계정은 한 사용자에게만 묶인다.

    제공자의 토큰·이메일은 저장하지 않는다(식별자만).
    """

    provider: ProviderName
    subject: str
    user_id: UUID
    created_at: datetime


class Session(BaseModel):
    """DB에는 토큰의 해시만 둔다. 원문 토큰은 로그인 응답의 Set-Cookie(또는 토큰 발급 응답)에만 존재한다."""

    token_hash: str
    user_id: UUID
    csrf_token: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    # 세션 표의 열이 아니다 — 저장소가 세션을 읽을 때 그 사용자의 지금 상태를 함께 싣는다.
    # 정지·승인 대기로 바뀌면 다음 요청부터 쿠키·Bearer 모두 끊긴다 (세션 폐기를 놓쳐도 여기서 막힌다).
    user_status: UserStatus = "active"

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now and self.user_status == "active"

    @property
    def public_id(self) -> str:
        return self.token_hash[:SESSION_ID_CHARS]


class AuthResult(BaseModel):
    """로그인·회원가입 결과. 토큰 원문은 쿠키(또는 토큰 발급 응답)로만 나가고 직렬화에서는 빠진다."""

    user: User
    session_token: str = Field(exclude=True)
    csrf_token: str
    expires_at: datetime


class PendingSignup(BaseModel):
    """승인제(`AUTH_SIGNUP_REQUIRES_APPROVAL`)에서의 가입 결과. 계정은 만들어졌지만 세션은 열지 않는다."""

    user: User
