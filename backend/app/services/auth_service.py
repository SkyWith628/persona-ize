"""회원가입·로그인·세션 확인.

- 로그인 실패는 이름이 없든 비밀번호가 틀리든 같은 401 메시지 (사용자 존재 여부 노출 금지).
- 세션 만료 `SESSION_TTL_DAYS`. 로그아웃은 revoked_at을 찍는다 (토큰 재사용 불가).
- CSRF: 세션마다 토큰 하나. 쿠키 인증으로 들어온 쓰기 요청은 `X-CSRF-Token`이 이 값과 같아야 한다.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import DomainError, InvalidInputError, TooManyAttemptsError
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


class LoginThrottle:
    """이름·IP별 최근 실패 시각을 기억한다. 한도에 닿으면 가장 오래된 실패가 창을 벗어날 때까지 막는다.

    프로세스 안 메모리다 — 여러 프로세스면 각자 센다(완화는 되지만 완벽하지 않다).
    성공하면 그 키의 기록을 지운다.
    """

    def __init__(self, max_failures: int, lockout: timedelta) -> None:
        self._max = max_failures
        self._window = lockout
        self._failures: dict[str, deque[datetime]] = {}

    def _prune(self, key: str, now: datetime) -> deque[datetime]:
        q = self._failures.setdefault(key, deque())
        while q and now - q[0] >= self._window:
            q.popleft()
        return q

    def retry_after(self, key: str, now: datetime) -> int | None:
        """막혀 있으면 몇 초 뒤에 풀리는지, 아니면 None."""
        q = self._prune(key, now)
        if len(q) < self._max:
            return None
        return max(int((q[0] + self._window - now).total_seconds()), 1)

    def record_failure(self, key: str, now: datetime) -> None:
        self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


class AuthService:
    def __init__(self, settings: Settings, repo: AuthRepo) -> None:
        self._settings = settings
        self._repo = repo
        self._throttle = LoginThrottle(
            settings.auth_max_failures, timedelta(minutes=settings.auth_lockout_minutes)
        )

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

    async def login(self, username: str, password: str, client_ip: str | None = None) -> AuthResult:
        username = username.strip()
        now = datetime.now(UTC)
        keys = [f"user:{username.lower()}"] + ([f"ip:{client_ip}"] if client_ip else [])
        waits = [w for w in (self._throttle.retry_after(k, now) for k in keys) if w is not None]
        if waits:
            raise TooManyAttemptsError(f"로그인 시도가 너무 많습니다. {max(waits)}초 뒤에 다시 시도하세요.")
        found = await self._repo.find_user_by_username(username)
        if found is None or not verify_password(password, found[1]):
            for k in keys:
                self._throttle.record_failure(k, now)
            raise UnauthorizedError("이름 또는 비밀번호가 맞지 않습니다.")
        for k in keys:
            self._throttle.reset(k)
        return await self._open_session(found[0])

    async def change_password(self, session: Session, current_password: str, new_password: str) -> int:
        """현재 비밀번호 확인 → 새 해시 저장 → 다른 세션 전부 폐기. 폐기한 세션 수를 돌려준다."""
        if len(new_password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        user = await self._repo.get_user(session.user_id)
        found = await self._repo.find_user_by_username(user.username)
        if found is None or not verify_password(current_password, found[1]):
            raise UnauthorizedError("현재 비밀번호가 맞지 않습니다.")
        if current_password == new_password:
            raise InvalidInputError("새 비밀번호가 현재 비밀번호와 같습니다.")
        await self._repo.update_password(user.id, hash_password(new_password))
        return await self._repo.revoke_other_sessions(user.id, session.token_hash, datetime.now(UTC))

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
