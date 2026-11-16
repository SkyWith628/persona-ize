"""회원관리 (관리자): 목록·검색, 승인·정지·권한, 세션 폐기, 계정 삭제, 작업 기록.

- 목록에는 수치(오늘 첨삭 수·상대 수)만 붙인다. 대화 원문·첨삭 문장은 다루지 않는다.
- 상태·권한을 바꾸는 모든 작업과 목록 조회(첫 쪽, limit > 1)는 `admin_audit_log`에 한 줄씩 남는다.
- 정지하면 그 사용자의 세션을 모두 폐기한다. 자기 자신은 정지·권한 내리기·삭제할 수 없다(400).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import BadRequestError
from app.domain.auth import AuditEntry, Role, User, UserStatus
from app.domain.usage import usage_day
from app.repositories.auth import AuthRepo
from app.repositories.base import ContactRepo
from app.repositories.usage import UsageRepo
from app.services.auth_service import AuthService
from app.services.deletion_service import AccountDeletion, DeletionService

MAX_DETAIL = 300


class AdminUserView(BaseModel):
    user: User
    is_admin: bool
    providers: list[str]
    rewrites_today: int = 0
    contacts: int = 0


class UserAdminService:
    def __init__(
        self,
        settings: Settings,
        auth: AuthRepo,
        auth_service: AuthService,
        contacts: ContactRepo,
        usage: UsageRepo,
        deletion: DeletionService,
    ) -> None:
        self._settings = settings
        self._auth = auth
        self._auth_service = auth_service
        self._contacts = contacts
        self._usage = usage
        self._deletion = deletion

    async def list_users(
        self,
        admin: User,
        *,
        query: str = "",
        status: UserStatus | None = None,
        role: Role | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[AdminUserView]]:
        total, users = await self._auth.list_users(
            query=query, status=status, role=role, limit=limit, offset=offset
        )
        views = await self._views(users)
        # 화면이 대기 수를 세려고 limit=1로 자주 부르는 조회와 다음 쪽 넘김은 적지 않는다 (기록이 읽히게).
        if offset == 0 and limit > 1:
            detail = f"query={query.strip()!r} status={status or '-'} role={role or '-'} n={len(users)}"
            await self._record(admin, "users.view", None, detail)
        return total, views

    async def update_user(
        self, admin: User, user_id: UUID, *, status: UserStatus | None = None, role: Role | None = None
    ) -> AdminUserView:
        """상태·권한 변경. pending → active가 승인. 정지하면 세션을 모두 폐기한다."""
        if status is None and role is None:
            raise BadRequestError("바꿀 값(status 또는 role)이 없습니다.")
        target = await self._auth.get_user(user_id)
        if target.id == admin.id and (status == "suspended" or (role == "user" and target.role == "admin")):
            raise BadRequestError("자기 자신은 정지하거나 권한을 내릴 수 없습니다.")
        new_status = status if status is not None and status != target.status else None
        new_role = role if role is not None and role != target.role else None
        if new_status is None and new_role is None:
            return (await self._views([target]))[0]
        updated = await self._auth.update_user(user_id, status=new_status, role=new_role)
        if new_status == "suspended":
            revoked = await self._auth.revoke_user_sessions(user_id, datetime.now(UTC))
            await self._record(admin, "user.suspend", target, f"revoked_sessions={revoked}")
        elif new_status == "active":
            action = "user.approve" if target.status == "pending" else "user.activate"
            await self._record(admin, action, target, f"{target.status}→active")
        if new_role is not None:
            await self._record(admin, "user.role", target, f"{target.role}→{new_role}")
        return (await self._views([updated]))[0]

    async def revoke_sessions(self, admin: User, user_id: UUID) -> int:
        target = await self._auth.get_user(user_id)
        revoked = await self._auth.revoke_user_sessions(user_id, datetime.now(UTC))
        await self._record(admin, "user.revoke_sessions", target, f"revoked_sessions={revoked}")
        return revoked

    async def delete_user(self, admin: User, user_id: UUID) -> AccountDeletion:
        target = await self._auth.get_user(user_id)
        if target.id == admin.id:
            raise BadRequestError("자기 자신은 여기서 삭제할 수 없습니다. 내 계정 삭제를 쓰세요.")
        deleted = await self._deletion.delete_account_as_admin(user_id)
        detail = (
            f"contacts={deleted.contacts} conversations={deleted.conversations} messages={deleted.messages}"
        )
        await self._record(admin, "user.delete", target, detail)
        return deleted

    async def list_audit(self, limit: int) -> list[AuditEntry]:
        return await self._auth.list_audit(limit)

    async def _views(self, users: list[User]) -> list[AdminUserView]:
        ids = [u.id for u in users]
        keys = [str(i) for i in ids]
        day = usage_day(offset_hours=self._settings.usage_day_offset_hours)
        providers = await self._auth.list_providers(ids)
        rewrites = await self._usage.rewrites_by_users(day, keys)
        contacts = await self._contacts.count_by_users(keys)
        return [
            AdminUserView(
                user=u,
                is_admin=self._auth_service.is_admin(u),
                providers=(["password"] if u.has_password else []) + providers.get(u.id, []),
                rewrites_today=rewrites.get(str(u.id), 0),
                contacts=contacts.get(str(u.id), 0),
            )
            for u in users
        ]

    async def _record(self, admin: User, action: str, target: User | None, detail: str = "") -> None:
        await self._auth.add_audit(
            AuditEntry(
                at=datetime.now(UTC),
                admin_user_id=admin.id,
                admin_username=admin.username,
                action=action,
                target_user_id=target.id if target else None,
                target_username=target.username if target else None,
                detail=detail[:MAX_DETAIL],
            )
        )
