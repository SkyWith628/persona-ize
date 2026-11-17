"""사용자·세션·소셜 계정 연결 저장소 (인메모리 / PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.core.errors import NotFoundError, VersionConflictError
from app.domain.auth import Identity, Role, Session, User, UserStatus

USERNAME_TAKEN = "이미 사용 중인 이름입니다."
EMAIL_TAKEN = "이미 가입된 이메일입니다."


class AuthRepo(Protocol):
    async def create_user(
        self,
        username: str,
        password_hash: str | None,
        *,
        consented_at: datetime | None = None,
        consent_version: str | None = None,
        email: str | None = None,
        display_name: str | None = None,
        status: UserStatus = "active",
    ) -> User:
        """username이나 email이 이미 있으면 VersionConflictError(메시지로 구분). email은 소문자로 넘긴다.

        동의 값을 주면 가입과 함께 기록한다. 소셜 전용 계정은 password_hash가 None이다.
        """
        ...

    async def get_user(self, user_id: UUID) -> User: ...

    async def find_user_by_username(self, username: str) -> tuple[User, str | None] | None:
        """(사용자, 비밀번호 해시). 없으면 None. 대소문자를 구분한다. 소셜 전용 계정은 해시가 None."""
        ...

    async def find_user_by_email(self, email: str) -> tuple[User, str | None] | None:
        """소문자 이메일로 찾는다. (사용자, 비밀번호 해시) 또는 None."""
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

    async def delete_user(self, user_id: UUID) -> None:
        """사용자와 세션을 지운다."""
        ...

    async def purge_sessions(self, before: datetime) -> int:
        """before 이전에 만료되거나 폐기된 세션 행을 지운다 (정리). 지운 수."""
        ...

    async def touch_login(self, user_id: UUID, at: datetime) -> None:
        """마지막 로그인 시각을 적는다."""
        ...

    async def update_user(
        self, user_id: UUID, *, status: UserStatus | None = None, role: Role | None = None
    ) -> User:
        """준 값만 바꾼다. 없으면 NotFoundError."""
        ...

    async def find_identity(self, provider: str, subject: str) -> Identity | None: ...

    async def add_identity(self, user_id: UUID, provider: str, subject: str, email: str | None) -> Identity:
        """(provider, subject)가 이미 묶여 있으면 VersionConflictError."""
        ...

    async def list_providers(self, user_ids: list[UUID]) -> dict[UUID, list[str]]:
        """사용자별로 연결된 소셜 제공자 이름(정렬). 연결이 없는 사용자는 결과에 없다."""
        ...

    async def list_users(
        self, *, query: str = "", status: UserStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[int, list[User]]:
        """(조건에 맞는 전체 수, 최근 가입 순 한 쪽). query는 아이디·이메일·표시 이름 부분 일치."""
        ...


LIKE_ESCAPE = "\\"


def like_pattern(query: str) -> str:
    """ILIKE 패턴. 사용자가 넣은 %, _는 글자 그대로 찾는다 (`escape=LIKE_ESCAPE`와 함께 쓴다)."""
    escaped = query
    for ch in (LIKE_ESCAPE, "%", "_"):
        escaped = escaped.replace(ch, LIKE_ESCAPE + ch)
    return f"%{escaped}%"


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
        display_name: str | None = None,
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
            display_name=display_name,
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
        return self._sessions.get(token_hash)

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

    async def delete_user(self, user_id: UUID) -> None:
        self._users.pop(user_id, None)
        for key in [k for k, s in self._sessions.items() if s.user_id == user_id]:
            del self._sessions[key]
        for ident in [k for k, i in self._identities.items() if i.user_id == user_id]:
            del self._identities[ident]

    async def purge_sessions(self, before: datetime) -> int:
        gone = [
            k
            for k, s in self._sessions.items()
            if s.expires_at < before or (s.revoked_at is not None and s.revoked_at < before)
        ]
        for k in gone:
            del self._sessions[k]
        return len(gone)

    async def touch_login(self, user_id: UUID, at: datetime) -> None:
        found = self._users.get(user_id)
        if found is not None:
            self._users[user_id] = (found[0].model_copy(update={"last_login_at": at}), found[1])

    async def update_user(
        self, user_id: UUID, *, status: UserStatus | None = None, role: Role | None = None
    ) -> User:
        user = await self.get_user(user_id)
        changes = {k: v for k, v in (("status", status), ("role", role)) if v is not None}
        user = user.model_copy(update=changes)
        self._users[user_id] = (user, self._users[user_id][1])
        return user

    async def find_identity(self, provider: str, subject: str) -> Identity | None:
        return self._identities.get((provider, subject))

    async def add_identity(self, user_id: UUID, provider: str, subject: str, email: str | None) -> Identity:
        if (provider, subject) in self._identities:
            raise VersionConflictError("이미 연결된 소셜 계정입니다.")
        ident = Identity(
            provider=provider,  # type: ignore[arg-type]
            subject=subject,
            user_id=user_id,
            email=email,
            created_at=datetime.now(UTC),
        )
        self._identities[(provider, subject)] = ident
        return ident

    async def list_providers(self, user_ids: list[UUID]) -> dict[UUID, list[str]]:
        wanted = set(user_ids)
        out: dict[UUID, set[str]] = {}
        for ident in self._identities.values():
            if ident.user_id in wanted:
                out.setdefault(ident.user_id, set()).add(ident.provider)
        return {uid: sorted(names) for uid, names in out.items()}

    async def list_users(
        self, *, query: str = "", status: UserStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[int, list[User]]:
        needle = query.strip().lower()

        def matches(u: User) -> bool:
            if status is not None and u.status != status:
                return False
            fields = (u.username, u.email or "", u.display_name or "")
            return not needle or any(needle in f.lower() for f in fields)

        found = sorted(
            (u for u, _ in self._users.values() if matches(u)), key=lambda u: u.created_at, reverse=True
        )
        return len(found), found[offset : offset + limit]


def _user_from_row(row: Any) -> User:
    return User(
        id=row.id,
        username=row.username,
        created_at=row.created_at,
        consented_at=row.consented_at,
        consent_version=row.consent_version,
        email=row.email,
        display_name=row.display_name,
        role=row.role,
        status=row.status,
        last_login_at=row.last_login_at,
        has_password=row.password_hash is not None,
    )


def _identity_from_row(row: Any) -> Identity:
    return Identity(
        provider=row.provider,
        subject=row.subject,
        user_id=row.user_id,
        email=row.email,
        created_at=row.created_at,
    )


def _session_from_row(row: Any) -> Session:
    return Session(
        token_hash=row.token_hash,
        user_id=row.user_id,
        csrf_token=row.csrf_token,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


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
        display_name: str | None = None,
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
            display_name=display_name,
            role="user",
            status=status,
        )
        try:
            async with self._sessions.begin() as s:
                s.add(row)
        except IntegrityError as exc:
            taken = EMAIL_TAKEN if "uq_users_email" in str(exc.orig) else USERNAME_TAKEN
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

        from app.db.models import SessionRow

        async with self._sessions() as s:
            row = await s.scalar(select(SessionRow).where(SessionRow.token_hash == token_hash))
        return None if row is None else _session_from_row(row)

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

    async def delete_user(self, user_id: UUID) -> None:
        from sqlalchemy import delete

        from app.db.models import UserRow

        async with self._sessions.begin() as s:
            await s.execute(delete(UserRow).where(UserRow.id == user_id))  # 세션·소셜 연결은 CASCADE

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

    async def touch_login(self, user_id: UUID, at: datetime) -> None:
        from sqlalchemy import update

        from app.db.models import UserRow

        async with self._sessions.begin() as s:
            await s.execute(update(UserRow).where(UserRow.id == user_id).values(last_login_at=at))

    async def update_user(
        self, user_id: UUID, *, status: UserStatus | None = None, role: Role | None = None
    ) -> User:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions.begin() as s:
            row = await s.scalar(select(UserRow).where(UserRow.id == user_id).with_for_update())
            if row is None:
                raise NotFoundError("사용자를 찾을 수 없습니다.")
            if status is not None:
                row.status = status
            if role is not None:
                row.role = role
            await s.flush()
            return _user_from_row(row)

    async def find_identity(self, provider: str, subject: str) -> Identity | None:
        from sqlalchemy import select

        from app.db.models import UserIdentityRow

        async with self._sessions() as s:
            row = await s.scalar(
                select(UserIdentityRow).where(
                    UserIdentityRow.provider == provider, UserIdentityRow.subject == subject
                )
            )
        return _identity_from_row(row) if row is not None else None

    async def add_identity(self, user_id: UUID, provider: str, subject: str, email: str | None) -> Identity:
        from sqlalchemy.exc import IntegrityError

        from app.db.models import UserIdentityRow

        row = UserIdentityRow(
            id=uuid4(),
            provider=provider,
            subject=subject,
            user_id=user_id,
            email=email,
            created_at=datetime.now(UTC),
        )
        try:
            async with self._sessions.begin() as s:
                s.add(row)
        except IntegrityError as exc:
            raise VersionConflictError("이미 연결된 소셜 계정입니다.") from exc
        return _identity_from_row(row)

    async def list_providers(self, user_ids: list[UUID]) -> dict[UUID, list[str]]:
        from sqlalchemy import select

        from app.db.models import UserIdentityRow

        if not user_ids:
            return {}
        async with self._sessions() as s:
            rows = await s.execute(
                select(UserIdentityRow.user_id, UserIdentityRow.provider)
                .where(UserIdentityRow.user_id.in_(user_ids))
                .distinct()
            )
            out: dict[UUID, list[str]] = {}
            for user_id, provider in rows:
                out.setdefault(user_id, []).append(provider)
        return {uid: sorted(names) for uid, names in out.items()}

    async def list_users(
        self, *, query: str = "", status: UserStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[int, list[User]]:
        from sqlalchemy import func, or_, select

        from app.db.models import UserRow

        conditions = []
        if status is not None:
            conditions.append(UserRow.status == status)
        if query.strip():
            pattern = like_pattern(query.strip())
            conditions.append(
                or_(
                    UserRow.username.ilike(pattern, escape=LIKE_ESCAPE),
                    UserRow.email.ilike(pattern, escape=LIKE_ESCAPE),
                    UserRow.display_name.ilike(pattern, escape=LIKE_ESCAPE),
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
            users = [_user_from_row(r) for r in rows]
        return int(total or 0), users
