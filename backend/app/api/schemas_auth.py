"""인증·계정 API 스키마 (계약 §4.2). `routes/auth.py`가 쓴다.

기존 응답 필드는 그대로 두고 더하기만 했다: `AuthOut`·`MeOut`에 `is_admin`·`consented_at`·`consent_version`·
`consent_current`. (같은 이름의 옛 정의가 `api/schemas.py`에 남아 있다 — 통합 때 지운다.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.auth import Role, UserStatus

LoginMethod = Literal["password", "google", "kakao"]
TokenClient = Literal["mobile", "desktop", "api"]


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class LoginIn(BaseModel):
    username: str = Field(min_length=3, max_length=254, description="아이디 또는 이메일")
    password: str = Field(min_length=1, max_length=128)


class SignupEmailIn(BaseModel):
    """이메일 가입. 초대 코드·동의 규칙은 아이디 가입(`RegisterIn`)과 같다. 아이디는 서버가 만든다."""

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(None, max_length=40)
    invite_code: str | None = Field(None, max_length=128)
    consent: bool | None = False


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
    is_admin: bool = Field(False, description="ADMIN_USERNAMES에 있는 사용자 (관리자 화면 접근 가능)")
    consented_at: datetime | None = Field(None, description="약관·개인정보 안내에 동의한 시각")
    consent_version: str | None = Field(None, description="동의한 안내의 버전")
    consent_current: bool = Field(False, description="동의한 버전이 지금 버전(CONSENT_VERSION)과 같은가")
    email: str | None = None
    display_name: str | None = None
    role: Role = "user"
    status: UserStatus = Field(
        "active", description="pending이면 관리자 승인 전 — /v1/auth/* 밖의 API는 403 account_pending"
    )


class AuthOut(AccountFields):
    csrf_token: str = Field(description="쓰기 요청의 X-CSRF-Token 헤더 값")
    expires_at: datetime


class MeOut(AccountFields):
    csrf_token: str
    providers: list[LoginMethod] = Field(default_factory=list, description="이 계정의 로그인 수단")


class AuthConfigOut(BaseModel):
    """로그인 전 화면이 가입 폼을 어떻게 그릴지 정하는 데 쓴다. 비밀 값(초대 코드 자체)은 없다."""

    allow_register: bool
    invite_required: bool
    require_consent: bool
    consent_version: str
    app_env: Literal["development", "production"]
    requires_approval: bool = Field(False, description="새 계정이 관리자 승인을 기다리는가")
    google: bool = Field(False, description="Google 로그인이 켜져 있는가")
    kakao: bool = Field(False, description="Kakao 로그인이 켜져 있는가")


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


class MemberOut(BaseModel):
    """회원관리 목록의 한 줄. 대화 원문·첨삭 문장은 없다. `user_id`는 승인·정지 대상을 가리키는 전체 id."""

    user_id: str
    username: str
    email: str | None
    display_name: str | None
    role: Role
    status: UserStatus
    is_admin: bool
    providers: list[LoginMethod]
    created_at: datetime
    last_login_at: datetime | None


class MemberPage(BaseModel):
    total: int
    items: list[MemberOut]


class MemberPatch(BaseModel):
    status: Literal["active", "suspended"] | None = Field(None, description="pending → active가 승인")
    role: Role | None = None


class RevokeSessionsOut(BaseModel):
    revoked: int


class AccountDeleteIn(BaseModel):
    password: str = Field(
        max_length=128, description="비밀번호 재확인. 비밀번호가 없는 소셜 전용 계정은 빈 문자열"
    )


class AccountDeletionOut(BaseModel):
    contacts: int
    conversations: int
    messages: int
    graph_nodes_removed: int
    graph_error: str | None
