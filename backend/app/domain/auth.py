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

MAX_EMAIL_LOCAL = 64  # RFC 5321의 local part 상한
MAX_DNS_LABEL = 63
MIN_TLD = 2
_ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyz")
_ASCII_DIGITS = frozenset("0123456789")
# local part에 허용하는 글자 (RFC 5322 atext + 점). 따옴표로 감싼 형식·공백·주석은 받지 않는다.
_EMAIL_LOCAL_CHARS = _ASCII_LETTERS | _ASCII_DIGITS | frozenset("!#$%&'*+/=?^_`{|}~-.")
_DNS_LABEL_CHARS = _ASCII_LETTERS | _ASCII_DIGITS | frozenset("-")


def canonical_email(raw: object) -> str | None:
    """이메일을 저장·비교용 모양(앞뒤 공백 제거 + 소문자)으로 바꾼다. 받을 수 없는 값이면 None.

    가입(`auth_service.normalize_email`)과 소셜 로그인(`oauth.clean_email`)이 함께 쓰는 **한 곳**이다
    (2026-11-17 보안 검토). 허용 글자 목록으로만 본다 — 정규식이 아니라서 긴 입력에도 선형이다.
    - 전체 254자 이하, `@`는 정확히 하나.
    - 출력 가능한 ASCII만. 폭 없는 문자(U+200B 등)·전각 글자·한글 도메인처럼 눈에는 같아 보여도 다른
      주소가 되는 값은 받지 않는다 (같은 사람인 척 다른 계정을 만드는 길을 막는다).
    - local part: 1~64자, 영문·숫자와 ``!#$%&'*+/=?^_`{|}~-``, 점은 맨 앞·맨 뒤·연속으로 올 수 없다.
    - 도메인: 점으로 나뉜 라벨 둘 이상. 라벨은 1~63자의 영문·숫자·하이픈(하이픈으로 시작·끝 불가),
      마지막 라벨(TLD)은 영문만 2자 이상 — `user@127.0.0.1`·`user@[::1]` 같은 IP 주소 형식은 거절된다.
    """
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    if not email or len(email) > MAX_EMAIL or email.count("@") != 1:
        return None
    local, _, domain = email.partition("@")
    if not 1 <= len(local) <= MAX_EMAIL_LOCAL or not set(local) <= _EMAIL_LOCAL_CHARS:
        return None
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    labels = domain.split(".")
    if len(labels) < 2:  # noqa: PLR2004 - 점이 하나는 있어야 한다
        return None
    for label in labels:
        if not 1 <= len(label) <= MAX_DNS_LABEL or not set(label) <= _DNS_LABEL_CHARS:
            return None
        if label.startswith("-") or label.endswith("-"):
            return None
    tld = labels[-1]
    if len(tld) < MIN_TLD or not set(tld) <= _ASCII_LETTERS:
        return None
    return email


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
