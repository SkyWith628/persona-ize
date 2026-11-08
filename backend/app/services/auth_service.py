"""회원가입·로그인·세션 확인.

- 로그인 실패는 이름이 없든 비밀번호가 틀리든 같은 401 메시지 (사용자 존재 여부 노출 금지).
- 세션 만료 `SESSION_TTL_DAYS`. 로그아웃은 revoked_at을 찍는다 (토큰 재사용 불가).
- CSRF: 세션마다 토큰 하나. 쿠키 인증으로 들어온 쓰기 요청은 `X-CSRF-Token`이 이 값과 같아야 한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import DomainError, InvalidInputError
from app.core.security import (
    hash_password,
    new_csrf_token,
    new_token,
    token_hash,
    tokens_equal,
    verify_password,
)
from app.domain.auth import AuthResult, Session, User
from app.repositories.auth import AuthRepo

MIN_PASSWORD = 8


class UnauthorizedError(DomainError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class AuthService:
    def __init__(self, settings: Settings, repo: AuthRepo) -> None:
        self._settings = settings
        self._repo = repo

    async def register(self, username: str, password: str) -> AuthResult:
        if not self._settings.auth_allow_register:
            raise ForbiddenError("회원가입이 닫혀 있습니다.")
        username = username.strip()
        if not (3 <= len(username) <= 32) or not username.replace("_", "").replace("-", "").isalnum():
            raise InvalidInputError("이름은 3~32자의 영문·숫자·_·-만 가능합니다.")
        if len(password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        user = await self._repo.create_user(username, hash_password(password))
        return await self._open_session(user)

    async def login(self, username: str, password: str) -> AuthResult:
        found = await self._repo.find_user_by_username(username.strip())
        if found is None or not verify_password(password, found[1]):
            raise UnauthorizedError("이름 또는 비밀번호가 맞지 않습니다.")
        return await self._open_session(found[0])

    async def logout(self, session_token: str | None) -> None:
        if session_token:
            await self._repo.revoke_session(token_hash(session_token), datetime.now(UTC))

    async def resolve(self, session_token: str | None) -> Session | None:
        """쿠키의 토큰 → 유효한 세션. 없거나 만료·폐기면 None."""
        if not session_token:
            return None
        session = await self._repo.get_session(token_hash(session_token))
        if session is None or not session.is_active(datetime.now(UTC)):
            return None
        return session

    @staticmethod
    def csrf_ok(session: Session, header_token: str | None) -> bool:
        return tokens_equal(session.csrf_token, header_token)

    async def me(self, session: Session) -> User:
        return await self._repo.get_user(session.user_id)

    async def _open_session(self, user: User) -> AuthResult:
        now = datetime.now(UTC)
        token = new_token()
        session = Session(
            token_hash=token_hash(token),
            user_id=user.id,
            csrf_token=new_csrf_token(),
            created_at=now,
            expires_at=now + timedelta(days=self._settings.session_ttl_days),
        )
        await self._repo.create_session(session)
        return AuthResult(
            user=user, session_token=token, csrf_token=session.csrf_token, expires_at=session.expires_at
        )
