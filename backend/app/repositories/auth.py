"""사용자·세션 저장소 (인메모리 / PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.core.errors import NotFoundError, VersionConflictError
from app.domain.auth import Session, User


class AuthRepo(Protocol):
    async def create_user(
        self,
        username: str,
        password_hash: str,
        *,
        consented_at: datetime | None = None,
        consent_version: str | None = None,
    ) -> User:
        """username이 이미 있으면 VersionConflictError. 동의 값을 주면 가입과 함께 기록한다."""
        ...

    async def get_user(self, user_id: UUID) -> User: ...

    async def find_user_by_username(self, username: str) -> tuple[User, str] | None:
        """(사용자, 비밀번호 해시). 없으면 None. 대소문자를 구분한다."""
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


class InMemoryAuthRepo:
    def __init__(self) -> None:
        self._users: dict[UUID, tuple[User, str]] = {}
        self._sessions: dict[str, Session] = {}

    async def create_user(
        self,
        username: str,
        password_hash: str,
        *,
        consented_at: datetime | None = None,
        consent_version: str | None = None,
    ) -> User:
        if any(u.username == username for u, _ in self._users.values()):
            raise VersionConflictError("이미 사용 중인 이름입니다.")
        user = User(
            id=uuid4(),
            username=username,
            created_at=datetime.now(UTC),
            consented_at=consented_at,
            consent_version=consent_version,
        )
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
        self._users[user_id] = (user, password_hash)

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
        password_hash: str,
        *,
        consented_at: datetime | None = None,
        consent_version: str | None = None,
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
        )
        try:
            async with self._sessions.begin() as s:
                s.add(row)
        except IntegrityError as exc:
            raise VersionConflictError("이미 사용 중인 이름입니다.") from exc
        return _user_from_row(row)

    async def get_user(self, user_id: UUID) -> User:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.id == user_id))
        if row is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        return _user_from_row(row)

    async def find_user_by_username(self, username: str) -> tuple[User, str] | None:
        from sqlalchemy import select

        from app.db.models import UserRow

        async with self._sessions() as s:
            row = await s.scalar(select(UserRow).where(UserRow.username == username))
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
