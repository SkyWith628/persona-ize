"""사용자·세션 저장소 (인메모리 / PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.core.errors import NotFoundError, VersionConflictError
from app.domain.auth import Session, User


class AuthRepo(Protocol):
    async def create_user(self, username: str, password_hash: str) -> User:
        """username이 이미 있으면 VersionConflictError."""
        ...

    async def get_user(self, user_id: UUID) -> User: ...

    async def find_user_by_username(self, username: str) -> tuple[User, str] | None:
        """(사용자, 비밀번호 해시). 없으면 None."""
        ...

    async def create_session(self, session: Session) -> Session: ...

    async def get_session(self, token_hash: str) -> Session | None: ...

    async def revoke_session(self, token_hash: str, at: datetime) -> None: ...

    async def update_password(self, user_id: UUID, password_hash: str) -> None: ...

    async def revoke_other_sessions(self, user_id: UUID, keep_token_hash: str, at: datetime) -> int:
        """이 사용자의 다른 활성 세션을 모두 폐기한다 (비밀번호 변경 뒤). 폐기한 수."""
        ...


class InMemoryAuthRepo:
    def __init__(self) -> None:
        self._users: dict[UUID, tuple[User, str]] = {}
        self._sessions: dict[str, Session] = {}

    async def create_user(self, username: str, password_hash: str) -> User:
        if any(u.username == username for u, _ in self._users.values()):
            raise VersionConflictError("이미 사용 중인 이름입니다.")
        user = User(id=uuid4(), username=username, created_at=datetime.now(UTC))
        self._users[user.id] = (user, password_hash)
        return user

    async def get_user(self, user_id: UUID) -> User:
        found = self._users.get(user_id)
        if found is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        return found[0]

    async def find_user_by_username(self, username: str) -> tuple[User, str] | None:
        for user, pw in self._users.values():
            if user.username == username:
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
        self._users[user_id] = (user, password_hash)

    async def revoke_other_sessions(self, user_id: UUID, keep_token_hash: str, at: datetime) -> int:
        n = 0
        for key, s in list(self._sessions.items()):
            if s.user_id == user_id and key != keep_token_hash and s.revoked_at is None:
                self._sessions[key] = s.model_copy(update={"revoked_at": at})
                n += 1
        return n


class PostgresAuthRepo:
    def __init__(self, sessions) -> None:  # noqa: ANN001 - SessionFactory (db extra)
        self._sessions = sessions

    async def create_user(self, username: str, password_hash: str) -> User:
        from sqlalchemy.exc import IntegrityError

        from app.db.models import UserRow

        row = UserRow(
            id=uuid4(), username=username, password_hash=password_hash, created_at=datetime.now(UTC)
        )
        try:
            async with self._sessions.begin() as s:
                s.add(row)
        except IntegrityError as exc:
            raise VersionConflictError("이미 사용 중인 이름입니다.") from exc
        return User(id=row.id, username=row.username, created_at=row.created_at)

    async def get_user(self, user_id: UUID) -> User:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.id == user_id))
        if row is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        return User(id=row.id, username=row.username, created_at=row.created_at)

    async def find_user_by_username(self, username: str) -> tuple[User, str] | None:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.username == username))
        if row is None:
            return None
        return User(id=row.id, username=row.username, created_at=row.created_at), row.password_hash

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
