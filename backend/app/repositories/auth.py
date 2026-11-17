"""사용자·세션·소셜 계정 연결 저장소 (인메모리 / PostgreSQL).

이메일은 서비스가 소문자로 바꿔 넘긴다(DB의 CHECK가 한 번 더 막는다). 세션을 읽을 때는 그 사용자의 지금
상태(`Session.user_status`)를 함께 싣는다 — 정지·승인 대기 계정의 세션은 저장소를 나서는 순간 죽은 세션이다.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.core.errors import LastAdminError, MemberChangedError, NotFoundError, VersionConflictError
from app.domain.auth import Identity, Role, Session, User, UserStatus

USERNAME_TAKEN = "이미 사용 중인 이름입니다."
EMAIL_TAKEN = "이미 가입된 이메일입니다."
IDENTITY_TAKEN = "이미 다른 계정에 연결된 소셜 계정입니다."
LIKE_ESCAPE = "\\"
LAST_ADMIN = "마지막 관리자입니다. 먼저 다른 관리자를 지정하세요."
MEMBER_CHANGED = "그 사이 회원의 상태가 바뀌었습니다. 목록을 새로 읽은 뒤 다시 하세요."
# 관리자 수를 줄일 수 있는 일(권한·상태 변경, 계정 삭제)을 한 줄로 세우는 PostgreSQL 권고 잠금의 키.
# 트랜잭션이 끝나면 풀린다(`pg_advisory_xact_lock`). 값 자체에 뜻은 없다 — 이 용도로만 쓰는 고정 숫자다.
ADMIN_SET_LOCK_KEY = 0x70695F61646D  # "pi_adm"


def is_active_admin(status: str, role: str, username: str, admin_names: Collection[str]) -> bool:
    """**활성 관리자**인가 — 상태가 active이고, 권한이 admin이거나 이름이 `ADMIN_USERNAMES`에 있다.

    "관리자가 0명이 되지 않는다"는 규칙이 세는 대상이다(두 저장소가 함께 쓴다). `ADMIN_USERNAMES`에 이름만
    적혀 있고 계정이 없으면 세지 않는다(들어올 사람이 없다). 정지·승인 대기 계정도 세지 않는다(로그인할 수
    없다). 이름 비교는 `Settings.is_admin_name`과 같이 대소문자까지 본다.
    """
    return status == "active" and (role == "admin" or username in admin_names)


class AuthRepo(Protocol):
    async def create_user(
        self,
        username: str,
        password_hash: str | None,
        *,
        consented_at: datetime | None = None,
        consent_version: str | None = None,
        email: str | None = None,
        email_verified: bool = False,
        display_name: str | None = None,
        role: Role = "user",
        status: UserStatus = "active",
    ) -> User:
        """username·email이 이미 있으면 VersionConflictError(메시지는 `USERNAME_TAKEN`·`EMAIL_TAKEN`).

        동의 값을 주면 가입과 함께 기록한다. `password_hash=None`은 소셜 전용 계정이다.
        """
        ...

    async def get_user(self, user_id: UUID) -> User: ...

    async def find_user_by_username(self, username: str) -> tuple[User, str | None] | None:
        """(사용자, 비밀번호 해시 — 소셜 전용 계정은 None). 없으면 None. 대소문자를 구분한다."""
        ...

    async def find_user_by_email(self, email: str) -> tuple[User, str | None] | None:
        """소문자로 바꾼 이메일로 찾는다. (사용자, 비밀번호 해시). 없으면 None."""
        ...

    async def update_user(
        self,
        user_id: UUID,
        *,
        status: UserStatus | None = None,
        role: Role | None = None,
        expected_status: UserStatus | None = None,
        admin_names: Collection[str] | None = None,
    ) -> User:
        """상태·권한을 바꾸고 바뀐 사용자를 돌려준다. 없으면 NotFoundError.

        아래 두 검사와 변경은 **한 트랜잭션**에서 한다(읽고 나서 바꾸는 사이에 남이 끼어들지 못한다).
        - `expected_status`: 지금 상태가 이 값과 다르면 아무것도 바꾸지 않고 `MemberChangedError`
          (오래된 화면에서 누른 승인이 그 사이 걸린 정지를 풀지 않게).
        - `admin_names`(= `ADMIN_USERNAMES`의 이름들, 빈 목록도 된다): 이 변경으로 그 사용자가 활성 관리자
          (`is_active_admin`)가 아니게 되는데 **다른** 활성 관리자가 없으면 `LastAdminError`. None이면 이
          검사를 하지 않는다(서버에서 직접 돌리는 운영 도구용).
        """
        ...

    async def ensure_not_last_admin(self, user_id: UUID, admin_names: Collection[str]) -> None:
        """그 사용자가 하나뿐인 활성 관리자면 `LastAdminError`. 계정 삭제가 자료를 지우기 **전에** 부른다.

        미리 보는 검사일 뿐이다 — 마지막 보증은 `delete_user(admin_names=…)`가 한다.
        """
        ...

    async def touch_login(self, user_id: UUID, at: datetime) -> None:
        """마지막 로그인 시각."""
        ...

    async def list_users(
        self,
        *,
        query: str = "",
        status: UserStatus | None = None,
        role: Role | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[User]]:
        """(조건에 맞는 전체 수, 최근 가입 순 한 쪽).

        query는 이름·이메일·표시 이름 부분 일치(대소문자 무시).
        """
        ...

    async def count_by_status(self) -> dict[str, int]:
        """상태별 사용자 수 (없는 상태는 결과에 없다)."""
        ...

    async def find_identity(self, provider: str, subject: str) -> Identity | None: ...

    async def add_identity(self, user_id: UUID, provider: str, subject: str) -> Identity:
        """소셜 계정을 사용자에 묶는다. 이미 묶인 (provider, subject)거나 그 사용자가 같은 제공자의 다른
        계정을 이미 묶었으면 VersionConflictError."""
        ...

    async def list_providers(self, user_ids: list[UUID]) -> dict[UUID, list[str]]:
        """사용자별 연결된 제공자 이름 (묶은 순서). 연결이 없는 사용자는 결과에 없다."""
        ...

    async def username_exists(self, username: str) -> bool:
        """대소문자만 다른 이름까지 포함해 이미 있는지 (`Admin`으로 `admin` 권한을 얻는 가입을 막는다)."""
        ...

    async def record_consent(self, user_id: UUID, version: str, at: datetime) -> User:
        """약관·개인정보 안내 동의 시각과 버전을 기록하고 바뀐 사용자를 돌려준다."""
        ...

    async def create_session(self, session: Session) -> Session: ...

    async def get_session(self, token_hash: str) -> Session | None: ...

    async def list_active_sessions(self, user_id: UUID, now: datetime) -> list[Session]:
        """폐기되지 않았고 만료 전인 세션. 최근에 만든 것 먼저."""
        ...

    async def revoke_session(self, token_hash: str, at: datetime) -> None: ...

    async def update_password(self, user_id: UUID, password_hash: str) -> None: ...

    async def revoke_other_sessions(self, user_id: UUID, keep_token_hash: str, at: datetime) -> int:
        """이 사용자의 다른 활성 세션을 모두 폐기한다 (비밀번호 변경·전체 로그아웃). 폐기한 수."""
        ...

    async def revoke_all_sessions(self, user_id: UUID, at: datetime) -> int:
        """이 사용자의 세션을 전부 폐기한다 (운영자의 비밀번호 재설정). 폐기한 수."""
        ...

    async def delete_user(self, user_id: UUID, *, admin_names: Collection[str] | None = None) -> None:
        """사용자와 세션을 지운다.

        `admin_names`를 주면 검사와 삭제를 한 트랜잭션에서 한다: 그 사용자가 활성 관리자인데 다른 활성
        관리자가 없으면 지우지 않고 `LastAdminError` (두 관리자가 동시에 서로를 지워도 한 명은 남는다).
        """
        ...

    async def purge_sessions(self, before: datetime) -> int:
        """before 이전에 만료되거나 폐기된 세션 행을 지운다 (정리). 지운 수."""
        ...


class InMemoryAuthRepo:
    def __init__(self) -> None:
        self._users: dict[UUID, tuple[User, str | None]] = {}
        self._sessions: dict[str, Session] = {}
        self._identities: dict[tuple[str, str], Identity] = {}

    async def create_user(
        self,
        username: str,
        password_hash: str | None,
        *,
        consented_at: datetime | None = None,
        consent_version: str | None = None,
        email: str | None = None,
        email_verified: bool = False,
        display_name: str | None = None,
        role: Role = "user",
        status: UserStatus = "active",
    ) -> User:
        if any(u.username == username for u, _ in self._users.values()):
            raise VersionConflictError(USERNAME_TAKEN)
        if email is not None and any(u.email == email for u, _ in self._users.values()):
            raise VersionConflictError(EMAIL_TAKEN)
        user = User(
            id=uuid4(),
            username=username,
            created_at=datetime.now(UTC),
            consented_at=consented_at,
            consent_version=consent_version,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            role=role,
            status=status,
            has_password=password_hash is not None,
        )
        self._users[user.id] = (user, password_hash)
        return user

    async def get_user(self, user_id: UUID) -> User:
        found = self._users.get(user_id)
        if found is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        return found[0]

    async def find_user_by_username(self, username: str) -> tuple[User, str | None] | None:
        for user, pw in self._users.values():
            if user.username == username:
                return user, pw
        return None

    async def find_user_by_email(self, email: str) -> tuple[User, str | None] | None:
        for user, pw in self._users.values():
            if user.email is not None and user.email == email:
                return user, pw
        return None

    async def update_user(
        self,
        user_id: UUID,
        *,
        status: UserStatus | None = None,
        role: Role | None = None,
        expected_status: UserStatus | None = None,
        admin_names: Collection[str] | None = None,
    ) -> User:
        # 이 함수 안에는 await가 없다 — 검사와 변경 사이에 다른 코루틴이 끼어들 수 없다(이벤트 루프 하나).
        found = self._users.get(user_id)
        if found is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        if expected_status is not None and found[0].status != expected_status:
            raise MemberChangedError(MEMBER_CHANGED)
        changes = {k: v for k, v in (("status", status), ("role", role)) if v is not None}
        user = found[0].model_copy(update=changes)
        if admin_names is not None:
            was = is_active_admin(found[0].status, found[0].role, found[0].username, admin_names)
            stays = is_active_admin(user.status, user.role, user.username, admin_names)
            if was and not stays and not self._other_active_admin(user_id, admin_names):
                raise LastAdminError(LAST_ADMIN)
        self._users[user_id] = (user, found[1])
        return user

    def _other_active_admin(self, user_id: UUID, admin_names: Collection[str]) -> bool:
        return any(
            u.id != user_id and is_active_admin(u.status, u.role, u.username, admin_names)
            for u, _ in self._users.values()
        )

    async def ensure_not_last_admin(self, user_id: UUID, admin_names: Collection[str]) -> None:
        found = self._users.get(user_id)
        if found is None:
            return
        user = found[0]
        if is_active_admin(user.status, user.role, user.username, admin_names) and not (
            self._other_active_admin(user_id, admin_names)
        ):
            raise LastAdminError(LAST_ADMIN)

    async def touch_login(self, user_id: UUID, at: datetime) -> None:
        found = self._users.get(user_id)
        if found is not None:
            self._users[user_id] = (found[0].model_copy(update={"last_login_at": at}), found[1])

    async def list_users(
        self,
        *,
        query: str = "",
        status: UserStatus | None = None,
        role: Role | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[User]]:
        needle = query.strip().lower()
        users = [u for u, _ in self._users.values()]
        if status is not None:
            users = [u for u in users if u.status == status]
        if role is not None:
            users = [u for u in users if u.role == role]
        if needle:
            users = [
                u
                for u in users
                if any(needle in (v or "").lower() for v in (u.username, u.email, u.display_name))
            ]
        users.sort(key=lambda u: u.created_at, reverse=True)
        return len(users), users[offset : offset + limit]

    async def count_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for user, _ in self._users.values():
            out[user.status] = out.get(user.status, 0) + 1
        return out

    async def find_identity(self, provider: str, subject: str) -> Identity | None:
        return self._identities.get((provider, subject))

    async def add_identity(self, user_id: UUID, provider: str, subject: str) -> Identity:
        if (provider, subject) in self._identities or any(
            i.user_id == user_id and i.provider == provider for i in self._identities.values()
        ):
            raise VersionConflictError(IDENTITY_TAKEN)
        identity = Identity(
            provider=provider,  # type: ignore[arg-type]
            subject=subject,
            user_id=user_id,
            created_at=datetime.now(UTC),
        )
        self._identities[(provider, subject)] = identity
        return identity

    async def list_providers(self, user_ids: list[UUID]) -> dict[UUID, list[str]]:
        wanted = set(user_ids)
        out: dict[UUID, list[str]] = {}
        for identity in sorted(self._identities.values(), key=lambda i: i.created_at):
            if identity.user_id in wanted:
                out.setdefault(identity.user_id, []).append(identity.provider)
        return out

    async def username_exists(self, username: str) -> bool:
        wanted = username.lower()
        return any(u.username.lower() == wanted for u, _ in self._users.values())

    async def record_consent(self, user_id: UUID, version: str, at: datetime) -> User:
        found = self._users.get(user_id)
        if found is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        user = found[0].model_copy(update={"consented_at": at, "consent_version": version})
        self._users[user_id] = (user, found[1])
        return user

    async def create_session(self, session: Session) -> Session:
        self._sessions[session.token_hash] = session
        return session

    async def get_session(self, token_hash: str) -> Session | None:
        session = self._sessions.get(token_hash)
        if session is None:
            return None
        owner = self._users.get(session.user_id)
        if owner is None:
            return None
        return session.model_copy(update={"user_status": owner[0].status})

    async def list_active_sessions(self, user_id: UUID, now: datetime) -> list[Session]:
        active = [s for s in self._sessions.values() if s.user_id == user_id and s.is_active(now)]
        return sorted(active, key=lambda s: s.created_at, reverse=True)

    async def revoke_session(self, token_hash: str, at: datetime) -> None:
        s = self._sessions.get(token_hash)
        if s is not None and s.revoked_at is None:
            self._sessions[token_hash] = s.model_copy(update={"revoked_at": at})

    async def update_password(self, user_id: UUID, password_hash: str) -> None:
        user, _ = self._users[user_id]
        self._users[user_id] = (user.model_copy(update={"has_password": True}), password_hash)

    async def revoke_other_sessions(self, user_id: UUID, keep_token_hash: str, at: datetime) -> int:
        n = 0
        for key, s in list(self._sessions.items()):
            if s.user_id == user_id and key != keep_token_hash and s.revoked_at is None:
                self._sessions[key] = s.model_copy(update={"revoked_at": at})
                n += 1
        return n

    async def revoke_all_sessions(self, user_id: UUID, at: datetime) -> int:
        n = 0
        for key, s in list(self._sessions.items()):
            if s.user_id == user_id and s.revoked_at is None:
                self._sessions[key] = s.model_copy(update={"revoked_at": at})
                n += 1
        return n

    async def delete_user(self, user_id: UUID, *, admin_names: Collection[str] | None = None) -> None:
        if admin_names is not None:  # 검사와 삭제 사이에 await가 없다 (위 update_user와 같은 이유로 원자적)
            await self.ensure_not_last_admin(user_id, admin_names)
        self._users.pop(user_id, None)
        for key in [k for k, s in self._sessions.items() if s.user_id == user_id]:
            del self._sessions[key]
        for ikey in [k for k, i in self._identities.items() if i.user_id == user_id]:
            del self._identities[ikey]

    async def purge_sessions(self, before: datetime) -> int:
        gone = [
            k
            for k, s in self._sessions.items()
            if s.expires_at < before or (s.revoked_at is not None and s.revoked_at < before)
        ]
        for k in gone:
            del self._sessions[k]
        return len(gone)


def _user_from_row(row: Any) -> User:
    return User(
        id=row.id,
        username=row.username,
        created_at=row.created_at,
        consented_at=row.consented_at,
        consent_version=row.consent_version,
        email=row.email,
        email_verified=bool(row.email_verified),
        display_name=row.display_name,
        role=row.role,
        status=row.status,
        last_login_at=row.last_login_at,
        has_password=row.password_hash is not None,
    )


def _session_from_row(row: Any, user_status: str = "active") -> Session:
    return Session(
        token_hash=row.token_hash,
        user_id=row.user_id,
        csrf_token=row.csrf_token,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        user_status=user_status,  # type: ignore[arg-type]
    )


def like_pattern(needle: str) -> str:
    """부분 일치 패턴. `%`·`_`·역슬래시는 글자 그대로 찾는다 (검색어가 와일드카드로 쓰이지 않게)."""
    escaped = needle
    for ch in (LIKE_ESCAPE, "%", "_"):
        escaped = escaped.replace(ch, LIKE_ESCAPE + ch)
    return f"%{escaped}%"


class PostgresAuthRepo:
    def __init__(self, sessions) -> None:  # noqa: ANN001 - SessionFactory (db extra)
        self._sessions = sessions

    async def create_user(
        self,
        username: str,
        password_hash: str | None,
        *,
        consented_at: datetime | None = None,
        consent_version: str | None = None,
        email: str | None = None,
        email_verified: bool = False,
        display_name: str | None = None,
        role: Role = "user",
        status: UserStatus = "active",
    ) -> User:
        from sqlalchemy.exc import IntegrityError

        from app.db.models import UserRow

        row = UserRow(
            id=uuid4(),
            username=username,
            password_hash=password_hash,
            created_at=datetime.now(UTC),
            consented_at=consented_at,
            consent_version=consent_version,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            role=role,
            status=status,
            last_login_at=None,
        )
        try:
            async with self._sessions.begin() as s:
                s.add(row)
        except IntegrityError as exc:
            # 어느 제약에 걸렸는지는 제약 이름으로만 가른다 (예외 문장을 밖으로 내보내지 않는다).
            reason = str(exc.orig)
            if "ck_users_" in reason:  # 소문자가 아닌 이메일·모르는 상태 값 — 중복이 아니라 호출 쪽 잘못이다
                raise
            taken = EMAIL_TAKEN if "uq_users_email" in reason else USERNAME_TAKEN
            raise VersionConflictError(taken) from exc
        return _user_from_row(row)

    async def get_user(self, user_id: UUID) -> User:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.id == user_id))
        if row is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        return _user_from_row(row)

    async def find_user_by_username(self, username: str) -> tuple[User, str | None] | None:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.username == username))
        if row is None:
            return None
        return _user_from_row(row), row.password_hash

    async def find_user_by_email(self, email: str) -> tuple[User, str | None] | None:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.email == email))
        if row is None:
            return None
        return _user_from_row(row), row.password_hash

    async def update_user(
        self,
        user_id: UUID,
        *,
        status: UserStatus | None = None,
        role: Role | None = None,
        expected_status: UserStatus | None = None,
        admin_names: Collection[str] | None = None,
    ) -> User:
        from sqlalchemy import select

        from app.db.models import UserRow

        # 한 트랜잭션: (관리자 집합 잠금) → 대상 행 잠금(FOR UPDATE) → 기대 상태·마지막 관리자 검사 → 변경.
        # 검사에서 예외가 나면 트랜잭션이 되돌려지고 잠금도 풀린다 — 아무것도 바뀌지 않는다.
        async with self._sessions.begin() as s:
            if admin_names is not None:
                await self._lock_admin_set(s)
            row = await s.scalar(select(UserRow).where(UserRow.id == user_id).with_for_update())
            if row is None:
                raise NotFoundError("사용자를 찾을 수 없습니다.")
            if expected_status is not None and row.status != expected_status:
                raise MemberChangedError(MEMBER_CHANGED)
            new_status, new_role = status or row.status, role or row.role
            if admin_names is not None:
                was = is_active_admin(row.status, row.role, row.username, admin_names)
                stays = is_active_admin(new_status, new_role, row.username, admin_names)
                if was and not stays and not await self._other_active_admin(s, user_id, admin_names):
                    raise LastAdminError(LAST_ADMIN)
            row.status, row.role = new_status, new_role  # 같은 값이면 UPDATE가 나가지 않는다
        return _user_from_row(row)

    @staticmethod
    async def _lock_admin_set(s) -> None:  # noqa: ANN001 - AsyncSession (db extra)
        """관리자 수를 줄일 수 있는 일을 한 줄로 세운다 (트랜잭션이 끝날 때까지).

        잠금 없이는 READ COMMITTED에서 두 관리자가 동시에 서로를 내릴 때 둘 다 "다른 관리자가 있다"를 보고
        통과한다. 잠금을 얻은 **다음** 문장은 앞선 트랜잭션이 커밋한 결과를 본다.
        """
        from sqlalchemy import func, select

        await s.execute(select(func.pg_advisory_xact_lock(ADMIN_SET_LOCK_KEY)))

    @staticmethod
    async def _other_active_admin(s, user_id: UUID, admin_names: Collection[str]) -> bool:  # noqa: ANN001
        """`user_id`가 아닌 활성 관리자가 하나라도 있는가 (`is_active_admin`과 같은 조건의 SQL)."""
        from sqlalchemy import or_, select

        from app.db.models import UserRow

        found = await s.scalar(
            select(UserRow.id)
            .where(
                UserRow.id != user_id,
                UserRow.status == "active",
                or_(UserRow.role == "admin", UserRow.username.in_(list(admin_names))),
            )
            .limit(1)
        )
        return found is not None

    async def _refuse_last_admin(self, s, user_id: UUID, admin_names: Collection[str]) -> None:  # noqa: ANN001
        """잠금 → 대상이 활성 관리자이고 다른 활성 관리자가 없으면 `LastAdminError`. 대상이 없으면 통과."""
        from sqlalchemy import select

        from app.db.models import UserRow

        await self._lock_admin_set(s)
        target = (
            await s.execute(
                select(UserRow.status, UserRow.role, UserRow.username).where(UserRow.id == user_id)
            )
        ).first()
        if target is None or not is_active_admin(target.status, target.role, target.username, admin_names):
            return
        if not await self._other_active_admin(s, user_id, admin_names):
            raise LastAdminError(LAST_ADMIN)

    async def ensure_not_last_admin(self, user_id: UUID, admin_names: Collection[str]) -> None:
        async with self._sessions.begin() as s:
            await self._refuse_last_admin(s, user_id, admin_names)

    async def touch_login(self, user_id: UUID, at: datetime) -> None:
        from sqlalchemy import update

        from app.db.models import UserRow

        async with self._sessions.begin() as s:
            await s.execute(update(UserRow).where(UserRow.id == user_id).values(last_login_at=at))

    async def list_users(
        self,
        *,
        query: str = "",
        status: UserStatus | None = None,
        role: Role | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[User]]:
        from sqlalchemy import func, or_, select

        from app.db.models import UserRow

        conditions = []
        if status is not None:
            conditions.append(UserRow.status == status)
        if role is not None:
            conditions.append(UserRow.role == role)
        needle = query.strip().lower()
        if needle:
            pattern = like_pattern(needle)
            conditions.append(
                or_(
                    func.lower(UserRow.username).like(pattern, escape=LIKE_ESCAPE),
                    UserRow.email.like(pattern, escape=LIKE_ESCAPE),  # 이메일은 이미 소문자다
                    func.lower(UserRow.display_name).like(pattern, escape=LIKE_ESCAPE),
                )
            )
        async with self._sessions() as s:
            total = await s.scalar(select(func.count()).select_from(UserRow).where(*conditions))
            rows = await s.scalars(
                select(UserRow)
                .where(*conditions)
                .order_by(UserRow.created_at.desc(), UserRow.id)
                .limit(limit)
                .offset(offset)
            )
            return int(total or 0), [_user_from_row(row) for row in rows]

    async def count_by_status(self) -> dict[str, int]:
        from sqlalchemy import func, select

        from app.db.models import UserRow

        async with self._sessions() as s:
            rows = await s.execute(select(UserRow.status, func.count()).group_by(UserRow.status))
            return {str(status): int(n) for status, n in rows}

    async def find_identity(self, provider: str, subject: str) -> Identity | None:
        from sqlalchemy import select

        from app.db.models import UserIdentityRow

        async with self._sessions() as s:
            row = await s.scalar(
                select(UserIdentityRow).where(
                    UserIdentityRow.provider == provider, UserIdentityRow.subject == subject
                )
            )
        if row is None:
            return None
        return Identity(
            provider=row.provider, subject=row.subject, user_id=row.user_id, created_at=row.created_at
        )

    async def add_identity(self, user_id: UUID, provider: str, subject: str) -> Identity:
        from sqlalchemy.exc import IntegrityError

        from app.db.models import UserIdentityRow

        now = datetime.now(UTC)
        try:
            async with self._sessions.begin() as s:
                s.add(
                    UserIdentityRow(
                        id=uuid4(), provider=provider, subject=subject, user_id=user_id, created_at=now
                    )
                )
        except IntegrityError as exc:
            raise VersionConflictError(IDENTITY_TAKEN) from exc
        return Identity(
            provider=provider,  # type: ignore[arg-type]
            subject=subject,
            user_id=user_id,
            created_at=now,
        )

    async def list_providers(self, user_ids: list[UUID]) -> dict[UUID, list[str]]:
        from sqlalchemy import select

        from app.db.models import UserIdentityRow

        if not user_ids:
            return {}
        out: dict[UUID, list[str]] = {}
        async with self._sessions() as s:
            rows = await s.execute(
                select(UserIdentityRow.user_id, UserIdentityRow.provider)
                .where(UserIdentityRow.user_id.in_(user_ids))
                .order_by(UserIdentityRow.created_at, UserIdentityRow.provider)
            )
            for user_id, provider in rows:
                out.setdefault(user_id, []).append(provider)
        return out

    async def username_exists(self, username: str) -> bool:
        from sqlalchemy import func, select

        from app.db.models import UserRow

        async with self._sessions() as s:
            found = await s.scalar(
                select(UserRow.id).where(func.lower(UserRow.username) == username.lower()).limit(1)
            )
        return found is not None

    async def record_consent(self, user_id: UUID, version: str, at: datetime) -> User:
        from sqlalchemy import select, update

        from app.db.models import UserRow

        async with self._sessions.begin() as s:
            await s.execute(
                update(UserRow).where(UserRow.id == user_id).values(consented_at=at, consent_version=version)
            )
            row = await s.scalar(select(UserRow).where(UserRow.id == user_id))
        if row is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        return _user_from_row(row)

    async def create_session(self, session: Session) -> Session:
        from app.db.models import SessionRow

        async with self._sessions.begin() as s:
            s.add(
                SessionRow(
                    token_hash=session.token_hash,
                    user_id=session.user_id,
                    csrf_token=session.csrf_token,
                    created_at=session.created_at,
                    expires_at=session.expires_at,
                    revoked_at=session.revoked_at,
                )
            )
        return session

    async def get_session(self, token_hash: str) -> Session | None:
        from sqlalchemy import select

        from app.db.models import SessionRow, UserRow

        # 사용자 상태를 같은 질의로 읽는다 — 요청마다 질의 하나로 "세션이 살아 있고 계정이 active인가"를 본다.
        async with self._sessions() as s:
            found = (
                await s.execute(
                    select(SessionRow, UserRow.status)
                    .join(UserRow, UserRow.id == SessionRow.user_id)
                    .where(SessionRow.token_hash == token_hash)
                )
            ).first()
        return None if found is None else _session_from_row(found[0], found[1])

    async def list_active_sessions(self, user_id: UUID, now: datetime) -> list[Session]:
        from sqlalchemy import select

        from app.db.models import SessionRow

        async with self._sessions() as s:
            rows = await s.scalars(
                select(SessionRow)
                .where(
                    SessionRow.user_id == user_id,
                    SessionRow.revoked_at.is_(None),
                    SessionRow.expires_at > now,
                )
                .order_by(SessionRow.created_at.desc())
            )
            return [_session_from_row(row) for row in rows]

    async def revoke_session(self, token_hash: str, at: datetime) -> None:
        from sqlalchemy import update

        from app.db.models import SessionRow

        async with self._sessions.begin() as s:
            await s.execute(
                update(SessionRow)
                .where(SessionRow.token_hash == token_hash, SessionRow.revoked_at.is_(None))
                .values(revoked_at=at)
            )

    async def update_password(self, user_id: UUID, password_hash: str) -> None:
        from sqlalchemy import update

        from app.db.models import UserRow

        async with self._sessions.begin() as s:
            await s.execute(update(UserRow).where(UserRow.id == user_id).values(password_hash=password_hash))

    async def revoke_other_sessions(self, user_id: UUID, keep_token_hash: str, at: datetime) -> int:
        from sqlalchemy import update

        from app.db.models import SessionRow

        async with self._sessions.begin() as s:
            result = await s.execute(
                update(SessionRow)
                .where(
                    SessionRow.user_id == user_id,
                    SessionRow.token_hash != keep_token_hash,
                    SessionRow.revoked_at.is_(None),
                )
                .values(revoked_at=at)
            )
        return int(result.rowcount or 0)

    async def revoke_all_sessions(self, user_id: UUID, at: datetime) -> int:
        from sqlalchemy import update

        from app.db.models import SessionRow

        async with self._sessions.begin() as s:
            result = await s.execute(
                update(SessionRow)
                .where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
                .values(revoked_at=at)
            )
        return int(result.rowcount or 0)

    async def delete_user(self, user_id: UUID, *, admin_names: Collection[str] | None = None) -> None:
        from sqlalchemy import delete

        from app.db.models import UserRow

        async with self._sessions.begin() as s:
            if admin_names is not None:  # 검사와 삭제가 한 트랜잭션 — 검사에 걸리면 지우지 않는다
                await self._refuse_last_admin(s, user_id, admin_names)
            await s.execute(delete(UserRow).where(UserRow.id == user_id))  # sessions는 CASCADE

    async def purge_sessions(self, before: datetime) -> int:
        from sqlalchemy import delete, or_

        from app.db.models import SessionRow

        async with self._sessions.begin() as s:
            result = await s.execute(
                delete(SessionRow).where(
                    or_(
                        SessionRow.expires_at < before,
                        SessionRow.revoked_at.is_not(None) & (SessionRow.revoked_at < before),
                    )
                )
            )
        return int(result.rowcount or 0)
