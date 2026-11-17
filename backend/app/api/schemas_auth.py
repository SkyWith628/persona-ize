"""인증·계정 API 스키마 (계약 §4.2). `routes/auth.py`가 쓴다.

기존 응답 필드는 그대로 두고 더하기만 했다: `AuthOut`·`MeOut`에 `is_admin`·`consented_at`·`consent_version`·
`consent_current`. (같은 이름의 옛 정의가 `api/schemas.py`에 남아 있다 — 통합 때 지운다.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

TokenClient = Literal["mobile", "desktop", "api"]


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class LoginIn(BaseModel):
    """로그인. `username`에 아이디 또는 이메일(`@`가 있으면 이메일로 찾는다)을 넣는다."""

    username: str = Field(min_length=3, max_length=254, description="아이디 또는 이메일")
    password: str = Field(min_length=1, max_length=128)


class SignupIn(BaseModel):
    """이메일 가입. 아이디는 서버가 이메일 앞부분에서 만든다(응답의 `username`)."""

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(None, max_length=200, description="표시 이름 (다듬은 뒤 40자까지)")
    invite_code: str | None = Field(None, max_length=128, description="REGISTER_INVITE_CODE 서버에서 필요")
    consent: bool | None = Field(False, description="REQUIRE_CONSENT 서버에서는 true여야 한다")


class SignupPendingOut(BaseModel):
    """승인제 서버의 가입 응답(202). 계정은 만들어졌지만 로그인되지 않았다 — 쿠키도 CSRF 토큰도 없다."""

    status: Literal["pending"] = "pending"
    username: str
    email: str | None = None


class OAuthStartIn(BaseModel):
    """소셜 로그인 시작(POST). 새 계정을 만들 수 있는 조건(동의·초대 코드)을 함께 보낸다."""

    next: str | None = Field(None, max_length=512, description="로그인 뒤 돌아갈 같은 출처 경로 (기본 /)")
    consent: bool | None = Field(False, description="약관·개인정보 안내 동의 (새 계정일 때만 쓰인다)")
    invite_code: str | None = Field(None, max_length=128, description="새 계정일 때만 쓰인다")


class OAuthStartOut(BaseModel):
    authorize_url: str = Field(description="브라우저를 보낼 제공자 로그인 주소 (location.assign)")


class ProvidersOut(BaseModel):
    """로그인 전 화면용 공개 설정 2 — 가입 정책과 켜진 소셜 로그인. 비밀 값은 없다."""

    signup_open: bool
    requires_approval: bool = Field(description="가입하면 관리자 승인 전까지 로그인할 수 없다")
    invite_required: bool
    require_consent: bool
    google: bool
    kakao: bool


class RegisterIn(Credentials):
    invite_code: str | None = Field(
        None, max_length=128, description="REGISTER_INVITE_CODE가 설정된 서버에서 필요"
    )
    consent: bool | None = Field(
        False, description="약관·개인정보 안내 동의. REQUIRE_CONSENT 서버에서는 true여야 한다"
    )


class AccountFields(BaseModel):
    """로그인한 사용자에 대해 화면이 알아야 하는 것."""

    user_id: str
    username: str
    is_admin: bool = Field(False, description="role이 admin이거나 ADMIN_USERNAMES에 있는 사용자")
    consented_at: datetime | None = Field(None, description="약관·개인정보 안내에 동의한 시각")
    consent_version: str | None = Field(None, description="동의한 안내의 버전")
    consent_current: bool = Field(False, description="동의한 버전이 지금 버전(CONSENT_VERSION)과 같은가")
    # 회원관리 (2026-11-16)
    email: str | None = Field(None, description="소문자로 저장된 이메일. 아이디 가입 계정은 null")
    display_name: str | None = None
    role: Literal["user", "admin"] = "user"
    status: Literal["active", "pending", "suspended"] = "active"


class AuthOut(AccountFields):
    csrf_token: str = Field(description="쓰기 요청의 X-CSRF-Token 헤더 값")
    expires_at: datetime


class MeOut(AccountFields):
    csrf_token: str
    providers: list[str] = Field(
        default_factory=list, description="로그인 수단: password, google, kakao (연결된 것만)"
    )


class AuthConfigOut(BaseModel):
    """로그인 전 화면이 가입 폼을 어떻게 그릴지 정하는 데 쓴다. 비밀 값(초대 코드 자체)은 없다."""

    allow_register: bool
    invite_required: bool
    require_consent: bool
    consent_version: str
    app_env: Literal["development", "production"]


class ConsentIn(BaseModel):
    """본문은 `{}`. 동의 버전은 서버가 정한다(지금의 CONSENT_VERSION)."""


class SessionOut(BaseModel):
    id: str = Field(description="토큰 해시 앞 12자 (식별용, 토큰을 되살릴 수 없다)")
    created_at: datetime
    expires_at: datetime
    current: bool = Field(description="이 요청을 보낸 세션인가")


class LogoutAllOut(BaseModel):
    revoked_sessions: int = Field(description="로그아웃된 다른 세션 수 (지금 세션은 남는다)")


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class PasswordChangeOut(BaseModel):
    revoked_sessions: int = Field(description="함께 로그아웃된 다른 세션 수")


class TokenIn(LoginIn):
    client: TokenClient = Field("api", description="토큰을 쓸 곳 (기록용)")


class TokenOut(BaseModel):
    access_token: str = Field(description="이후 요청의 `Authorization: Bearer <값>`")
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    user_id: str
    username: str


class AccountDeleteIn(BaseModel):
    password: str = Field(
        "", max_length=128, description="현재 비밀번호. 비밀번호가 없는 소셜 전용 계정은 빈 문자열"
    )


class AccountDeletionOut(BaseModel):
    contacts: int
    conversations: int
    messages: int
    graph_nodes_removed: int
    graph_error: str | None
