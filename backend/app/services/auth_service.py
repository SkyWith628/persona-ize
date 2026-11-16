"""회원가입·로그인·세션 확인.

- 로그인 실패는 이름이 없든 비밀번호가 틀리든 같은 401 메시지 (사용자 존재 여부 노출 금지).
- 세션 만료 `SESSION_TTL_DAYS`. 로그아웃은 revoked_at을 찍는다 (토큰 재사용 불가).
- CSRF: 세션마다 토큰 하나. 쿠키 인증으로 들어온 쓰기 요청은 `X-CSRF-Token`이 이 값과 같아야 한다.
- 가입 정책: `AUTH_ALLOW_REGISTER`가 false면 아이디·이메일·소셜 신규 가입 모두 403 `signup_closed`.
  `AUTH_SIGNUP_REQUIRES_APPROVAL`이면 새 계정은 pending(승인 대기)으로 만든다.
- 로그인은 아이디 또는 이메일(`@` 포함)로. 정지 계정은 비밀번호가 맞아도 403 `account_suspended`.
- 소셜 로그인: 연결된 계정 → 그 사용자, 아니면 **확인된** 이메일이 같은 기존 계정에 연결, 아니면 새 계정.
  확인되지 않은 이메일로는 절대 연결하지 않는다 (남의 주소를 적어 계정을 가로채는 것을 막는다).
- `ADMIN_USERNAMES`의 이름은 공개 가입으로 만들 수 없다 (먼저 가입해 관리자가 되는 것을 막는다).
"""

from __future__ import annotations

import re
import secrets
from collections import deque
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import (
    AccountSuspendedError,
    DomainError,
    InvalidInputError,
    SignupClosedError,
    TooManyAttemptsError,
    VersionConflictError,
)
from app.core.security import (
    hash_password,
    new_csrf_token,
    new_token,
    token_hash,
    tokens_equal,
    verify_password,
)
from app.domain.auth import MAX_DISPLAY_NAME, MAX_EMAIL, AuthResult, Role, Session, User, UserStatus
from app.repositories.auth import EMAIL_TAKEN, USERNAME_TAKEN, AuthRepo
from app.services.oauth import OAuthProfile

MIN_PASSWORD = 8
GENERATED_USERNAME_TRIES = 8
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw: str) -> str:
    """앞뒤 공백을 떼고 소문자로. 형식·길이가 맞지 않으면 InvalidInputError."""
    email = raw.strip().lower()
    if not email or len(email) > MAX_EMAIL or not _EMAIL_RE.fullmatch(email):
        raise InvalidInputError("이메일 형식이 올바르지 않습니다.")
    return email


def username_seed(text: str) -> str:
    """이메일 앞부분·제공자 이름에서 아이디 후보를 만든다 (영문 소문자·숫자·_·-, 3~24자)."""
    seed = re.sub(r"[^a-z0-9_-]", "_", text.lower()).strip("_-")[:24].rstrip("_-")
    return seed if len(seed) >= 3 else "user"


class UnauthorizedError(DomainError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


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

    @property
    def admin_names(self) -> set[str]:
        return {name.strip() for name in self._settings.admin_usernames.split(",") if name.strip()}

    def is_admin(self, user: User) -> bool:
        """역할이 admin이거나 `ADMIN_USERNAMES`에 있는 아이디 (초기 관리자)."""
        return user.role == "admin" or user.username in self.admin_names

    @property
    def _new_status(self) -> UserStatus:
        return "pending" if self._settings.auth_signup_requires_approval else "active"

    def _require_signup_open(self) -> None:
        if not self._settings.auth_allow_register:
            raise SignupClosedError("회원가입이 닫혀 있습니다.")

    async def register(self, username: str, password: str) -> AuthResult:
        """아이디 가입 (하위 호환). 가입 정책(닫힘·승인 대기)은 이메일 가입과 같다."""
        self._require_signup_open()
        if username.strip() in self.admin_names:
            raise VersionConflictError(USERNAME_TAKEN)  # 예약된 이름인지는 알리지 않는다
        return await self._open_session(
            await self.create_account(username, password, status=self._new_status)
        )

    async def signup(self, email: str, password: str, display_name: str | None = None) -> AuthResult:
        """이메일 가입. 아이디는 이메일 앞부분에서 만든다. 이메일이 이미 있으면 409."""
        self._require_signup_open()
        email = normalize_email(email)
        name = " ".join((display_name or "").split()) or None
        if name is not None and len(name) > MAX_DISPLAY_NAME:
            raise InvalidInputError(f"표시 이름은 {MAX_DISPLAY_NAME}자까지입니다.")
        if len(password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        user = await self._create_with_generated_username(
            email.partition("@")[0],
            hash_password(password),
            email=email,
            display_name=name,
            status=self._new_status,
        )
        return await self._open_session(user)

    async def create_account(
        self, username: str, password: str, *, role: Role = "user", status: UserStatus = "active"
    ) -> User:
        """계정만 만든다 (세션 없음). 가입이 닫힌 배포에서 운영자가 쓴다 — `app.tools.create_user`."""
        username = username.strip()
        if not (3 <= len(username) <= 32) or not username.replace("_", "").replace("-", "").isalnum():
            raise InvalidInputError("이름은 3~32자의 영문·숫자·_·-만 가능합니다.")
        if len(password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        return await self._repo.create_user(username, hash_password(password), role=role, status=status)

    async def reset_password(self, username: str, password: str, *, make_admin: bool = False) -> User:
        """운영자가 서버에서 비밀번호를 새로 정한다. 그 계정의 세션은 모두 폐기된다."""
        if len(password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        found = await self._repo.find_user_by_username(username.strip())
        if found is None:
            raise InvalidInputError("그런 아이디가 없습니다.")
        user = found[0]
        await self._repo.update_password(user.id, hash_password(password))
        await self._repo.revoke_user_sessions(user.id, datetime.now(UTC))
        if make_admin and user.role != "admin":
            user = await self._repo.update_user(user.id, role="admin")
        return user

    async def login(self, username: str, password: str, client_ip: str | None = None) -> AuthResult:
        """`username`은 아이디 또는 이메일. 없는 계정·틀린 비밀번호·비밀번호 없는 계정 모두 같은 401."""
        identifier = username.strip()
        now = datetime.now(UTC)
        keys = [f"user:{identifier.lower()}"] + ([f"ip:{client_ip}"] if client_ip else [])
        waits = [w for w in (self._throttle.retry_after(k, now) for k in keys) if w is not None]
        if waits:
            raise TooManyAttemptsError(f"로그인 시도가 너무 많습니다. {max(waits)}초 뒤에 다시 시도하세요.")
        if "@" in identifier:  # 아이디에는 @가 들어갈 수 없다
            found = await self._repo.find_user_by_email(identifier.lower())
        else:
            found = await self._repo.find_user_by_username(identifier)
        if found is None or found[1] is None or not verify_password(password, found[1]):
            for k in keys:
                self._throttle.record_failure(k, now)
            raise UnauthorizedError("이름 또는 비밀번호가 맞지 않습니다.")
        for k in keys:
            self._throttle.reset(k)
        self._require_not_suspended(found[0])  # 비밀번호를 맞힌 뒤에만 상태를 알린다
        return await self._open_session(found[0])

    async def login_with_identity(self, provider: str, profile: OAuthProfile) -> AuthResult:
        """소셜 로그인. 연결된 계정 → 확인된 이메일이 같은 계정에 연결 → 새 계정 순."""
        identity = await self._repo.find_identity(provider, profile.subject)
        if identity is not None:
            user = await self._repo.get_user(identity.user_id)
        else:
            user = await self._link_or_create(provider, profile)
        self._require_not_suspended(user)
        return await self._open_session(user)

    async def _link_or_create(self, provider: str, profile: OAuthProfile) -> User:
        verified_email = profile.email if profile.email and profile.email_verified else None
        if verified_email is not None:
            found = await self._repo.find_user_by_email(verified_email)
            if found is not None:
                return await self._attach(found[0], provider, profile, created=False)
        self._require_signup_open()
        # 확인되지 않은 이메일은 계정의 이메일로 삼지 않는다 (연결 기록에만 참고로 남긴다).
        user = await self._create_with_generated_username(
            (verified_email or "").partition("@")[0] or provider,
            None,
            email=verified_email,
            display_name=profile.name,
            status=self._new_status,
        )
        return await self._attach(user, provider, profile, created=True)

    async def _attach(self, user: User, provider: str, profile: OAuthProfile, *, created: bool) -> User:
        """소셜 계정을 사용자에 묶는다. 같은 로그인이 동시에 두 번 들어와 먼저 묶였으면 그쪽 사용자를 쓴다."""
        try:
            await self._repo.add_identity(user.id, provider, profile.subject, profile.email)
        except VersionConflictError:
            if created:
                await self._repo.delete_user(user.id)  # 방금 만든 빈 계정을 남기지 않는다
            identity = await self._repo.find_identity(provider, profile.subject)
            if identity is None:
                raise
            return await self._repo.get_user(identity.user_id)
        return user

    async def _create_with_generated_username(
        self,
        seed: str,
        password_hash: str | None,
        *,
        email: str | None,
        display_name: str | None,
        status: UserStatus,
    ) -> User:
        """아이디를 만들어 계정을 넣는다. 겹치면 짧은 무작위 꼬리를 붙여 다시 시도한다."""
        base = username_seed(seed)
        reserved = self.admin_names
        for attempt in range(GENERATED_USERNAME_TRIES):
            candidate = base if attempt == 0 else f"{base}_{secrets.token_hex(3)}"
            if candidate in reserved or await self._repo.find_user_by_username(candidate) is not None:
                continue
            try:
                return await self._repo.create_user(
                    candidate, password_hash, email=email, display_name=display_name, status=status
                )
            except VersionConflictError as exc:
                if exc.message == EMAIL_TAKEN:
                    raise
        raise VersionConflictError("아이디를 만들지 못했습니다. 잠시 뒤 다시 시도하세요.")

    @staticmethod
    def _require_not_suspended(user: User) -> None:
        if user.status == "suspended":
            raise AccountSuspendedError("정지된 계정입니다. 관리자에게 문의하세요.")

    async def change_password(self, session: Session, current_password: str, new_password: str) -> int:
        """현재 비밀번호 확인 → 새 해시 저장 → 다른 세션 전부 폐기. 폐기한 세션 수를 돌려준다."""
        if len(new_password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        user = await self._repo.get_user(session.user_id)
        found = await self._repo.find_user_by_username(user.username)
        if found is None or found[1] is None or not verify_password(current_password, found[1]):
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

    async def providers_of(self, user: User) -> list[str]:
        """로그인 수단: 비밀번호가 있으면 password, 그리고 연결된 소셜 제공자."""
        linked = (await self._repo.list_providers([user.id])).get(user.id, [])
        return (["password"] if user.has_password else []) + linked

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
        await self._repo.touch_login(user.id, now)
        return AuthResult(
            user=user.model_copy(update={"last_login_at": now}),
            session_token=token,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )
