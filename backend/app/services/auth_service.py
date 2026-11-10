"""회원가입·로그인·세션 확인.

- 로그인 실패는 이름이 없든 비밀번호가 틀리든 같은 401 메시지이고 걸리는 시간도 같다 (가입 여부 노출 금지).
- 세션 만료 `SESSION_TTL_DAYS`. 로그아웃은 revoked_at을 찍는다 (토큰 재사용 불가).
- CSRF: 세션마다 토큰 하나. 쿠키 인증으로 들어온 쓰기 요청은 `X-CSRF-Token`이 이 값과 같아야 한다.
  같은 세션 토큰을 `Authorization: Bearer`로 보낸 요청은 CSRF 검사를 하지 않는다 (`api/deps.py`).
- 가입 정책(`signup`): 가입 열림 → 초대 코드(`REGISTER_INVITE_CODE`) → 동의(`REQUIRE_CONSENT`) 순으로 본다.
  `register`는 정책 없이 계정을 만드는 안쪽 단계다 — 운영 스크립트(`prepare_demo.py`)가 직접 부른다.
- 이름은 영문·숫자·_·-만 받고, 대소문자만 다른 이름은 같은 이름으로 본다. 관리자 여부를 소문자로 비교하므로
  (`ADMIN_USERNAMES`) `Admin`으로 새로 가입해 `admin`의 권한을 얻는 일이 없어야 한다.
"""

from __future__ import annotations

import re
import secrets
from collections import deque
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import (
    ConsentRequiredError,
    ForbiddenError,
    InvalidInputError,
    InvalidInviteError,
    NotFoundError,
    TooManyAttemptsError,
    UnauthorizedError,
    VersionConflictError,
)
from app.core.ratelimit import client_key
from app.core.security import (
    hash_password,
    hash_password_async,
    new_csrf_token,
    new_token,
    token_hash,
    tokens_equal,
    verify_password_async,
)
from app.domain.auth import AuthResult, Session, User
from app.repositories.auth import AuthRepo

# 예전에는 UnauthorizedError·ForbiddenError를 이 모듈에서 정의했다. 정의는 core/errors.py로 옮겼고,
# `from app.services.auth_service import ForbiddenError` 같은 기존 import는 그대로 동작한다.
__all__ = ["MIN_PASSWORD", "AuthService", "ForbiddenError", "LoginThrottle", "UnauthorizedError"]

MIN_PASSWORD = 8
MAX_TOKEN_CHARS = 512  # 세션 토큰은 43자다. 터무니없이 긴 값은 조회하지 않는다
_USERNAME_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")
# 없는 이름으로 로그인할 때 견주는 미끼 해시(누구의 것도 아닌 임의 값, 프로세스마다 새로 만든다).
# 실제 해시와 같은 비용으로 검증 시간만 쓴다 — 설령 맞아떨어져도 사용자가 없으므로 로그인되지 않는다.
_DECOY_HASH = hash_password(secrets.token_urlsafe(16))

PRUNE_THRESHOLD = 1000  # 이 수를 넘으면 실패 기록 때마다 빈 키를 정리한다


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
        if len(self._failures) > PRUNE_THRESHOLD:  # 이름·IP를 바꿔 가며 때리면 키가 쌓인다
            self.prune_all(now)

    def prune_all(self, now: datetime) -> int:
        """창을 벗어난 기록만 남은 키를 모두 비운다. 비운 키 수."""
        empty = [k for k in list(self._failures) if not self._prune(k, now)]
        for k in empty:
            del self._failures[k]
        return len(empty)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


class AuthService:
    def __init__(self, settings: Settings, repo: AuthRepo) -> None:
        self._settings = settings
        self._repo = repo
        self._throttle = LoginThrottle(
            settings.auth_max_failures, timedelta(minutes=settings.auth_lockout_minutes)
        )

    # ---- 가입 ----

    async def signup(
        self,
        username: str,
        password: str,
        *,
        invite_code: str | None = None,
        consent: bool = False,
        client_ip: str | None = None,
    ) -> AuthResult:
        """공개 가입(API). 가입 닫힘 403 → 초대 코드 403 `invalid_invite` → 동의 422 `consent_required`."""
        if not self._settings.auth_allow_register:
            raise ForbiddenError("회원가입이 닫혀 있습니다.")
        self._check_invite(invite_code, client_ip)
        if self._settings.require_consent and not consent:
            raise ConsentRequiredError("약관과 개인정보 처리 안내에 동의해야 가입할 수 있습니다.")
        if self._settings.is_production and username.strip().lower() in self._settings.admin_username_set:
            # 관리자 여부는 이름으로만 정하므로, 운영에서는 그 이름을 공개 가입으로 선점할 수 없게 한다.
            # 관리자 계정은 서버에서 `scripts/reset_password.py --username <이름> --create`로 만든다.
            # 응답은 이미 있는 이름과 구분되지 않게 한다 (관리자 이름을 알려 주지 않는다).
            raise VersionConflictError("이미 사용 중인 이름입니다.")
        return await self.register(username, password, consent=consent)

    async def register(self, username: str, password: str, *, consent: bool = False) -> AuthResult:
        """계정을 만들고 세션을 연다. `consent`면 동의 시각·현재 버전을 함께 기록한다."""
        if not self._settings.auth_allow_register:
            raise ForbiddenError("회원가입이 닫혀 있습니다.")
        user = await self._create_user(username, password, consent=consent)
        return await self._open_session(user)

    async def create_account(self, username: str, password: str) -> User:
        """운영자가 서버에서 직접 만드는 계정 (가입이 닫혀 있어도, 초대 코드 없이). 세션은 열지 않는다."""
        return await self._create_user(username, password, consent=False)

    async def _create_user(self, username: str, password: str, *, consent: bool) -> User:
        username = username.strip()
        if not _USERNAME_RE.fullmatch(username) or not any(ch.isalnum() for ch in username):
            raise InvalidInputError("이름은 3~32자의 영문·숫자·_·-만 가능합니다.")
        if len(password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        if await self._repo.username_exists(username):  # 대소문자만 다른 이름 포함
            raise VersionConflictError("이미 사용 중인 이름입니다.")
        now = datetime.now(UTC)
        return await self._repo.create_user(
            username,
            await hash_password_async(password),
            consented_at=now if consent else None,
            consent_version=self._settings.consent_version if consent else None,
        )

    def _check_invite(self, invite_code: str | None, client_ip: str | None) -> None:
        expected = self._settings.register_invite_code.strip()
        if not expected:
            return
        key = f"invite:{client_key(client_ip)}" if client_ip else None
        now = datetime.now(UTC)
        if key is not None:
            wait = self._throttle.retry_after(key, now)
            if wait is not None:
                raise TooManyAttemptsError(
                    f"가입 시도가 너무 많습니다. {wait}초 뒤에 다시 시도하세요.", retry_after=wait
                )
        if not tokens_equal(expected, (invite_code or "").strip()):
            if key is not None:  # 초대 코드를 하나씩 맞춰 보는 것을 막는다
                self._throttle.record_failure(key, now)
            raise InvalidInviteError("초대 코드가 맞지 않습니다.")

    # ---- 로그인·세션 ----

    async def login(self, username: str, password: str, client_ip: str | None = None) -> AuthResult:
        """이름·비밀번호 확인 뒤 새 세션. 쿠키 로그인과 Bearer 토큰 발급이 함께 쓴다."""
        username = username.strip()
        now = datetime.now(UTC)
        keys = [f"user:{username.lower()}"] + ([f"ip:{client_key(client_ip)}"] if client_ip else [])
        waits = [w for w in (self._throttle.retry_after(k, now) for k in keys) if w is not None]
        if waits:
            raise TooManyAttemptsError(
                f"로그인 시도가 너무 많습니다. {max(waits)}초 뒤에 다시 시도하세요.", retry_after=max(waits)
            )
        found = await self._repo.find_user_by_username(username)
        # 없는 이름이어도 해시 한 번을 똑같이 계산한다 — 응답 시간으로 가입 여부를 알아낼 수 없게.
        stored = found[1] if found is not None else _DECOY_HASH
        password_ok = await verify_password_async(password, stored)
        if found is None or not password_ok:
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
        if found is None or not await verify_password_async(current_password, found[1]):
            raise UnauthorizedError("현재 비밀번호가 맞지 않습니다.")
        if current_password == new_password:
            raise InvalidInputError("새 비밀번호가 현재 비밀번호와 같습니다.")
        await self._repo.update_password(user.id, await hash_password_async(new_password))
        return await self._repo.revoke_other_sessions(user.id, session.token_hash, datetime.now(UTC))

    async def reset_password(self, username: str, new_password: str) -> int:
        """운영자용 재설정 (`scripts/reset_password.py`): 새 해시 저장 → 그 사용자의 세션 전부 폐기.

        이메일이 없어 사용자가 스스로 재설정할 수 없다. 폐기한 세션 수를 돌려준다. 없는 이름은 NotFoundError.
        """
        if len(new_password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        found = await self._repo.find_user_by_username(username.strip())
        if found is None:
            raise NotFoundError("그 이름의 사용자가 없습니다 (대소문자를 구분합니다).")
        user = found[0]
        await self._repo.update_password(user.id, await hash_password_async(new_password))
        self._throttle.reset(f"user:{user.username.lower()}")
        return await self._repo.revoke_all_sessions(user.id, datetime.now(UTC))

    async def logout(self, session_token: str | None) -> None:
        if session_token and len(session_token) <= MAX_TOKEN_CHARS:
            await self._repo.revoke_session(token_hash(session_token), datetime.now(UTC))

    async def logout_all(self, session: Session) -> int:
        """지금 세션만 남기고 다른 기기·토큰을 모두 로그아웃한다. 폐기한 세션 수."""
        return await self._repo.revoke_other_sessions(session.user_id, session.token_hash, datetime.now(UTC))

    async def list_sessions(self, session: Session) -> list[Session]:
        """이 사용자의 활성 세션 (최근 것 먼저)."""
        return await self._repo.list_active_sessions(session.user_id, datetime.now(UTC))

    async def resolve(self, session_token: str | None) -> Session | None:
        """쿠키 또는 Bearer의 토큰 → 유효한 세션. 없거나 만료·폐기면 None."""
        if not session_token or len(session_token) > MAX_TOKEN_CHARS:
            return None
        session = await self._repo.get_session(token_hash(session_token))
        if session is None or not session.is_active(datetime.now(UTC)):
            return None
        return session

    @staticmethod
    def csrf_ok(session: Session, header_token: str | None) -> bool:
        return tokens_equal(session.csrf_token, header_token)

    # ---- 내 정보·동의 ----

    async def me(self, session: Session) -> User:
        return await self._repo.get_user(session.user_id)

    async def record_consent(self, session: Session) -> User:
        """현재 `CONSENT_VERSION`으로 동의를 기록한다 (다시 동의하면 시각·버전이 갱신된다)."""
        return await self._repo.record_consent(
            session.user_id, self._settings.consent_version, datetime.now(UTC)
        )

    def is_admin(self, user: User) -> bool:
        return self._settings.is_admin_name(user.username)

    def consent_current(self, user: User) -> bool:
        """기록된 동의 버전이 지금 버전과 같은가 (동의한 적 없으면 False)."""
        return user.consent_version is not None and user.consent_version == self._settings.consent_version

    def prune_throttle(self, now: datetime | None = None) -> int:
        """창이 지난 로그인·초대 코드 실패 기록을 비운다 (주기 정리가 부른다). 비운 키 수."""
        return self._throttle.prune_all(now or datetime.now(UTC))

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
