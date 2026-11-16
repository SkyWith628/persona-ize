"""사용자·세션·소셜 계정 연결·관리자 기록 저장소 (인메모리 / PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.core.errors import NotFoundError, VersionConflictError
from app.domain.auth import AuditEntry, Identity, Role, Session, User, UserCounts, UserStatus

USERNAME_TAKEN = "이미 사용 중인 이름입니다."
EMAIL_TAKEN = "이미 가입된 이메일입니다."


class AuthRepo(Protocol):
    async def create_user(
        self,
        username: str,
        password_hash: str | None,
        *,
        email: str | None = None,
        display_name: str | None = None,
        role: Role = "user",
        status: UserStatus = "active",
    ) -> User:
        """username이나 email이 이미 있으면 VersionConflictError. email은 소문자로 넘긴다."""
        ...

    async def get_user(self, user_id: UUID) -> User: ...

    async def find_user_by_username(self, username: str) -> tuple[User, str | None] | None:
        """(사용자, 비밀번호 해시). 없으면 None. 소셜 전용 계정은 해시가 None."""
        ...

    async def find_user_by_email(self, email: str) -> tuple[User, str | None] | None:
        """소문자 이메일로 찾는다. (사용자, 비밀번호 해시) 또는 None."""
        ...

    async def create_session(self, session: Session) -> Session: ...

    async def get_session(self, token_hash: str) -> Session | None: ...

    async def revoke_session(self, token_hash: str, at: datetime) -> None: ...

    async def update_password(self, user_id: UUID, password_hash: str) -> None: ...

    async def revoke_other_sessions(self, user_id: UUID, keep_token_hash: str, at: datetime) -> int:
        """이 사용자의 다른 활성 세션을 모두 폐기한다 (비밀번호 변경 뒤). 폐기한 수."""
        ...

    async def revoke_user_sessions(self, user_id: UUID, at: datetime) -> int:
        """이 사용자의 활성 세션을 전부 폐기한다 (정지·관리자 강제 로그아웃). 폐기한 수."""
        ...

    async def delete_user(self, user_id: UUID) -> None:
        """사용자와 세션·소셜 연결을 지운다."""
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
        self,
        *,
        query: str = "",
        status: UserStatus | None = None,
        role: Role | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[User]]:
        """(조건에 맞는 전체 수, 최신 가입순 한 쪽). query는 아이디·이메일·표시 이름 부분 일치."""
        ...

    async def count_users(self, since: datetime) -> UserCounts:
        """전체·대기·정지 수와 since 이후 가입 수."""
        ...

    async def add_audit(self, entry: AuditEntry) -> AuditEntry: ...

    async def list_audit(self, limit: int) -> list[AuditEntry]:
        """최근 순."""
        ...


def _like_pattern(query: str) -> str:
    """ILIKE 패턴. 사용자가 넣은 %, _는 글자 그대로 찾는다."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class InMemoryAuthRepo:
    def __init__(self) -> None:
        self._users: dict[UUID, tuple[User, str | None]] = {}
        self._sessions: dict[str, Session] = {}
        self._identities: dict[tuple[str, str], Identity] = {}
        self._audit: list[AuditEntry] = []

    async def create_user(
        self,
        username: str,
        password_hash: str | None,
        *,
        email: str | None = None,
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
            email=email,
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

    async def create_session(self, session: Session) -> Session:
        self._sessions[session.token_hash] = session
        return session

    async def get_session(self, token_hash: str) -> Session | None:
        return self._sessions.get(token_hash)

    async def revoke_session(self, token_hash: str, at: datetime) -> None:
        s = self._sessions.get(token_hash)
        if s is not None:
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

    async def revoke_user_sessions(self, user_id: UUID, at: datetime) -> int:
        return await self.revoke_other_sessions(user_id, "", at)

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
        self,
        *,
        query: str = "",
        status: UserStatus | None = None,
        role: Role | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[User]]:
        needle = query.strip().lower()

        def matches(u: User) -> bool:
            if status is not None and u.status != status:
                return False
            if role is not None and u.role != role:
                return False
            fields = (u.username, u.email or "", u.display_name or "")
            return not needle or any(needle in f.lower() for f in fields)

        found = sorted(
            (u for u, _ in self._users.values() if matches(u)), key=lambda u: u.created_at, reverse=True
        )
        return len(found), found[offset : offset + limit]

    async def count_users(self, since: datetime) -> UserCounts:
        users = [u for u, _ in self._users.values()]
        return UserCounts(
            total=len(users),
            pending=sum(1 for u in users if u.status == "pending"),
            suspended=sum(1 for u in users if u.status == "suspended"),
            new_in_range=sum(1 for u in users if u.created_at >= since),
        )

    async def add_audit(self, entry: AuditEntry) -> AuditEntry:
        saved = entry.model_copy(update={"id": len(self._audit) + 1})
        self._audit.append(saved)
        return saved

    async def list_audit(self, limit: int) -> list[AuditEntry]:
        return list(reversed(self._audit))[:limit]


def _user(row) -> User:  # noqa: ANN001 - UserRow (db extra)
    return User(
        id=row.id,
        username=row.username,
        created_at=row.created_at,
        email=row.email,
        display_name=row.display_name,
        role=row.role,
        status=row.status,
        last_login_at=row.last_login_at,
        has_password=row.password_hash is not None,
    )


def _identity(row) -> Identity:  # noqa: ANN001 - UserIdentityRow
    return Identity(
        provider=row.provider,
        subject=row.subject,
        user_id=row.user_id,
        email=row.email,
        created_at=row.created_at,
    )


class PostgresAuthRepo:
    def __init__(self, sessions) -> None:  # noqa: ANN001 - SessionFactory (db extra)
        self._sessions = sessions

    async def create_user(
        self,
        username: str,
        password_hash: str | None,
        *,
        email: str | None = None,
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
            email=email,
            display_name=display_name,
            role=role,
            status=status,
        )
        try:
            async with self._sessions.begin() as s:
                s.add(row)
        except IntegrityError as exc:
            taken = EMAIL_TAKEN if "uq_users_email" in str(exc.orig) else USERNAME_TAKEN
            raise VersionConflictError(taken) from exc
        return _user(row)

    async def get_user(self, user_id: UUID) -> User:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.id == user_id))
        if row is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        return _user(row)

    async def find_user_by_username(self, username: str) -> tuple[User, str | None] | None:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.username == username))
        if row is None:
            return None
        return _user(row), row.password_hash

    async def find_user_by_email(self, email: str) -> tuple[User, str | None] | None:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.email == email))
        if row is None:
            return None
        return _user(row), row.password_hash

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
        if row is None:
            return None
        return Session(
            token_hash=row.token_hash,
            user_id=row.user_id,
            csrf_token=row.csrf_token,
            created_at=row.created_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )

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

    async def revoke_user_sessions(self, user_id: UUID, at: datetime) -> int:
        return await self.revoke_other_sessions(user_id, "", at)

    async def delete_user(self, user_id: UUID) -> None:
        from sqlalchemy import delete

        from app.db.models import UserRow

        async with self._sessions.begin() as s:
            # sessions·user_identities는 CASCADE
            await s.execute(delete(UserRow).where(UserRow.id == user_id))

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
        return _user(row)

    async def find_identity(self, provider: str, subject: str) -> Identity | None:
        from sqlalchemy import select

        from app.db.models import UserIdentityRow

        async with self._sessions() as s:
            row = await s.scalar(
                select(UserIdentityRow).where(
                    UserIdentityRow.provider == provider, UserIdentityRow.subject == subject
                )
            )
        return _identity(row) if row is not None else None

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
        return _identity(row)

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
        if query.strip():
            pattern = _like_pattern(query.strip())
            conditions.append(
                or_(
                    UserRow.username.ilike(pattern, escape="\\"),
                    UserRow.email.ilike(pattern, escape="\\"),
                    UserRow.display_name.ilike(pattern, escape="\\"),
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
            users = [_user(r) for r in rows]
        return int(total or 0), users

    async def count_users(self, since: datetime) -> UserCounts:
        from sqlalchemy import func, select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = (
                await s.execute(
                    select(
                        func.count(),
                        func.count().filter(UserRow.status == "pending"),
                        func.count().filter(UserRow.status == "suspended"),
                        func.count().filter(UserRow.created_at >= since),
                    ).select_from(UserRow)
                )
            ).one()
        return UserCounts(total=row[0], pending=row[1], suspended=row[2], new_in_range=row[3])

    async def add_audit(self, entry: AuditEntry) -> AuditEntry:
        from app.db.models import AdminAuditRow

        row = AdminAuditRow(
            at=entry.at,
            admin_user_id=entry.admin_user_id,
            admin_username=entry.admin_username,
            action=entry.action,
            target_user_id=entry.target_user_id,
            target_username=entry.target_username,
            detail=entry.detail,
        )
        async with self._sessions.begin() as s:
            s.add(row)
            await s.flush()
            new_id = row.id
        return entry.model_copy(update={"id": new_id})

    async def list_audit(self, limit: int) -> list[AuditEntry]:
        from sqlalchemy import select

        from app.db.models import AdminAuditRow

        async with self._sessions() as s:
            rows = await s.scalars(
                select(AdminAuditRow).order_by(AdminAuditRow.at.desc(), AdminAuditRow.id.desc()).limit(limit)
            )
            return [
                AuditEntry(
                    id=r.id,
                    at=r.at,
                    admin_user_id=r.admin_user_id,
                    admin_username=r.admin_username,
                    action=r.action,
                    target_user_id=r.target_user_id,
                    target_username=r.target_username,
                    detail=r.detail,
                )
                for r in rows
            ]
