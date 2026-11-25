"""회원가입·로그인·세션 확인.

- 로그인 실패는 이름이 없든 비밀번호가 틀리든 같은 401 메시지이고 걸리는 시간도 같다 (가입 여부 노출 금지).
- 세션 만료 `SESSION_TTL_DAYS`. 로그아웃은 revoked_at을 찍는다 (토큰 재사용 불가).
- CSRF: 세션마다 토큰 하나. 쿠키 인증으로 들어온 쓰기 요청은 `X-CSRF-Token`이 이 값과 같아야 한다.
  같은 세션 토큰을 `Authorization: Bearer`로 보낸 요청은 CSRF 검사를 하지 않는다 (`api/deps.py`).
- 가입 정책(`signup`): 가입 열림 → 초대 코드(`REGISTER_INVITE_CODE`) → 동의(`REQUIRE_CONSENT`) → 이름·비밀번호
  형식 → 이름 중복·예약 순으로 본다. 운영 모드에서 `ADMIN_USERNAMES`의 이름은 공개 가입으로 만들 수 없고
  (선점 방지), 그 응답은 형식 검사 뒤에 "이미 사용 중"으로 나간다 — 어떤 입력으로도 일반 이름과 구분되지 않게.
  `register`는 정책 없이 계정을 만드는 안쪽 단계다 — 운영 스크립트(`prepare_demo.py`)가 직접 부른다.
- 이름은 영문·숫자·_·-만 받고, 대소문자만 다른 이름은 같은 이름으로 본다 (`Admin`으로 새로 가입해 `admin`과
  헷갈리게 하는 일이 없게). 관리자 판정은 `Settings.is_admin_name` 한 곳이다 (대소문자까지 일치).
- 실패 제한(`FailureLimits`): 같은 (이름, IP)에서 `AUTH_MAX_FAILURES`번 틀리면 그 쌍만 잠근다. 한 IP의 실패
  합계, 한 이름의 실패 합계는 그 몇 배(`AUTH_IP_FAILURE_FACTOR`·`AUTH_NAME_FAILURE_FACTOR`)에서 잠근다.
  예전에는 이름 하나·IP 하나가 같은 한도라, 남의 계정을 5번 틀려 15분씩 잠그거나 같은 공유기 뒤의 한 사람이
  틀려 모두가 막혔다. 쿠키 로그인과 토큰 발급은 같은 기록을 쓰고, 초대 코드 실패도 같은 규칙이다(기록은 따로).

회원관리·소셜 로그인 (2026-11-16, `design/iphone-mobile` c001cbb에서 옮김 — 위 규칙 위에 얹었다):
- 로그인 식별자는 아이디 또는 이메일(`@`가 있으면 이메일 — 아이디에는 `@`가 못 들어간다). 이메일 로그인도
  같은 `FailureLimits`를 지나고, 없는 이메일·틀린 비밀번호·비밀번호 없는 계정이 모두 같은 401에 같은
  해시 비용이다.
- 실패 제한의 "이름"은 **계정**이다(2026-11-17): 계정을 찾았으면 키는 그 계정 아이디의 소문자 — 아이디로
  틀리든 이메일로 틀리든 한 계정의 실패로 세고, 한쪽으로 성공하면 함께 지운다. 계정이 없을 때만 소문자로
  바꾼 입력값이 키다. 예전에는 키가 입력값이라 아이디와 이메일을 번갈아 대면 한도의 두 배까지 시도할 수
  있었다. 식별자 하나만 보면 있는 계정과 없는 계정이 여전히 같은 횟수에 같은 응답이다. 남는 것: 아이디로
  잠근 직후 이메일을 대면 첫 시도가 429라 "이 이메일이 그 아이디의 것"임을 알 수 있다(반대도) — 한도를
  지키는 쪽을 골랐다(`docs/project/decisions.md`).
- 상태: active만 로그인된다. pending(승인 대기)·suspended(정지)는 **비밀번호가 맞은 뒤에만** 403
  `account_pending`·`account_suspended`로 알린다(틀리면 여전히 401). 열려 있던 세션·Bearer 토큰은
  `Session.is_active`가 사용자 상태를 함께 보므로 다음 요청부터 끊긴다.
- 승인제(`AUTH_SIGNUP_REQUIRES_APPROVAL`): 새 계정은 pending으로 만들고 세션을 열지 않는다(`PendingSignup`).
  서버에서 만드는 계정(`create_account`, `register`)은 승인 없이 active다.
- 이메일 가입(`signup_email`): 아이디 가입과 같은 문(가입 열림 → 초대 코드 → 동의)을 지난다. 아이디는 이메일
  앞부분에서 만들고, `ADMIN_USERNAMES`의 이름과는 (대소문자 무시) 절대 겹치지 않게 한다. 주소 소유는 확인하지
  않는다 → `email_verified=False`.
- 소셜 로그인(`login_with_identity`): ① 이미 묶인 (provider, subject) → 그 사용자. ② 제공자가 **확인해 준**
  이메일이 기존 계정의 **확인된** 이메일과 같으면 그 계정에 묶는다. 기존 계정의 이메일이 확인되지 않은
  것(이메일 가입)이면 묶지 않고 409 `email_in_use` — 남의 주소로 먼저 가입해 둔 계정에 주인이 딸려 들어가는
  일을 막는다. ③ 아니면 새 계정(가입 열림·초대 코드·동의·승인제를 그대로 따른다). 확인되지 않은 이메일은
  계정에 적지 않는다.
- 관리자: `role == "admin"` 이거나 이름이 `ADMIN_USERNAMES`에 있으면 관리자다(`is_admin` 한 곳).
"""

from __future__ import annotations

import re
import secrets
from collections import deque
from collections.abc import Callable, Hashable
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import (
    AccountPendingError,
    AccountSuspendedError,
    ConsentRequiredError,
    EmailInUseError,
    ForbiddenError,
    InvalidInputError,
    InvalidInviteError,
    NotFoundError,
    SignupClosedError,
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
from app.domain.auth import (
    MAX_DISPLAY_NAME,
    AuthResult,
    PendingSignup,
    Role,
    Session,
    User,
    UserStatus,
    canonical_email,
)
from app.repositories.auth import EMAIL_TAKEN, USERNAME_TAKEN, AuthRepo
from app.services.oauth import OAuthProfile

# 예전에는 UnauthorizedError·ForbiddenError를 이 모듈에서 정의했다. 정의는 core/errors.py로 옮겼고,
# `from app.services.auth_service import ForbiddenError` 같은 기존 import는 그대로 동작한다.
__all__ = [
    "MIN_PASSWORD",
    "AuthService",
    "ForbiddenError",
    "LoginThrottle",
    "UnauthorizedError",
    "normalize_email",
    "username_seed",
]

MIN_PASSWORD = 8
MAX_TOKEN_CHARS = 512  # 세션 토큰은 43자다. 터무니없이 긴 값은 조회하지 않는다
_USERNAME_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")
# 없는 이름으로 로그인할 때 견주는 미끼 해시(누구의 것도 아닌 임의 값, 프로세스마다 새로 만든다).
# 실제 해시와 같은 비용으로 검증 시간만 쓴다 — 설령 맞아떨어져도 사용자가 없으므로 로그인되지 않는다.
_DECOY_HASH = hash_password(secrets.token_urlsafe(16))

PRUNE_THRESHOLD = 1000  # 이 수를 넘으면 실패 기록 때마다 빈 키를 정리한다

GENERATED_USERNAME_TRIES = 8
MAX_SEED_CHARS = 24  # 만든 아이디 = 앞부분(24자까지) + `_` + 무작위 6자 ≤ 32자
_SEED_STRIP_RE = re.compile(r"[^a-z0-9_-]")


def normalize_email(raw: str) -> str:
    """앞뒤 공백을 떼고 소문자로. 형식·길이가 맞지 않으면 InvalidInputError.

    규칙은 `domain.auth.canonical_email` 한 곳에 있다 (ASCII만, `@` 하나, DNS 모양 도메인, 254자 이하).
    """
    email = canonical_email(raw)
    if email is None:
        raise InvalidInputError("이메일 형식이 올바르지 않습니다.")
    return email


def username_seed(text: str) -> str:
    """이메일 앞부분·제공자 이름에서 아이디 후보를 만든다 (영문 소문자·숫자·_·-, 3~24자). 안 되면 `user`."""
    seed = _SEED_STRIP_RE.sub("_", text.lower()).strip("_-")[:MAX_SEED_CHARS].rstrip("_-")
    return seed if len(seed) >= 3 and any(ch.isalnum() for ch in seed) else "user"  # noqa: PLR2004


def clean_display_name(raw: str | None) -> str | None:
    """표시 이름: 제어 문자를 지우고 공백을 하나로. 비면 None, 길면 InvalidInputError."""
    printable = "".join(ch if ch.isprintable() else " " for ch in (raw or ""))
    name = " ".join(printable.split())
    if len(name) > MAX_DISPLAY_NAME:
        raise InvalidInputError(f"표시 이름은 {MAX_DISPLAY_NAME}자까지입니다.")
    return name or None


class LoginThrottle:
    """키별 최근 실패 시각을 기억한다. 한도에 닿으면 가장 오래된 실패가 창을 벗어날 때까지 막는다.

    프로세스 안 메모리다 — 여러 프로세스면 각자 센다(완화는 되지만 완벽하지 않다).
    키는 이름, IP, (이름, IP) 쌍 무엇이든 된다. 어떤 키를 어떤 한도로 볼지는 `FailureLimits`가 정한다.
    """

    def __init__(self, max_failures: int, lockout: timedelta) -> None:
        self._max = max_failures
        self._window = lockout
        self._failures: dict[Hashable, deque[datetime]] = {}

    def _prune(self, key: Hashable, now: datetime) -> deque[datetime]:
        q = self._failures.setdefault(key, deque())
        while q and now - q[0] >= self._window:
            q.popleft()
        return q

    def retry_after(self, key: Hashable, now: datetime) -> int | None:
        """막혀 있으면 몇 초 뒤에 풀리는지, 아니면 None."""
        q = self._prune(key, now)
        if len(q) < self._max:
            return None
        return max(int((q[0] + self._window - now).total_seconds()), 1)

    def record_failure(self, key: Hashable, now: datetime) -> None:
        self._prune(key, now).append(now)
        if len(self._failures) > PRUNE_THRESHOLD:  # 이름·IP를 바꿔 가며 때리면 키가 쌓인다
            self.prune_all(now)

    def prune_all(self, now: datetime) -> int:
        """창을 벗어난 기록만 남은 키를 모두 비운다. 비운 키 수."""
        empty = [k for k in list(self._failures) if not self._prune(k, now)]
        for k in empty:
            del self._failures[k]
        return len(empty)

    def reset(self, key: Hashable) -> None:
        self._failures.pop(key, None)

    def reset_where(self, matches: Callable[[Hashable], bool]) -> None:
        for key in [k for k in self._failures if matches(k)]:
            del self._failures[key]


class FailureLimits:
    """실패 제한 한 벌 — (이름, IP) 쌍 · IP 단독 · 이름 단독을 서로 다른 한도로 본다.

    - 쌍: `max_failures`. 같은 곳에서 같은 계정을 계속 틀리는 것(추측, 또는 본인의 오타)만 막는다.
      다른 IP의 올바른 로그인은 막지 않고, 같은 IP의 다른 사람도 막지 않는다.
    - IP 단독: `max_failures × ip_factor`. 한 곳에서 여러 계정을 돌려 가며 추측하는 것을 막는다.
      성공해도 지우지 않는다 — 자기 계정으로 로그인해 남의 계정을 추측한 횟수를 지울 수 없게.
    - 이름 단독: `max_failures × name_factor`. 여러 IP로 한 계정을 추측하는 것을 늦춘다. 이 한도에 닿으면
      그 계정은 어디서든 잠시 막힌다 — 남의 계정을 잠그려면 이제 서로 다른 IP 여러 개가 필요하다.
    배수가 0이면 그 범위는 보지 않는다. IP를 모르는 호출(스크립트·테스트)은 쌍과 이름만 본다.
    """

    def __init__(self, max_failures: int, window: timedelta, *, ip_factor: int, name_factor: int) -> None:
        self.pair = LoginThrottle(max_failures, window)
        self.by_ip = LoginThrottle(max_failures * ip_factor, window) if ip_factor > 0 else None
        self.by_name = LoginThrottle(max_failures * name_factor, window) if name_factor > 0 else None

    def _scopes(self, name: str, ip: str | None) -> list[tuple[LoginThrottle, Hashable]]:
        scopes: list[tuple[LoginThrottle, Hashable]] = [(self.pair, (name, ip))]
        if ip is not None and self.by_ip is not None:
            scopes.append((self.by_ip, ip))
        if self.by_name is not None:
            scopes.append((self.by_name, name))
        return scopes

    def retry_after(self, name: str, ip: str | None, now: datetime) -> int | None:
        """어느 범위든 막혀 있으면 다 풀릴 때까지 남은 초, 아니면 None."""
        waits = [w for throttle, key in self._scopes(name, ip) if (w := throttle.retry_after(key, now))]
        return max(waits) if waits else None

    def record_failure(self, name: str, ip: str | None, now: datetime) -> None:
        for throttle, key in self._scopes(name, ip):
            throttle.record_failure(key, now)

    def record_success(self, name: str, ip: str | None) -> None:
        self.pair.reset((name, ip))
        if self.by_name is not None:
            self.by_name.reset(name)

    def forget_name(self, name: str) -> None:
        """이 이름의 기록을 모든 IP에 걸쳐 지운다 (운영자가 비밀번호를 재설정했을 때)."""
        self.pair.reset_where(lambda key: isinstance(key, tuple) and key[0] == name)
        if self.by_name is not None:
            self.by_name.reset(name)

    def prune_all(self, now: datetime) -> int:
        return sum(t.prune_all(now) for t in (self.pair, self.by_ip, self.by_name) if t is not None)


class AuthService:
    def __init__(self, settings: Settings, repo: AuthRepo) -> None:
        self._settings = settings
        self._repo = repo
        window = timedelta(minutes=settings.auth_lockout_minutes)
        # 쿠키 로그인과 Bearer 토큰 발급이 같은 기록을 쓴다 (두 경로를 번갈아 써도 시도 횟수가 늘지 않는다).
        self._login_limits = FailureLimits(
            settings.auth_max_failures,
            window,
            ip_factor=settings.auth_ip_failure_factor,
            name_factor=settings.auth_name_failure_factor,
        )
        # 초대 코드: 추측하는 쪽은 이름을 마음대로 바꾸므로 이름 단독 범위는 뜻이 없다 — 쌍과 IP만 본다.
        self._invite_limits = FailureLimits(
            settings.auth_max_failures, window, ip_factor=settings.auth_ip_failure_factor, name_factor=0
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
    ) -> AuthResult | PendingSignup:
        """공개 가입(API). 가입 닫힘 403 → 초대 코드 403 `invalid_invite` → 동의 422 `consent_required`
        → 이름·비밀번호 형식 422 → 이미 있는(또는 운영 모드의 관리자) 이름 409.

        승인제면 계정을 pending으로 만들고 세션 없이 `PendingSignup`을 돌려준다.
        """
        self._require_signup_open()
        self._check_invite(invite_code, username, client_ip)
        self._require_consent(consent)
        user = await self._create_user(
            username, password, consent=consent, public=True, status=self._new_status
        )
        return await self._finish_signup(user)

    async def signup_email(
        self,
        email: str,
        password: str,
        *,
        display_name: str | None = None,
        invite_code: str | None = None,
        consent: bool = False,
        client_ip: str | None = None,
    ) -> AuthResult | PendingSignup:
        """이메일 가입(API). 문은 `signup`과 같다(가입 닫힘 → 초대 코드 → 동의 → 형식 → 중복 409).

        아이디는 이메일 앞부분에서 만든다. 주소 소유는 확인하지 않는다(`email_verified=False`).
        """
        self._require_signup_open()
        self._check_invite(invite_code, email, client_ip)
        self._require_consent(consent)
        email = normalize_email(email)
        name = clean_display_name(display_name)
        if len(password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        if await self._repo.find_user_by_email(email) is not None:
            raise VersionConflictError(EMAIL_TAKEN)
        user = await self._create_with_generated_username(
            email.partition("@")[0],
            await hash_password_async(password),
            email=email,
            email_verified=False,
            display_name=name,
            consent=consent,
        )
        return await self._finish_signup(user)

    async def register(self, username: str, password: str, *, consent: bool = False) -> AuthResult:
        """계정을 만들고 세션을 연다. `consent`면 동의 시각·현재 버전을 함께 기록한다.

        정책 없는 안쪽 단계다(스크립트용) — 승인제여도 active로 만든다.
        """
        self._require_signup_open()
        user = await self._create_user(username, password, consent=consent)
        return await self._open_session(user)

    async def create_account(self, username: str, password: str, *, role: Role = "user") -> User:
        """운영자가 서버에서 직접 만드는 계정 (가입이 닫혀 있어도, 초대 코드·승인 없이). 세션은 없다."""
        return await self._create_user(username, password, consent=False, role=role)

    @property
    def _new_status(self) -> UserStatus:
        return "pending" if self._settings.auth_signup_requires_approval else "active"

    def _require_signup_open(self) -> None:
        if not self._settings.auth_allow_register:
            raise SignupClosedError("회원가입이 닫혀 있습니다.")

    def _require_consent(self, consent: bool) -> None:
        if self._settings.require_consent and not consent:
            raise ConsentRequiredError("약관과 개인정보 처리 안내에 동의해야 가입할 수 있습니다.")

    async def _finish_signup(self, user: User) -> AuthResult | PendingSignup:
        """가입 마무리: active면 세션을 열고, 승인 대기면 세션 없이 끝낸다."""
        if user.status != "active":
            return PendingSignup(user=user)
        return await self._open_session(user)

    async def _create_with_generated_username(
        self,
        seed: str,
        password_hash: str | None,
        *,
        email: str | None,
        email_verified: bool,
        display_name: str | None,
        consent: bool,
    ) -> User:
        """아이디를 만들어 공개 가입 계정을 넣는다. 겹치면 무작위 꼬리를 붙여 다시 시도한다.

        `ADMIN_USERNAMES`의 이름과는 대소문자를 무시하고 겹치지 않게 한다(개발 모드에서도) — 이메일 앞부분을
        관리자 이름으로 골라 관리자가 되는 길을 막는다.
        """
        base = username_seed(seed)
        reserved = self._settings.admin_username_set
        now = datetime.now(UTC)
        for attempt in range(GENERATED_USERNAME_TRIES):
            candidate = base if attempt == 0 else f"{base}_{secrets.token_hex(3)}"
            if candidate.lower() in reserved or await self._repo.username_exists(candidate):
                continue
            try:
                return await self._repo.create_user(
                    candidate,
                    password_hash,
                    consented_at=now if consent else None,
                    consent_version=self._settings.consent_version if consent else None,
                    email=email,
                    email_verified=email_verified,
                    display_name=display_name,
                    status=self._new_status,
                )
            except VersionConflictError as exc:
                if exc.message != USERNAME_TAKEN:  # 이메일이 그 사이에 가입됐다
                    raise
        raise VersionConflictError("아이디를 만들지 못했습니다. 잠시 뒤 다시 시도하세요.")

    async def _create_user(
        self,
        username: str,
        password: str,
        *,
        consent: bool,
        public: bool = False,
        role: Role = "user",
        status: UserStatus = "active",
    ) -> User:
        username = username.strip()
        if not _USERNAME_RE.fullmatch(username) or not any(ch.isalnum() for ch in username):
            raise InvalidInputError("이름은 3~32자의 영문·숫자·_·-만 가능합니다.")
        if len(password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        taken = await self._repo.username_exists(username)  # 대소문자만 다른 이름 포함
        # 관리자 여부는 이름으로만 정하므로, 운영에서는 그 이름을 공개 가입으로 선점할 수 없게 한다
        # (관리자 계정은 서버에서 `scripts/reset_password.py --username <이름> --create`로 만든다).
        # 형식 검사 뒤에, 이미 있는 이름과 같은 조회를 거쳐 같은 응답으로 나간다 — 짧은 비밀번호를 보내
        # 422와 409로 관리자 이름을 가려내거나 응답 시간으로 구분할 수 없게 (2026-11-11 검토).
        reserved = (
            public and self._settings.is_production and username.lower() in self._settings.admin_username_set
        )
        if taken or reserved:
            raise VersionConflictError("이미 사용 중인 이름입니다.")
        now = datetime.now(UTC)
        return await self._repo.create_user(
            username,
            await hash_password_async(password),
            consented_at=now if consent else None,
            consent_version=self._settings.consent_version if consent else None,
            role=role,
            status=status,
        )

    @property
    def invite_required(self) -> bool:
        return bool(self._settings.register_invite_code.strip())

    def check_invite(self, invite_code: str | None, scope: str, client_ip: str | None) -> None:
        """초대 코드 확인 (소셜 로그인 시작이 쓴다). 틀리면 403 `invalid_invite`, 잦으면 429."""
        self._check_invite(invite_code, scope, client_ip)

    def _check_invite(self, invite_code: str | None, username: str, client_ip: str | None) -> None:
        expected = self._settings.register_invite_code.strip()
        if not expected:
            return
        name = username.strip().lower()
        ip = client_key(client_ip) if client_ip else None
        now = datetime.now(UTC)
        wait = self._invite_limits.retry_after(name, ip, now)
        if wait is not None:
            raise TooManyAttemptsError(
                f"가입 시도가 너무 많습니다. {wait}초 뒤에 다시 시도하세요.", retry_after=wait
            )
        if not tokens_equal(expected, (invite_code or "").strip()):
            self._invite_limits.record_failure(name, ip, now)  # 초대 코드를 하나씩 맞춰 보는 것을 막는다
            raise InvalidInviteError("초대 코드가 맞지 않습니다.")
        self._invite_limits.record_success(name, ip)

    # ---- 로그인·세션 ----

    async def login(self, username: str, password: str, client_ip: str | None = None) -> AuthResult:
        """이름(또는 이메일)·비밀번호 확인 뒤 새 세션. 쿠키 로그인과 Bearer 토큰 발급이 함께 쓴다.

        `username`에 `@`가 있으면 이메일로 찾는다. 실패 제한은 계정 기준이다 — 찾은 계정의 아이디(소문자)가
        키라서 아이디·이메일 어느 쪽으로 틀려도 같은 기록에 쌓인다. 계정이 없으면 소문자로 바꾼 입력값이 키다.
        그래서 잠금 확인보다 조회가 먼저다(계정이 있든 없든 조회 한 번 — 잠겨 있으면 해시는 계산하지 않는다).
        """
        username = username.strip()
        typed = username.lower()
        ip = client_key(client_ip) if client_ip else None
        now = datetime.now(UTC)
        if "@" in username:  # 아이디에는 @가 들어갈 수 없다
            found = await self._repo.find_user_by_email(typed)
        else:
            found = await self._repo.find_user_by_username(username)
        name = found[0].username.lower() if found is not None else typed
        wait = self._login_limits.retry_after(name, ip, now)
        if wait is not None:
            raise TooManyAttemptsError(
                f"로그인 시도가 너무 많습니다. {wait}초 뒤에 다시 시도하세요.", retry_after=wait
            )
        # 없는 이름·이메일, 비밀번호 없는(소셜 전용) 계정이어도 해시 한 번을 똑같이 계산한다 —
        # 응답 시간으로 가입 여부를 알아낼 수 없게.
        stored = found[1] if found is not None and found[1] else _DECOY_HASH
        password_ok = await verify_password_async(password, stored)
        if found is None or not found[1] or not password_ok:
            self._login_limits.record_failure(name, ip, now)
            raise UnauthorizedError("이름 또는 비밀번호가 맞지 않습니다.")
        self._login_limits.record_success(name, ip)
        self._require_active(found[0])  # 비밀번호를 맞힌 뒤에만 계정 상태를 알린다
        return await self._open_session(found[0])

    @staticmethod
    def _require_active(user: User) -> None:
        if user.status == "suspended":
            raise AccountSuspendedError("정지된 계정입니다. 관리자에게 문의하세요.")
        if user.status != "active":
            raise AccountPendingError("관리자 승인을 기다리는 계정입니다. 승인되면 로그인할 수 있습니다.")

    # ---- 소셜 로그인 ----

    async def login_with_identity(
        self, provider: str, profile: OAuthProfile, *, consent: bool = False, invite_ok: bool = False
    ) -> AuthResult:
        """소셜 로그인. 묶인 계정 → 확인된 이메일이 같은 계정에 묶기 → 새 계정 순 (모듈 머리말).

        `consent`·`invite_ok`는 로그인을 시작할 때 받은 값이고 **새 계정을 만들 때만** 쓴다.
        정지·승인 대기 계정은 세션 없이 `AccountSuspendedError`·`AccountPendingError`.
        """
        identity = await self._repo.find_identity(provider, profile.subject)
        if identity is not None:
            user = await self._repo.get_user(identity.user_id)
        else:
            user = await self._link_or_create(provider, profile, consent=consent, invite_ok=invite_ok)
        self._require_active(user)
        return await self._open_session(user)

    async def _link_or_create(
        self, provider: str, profile: OAuthProfile, *, consent: bool, invite_ok: bool
    ) -> User:
        # 제공자 구현이 이미 소문자로 다듬지만, 저장·비교 직전에 한 번 더 맞춘다.
        email = (profile.email or "").strip().lower()
        verified_email = email if email and profile.email_verified else None
        if verified_email is not None:
            found = await self._repo.find_user_by_email(verified_email)
            if found is not None:
                if not found[0].email_verified:
                    # 그 계정의 이메일은 가입할 때 적은 값일 뿐 확인된 적이 없다 — 자동으로 묶지 않는다.
                    raise EmailInUseError("이 이메일로 가입한 계정이 있습니다. 비밀번호로 로그인하세요.")
                return await self._attach(found[0], provider, profile, created=False)
        # 새 계정: 공개 가입과 같은 문을 지난다.
        self._require_signup_open()
        if self.invite_required and not invite_ok:
            raise InvalidInviteError("초대 코드가 필요합니다.")
        self._require_consent(consent)
        # 확인되지 않은 이메일은 계정의 이메일로 삼지 않는다.
        user = await self._create_with_generated_username(
            (verified_email or "").partition("@")[0] or provider,
            None,
            email=verified_email,
            email_verified=verified_email is not None,
            display_name=profile.name,
            consent=consent,
        )
        return await self._attach(user, provider, profile, created=True)

    async def _attach(self, user: User, provider: str, profile: OAuthProfile, *, created: bool) -> User:
        """소셜 계정을 사용자에 묶는다. 같은 로그인이 동시에 두 번 들어와 먼저 묶였으면 그쪽 사용자를 쓴다."""
        try:
            await self._repo.add_identity(user.id, provider, profile.subject)
        except VersionConflictError:
            if created:
                await self._repo.delete_user(user.id)  # 방금 만든 빈 계정을 남기지 않는다
            identity = await self._repo.find_identity(provider, profile.subject)
            if identity is None:  # 이 사용자가 같은 제공자의 다른 계정을 이미 묶었다
                raise
            return await self._repo.get_user(identity.user_id)
        return user

    async def providers_of(self, user: User) -> list[str]:
        """로그인 수단: 비밀번호가 있으면 `password`, 그리고 연결된 소셜 제공자."""
        linked = (await self._repo.list_providers([user.id])).get(user.id, [])
        return (["password"] if user.has_password else []) + linked

    async def change_password(self, session: Session, current_password: str, new_password: str) -> int:
        """현재 비밀번호 확인 → 새 해시 저장 → 다른 세션 전부 폐기. 폐기한 세션 수를 돌려준다."""
        if len(new_password) < MIN_PASSWORD:
            raise InvalidInputError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
        user = await self._repo.get_user(session.user_id)
        found = await self._repo.find_user_by_username(user.username)
        # 비밀번호 없는(소셜 전용) 계정도 같은 401이다 (해시 비용도 같게).
        stored = found[1] if found is not None and found[1] else _DECOY_HASH
        if not await verify_password_async(current_password, stored) or found is None or not found[1]:
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
        # 로그인 실패는 계정 기준 키(아이디 소문자)로 쌓인다 — 이메일로 틀린 기록도 이 한 번으로 지워진다.
        self._login_limits.forget_name(user.username.lower())
        return await self._repo.revoke_all_sessions(user.id, datetime.now(UTC))

    async def set_role(self, username: str, role: Role) -> User:
        """운영자용 (`app.tools.create_user --admin`): 그 이름의 권한을 바꾼다. 없는 이름은 NotFoundError."""
        found = await self._repo.find_user_by_username(username.strip())
        if found is None:
            raise NotFoundError("그 이름의 사용자가 없습니다 (대소문자를 구분합니다).")
        if found[0].role == role:
            return found[0]
        return await self._repo.update_user(found[0].id, role=role)

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
        """관리자인가 — 권한이 admin이거나 이름이 `ADMIN_USERNAMES`에 있다 (판정은 여기 한 곳)."""
        return user.role == "admin" or self._settings.is_admin_name(user.username)

    def consent_current(self, user: User) -> bool:
        """기록된 동의 버전이 지금 버전과 같은가 (동의한 적 없으면 False)."""
        return user.consent_version is not None and user.consent_version == self._settings.consent_version

    def prune_throttle(self, now: datetime | None = None) -> int:
        """창이 지난 로그인·초대 코드 실패 기록을 비운다 (주기 정리가 부른다). 비운 키 수."""
        now = now or datetime.now(UTC)
        return self._login_limits.prune_all(now) + self._invite_limits.prune_all(now)

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
