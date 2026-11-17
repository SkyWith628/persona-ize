"""회원관리 (관리자): 목록·검색, 승인·정지·권한 변경, 강제 로그아웃, 계정 삭제.

- 기존 `/v1/admin/users`(줄인 id + 활동 수치)와 달리 여기서는 **전체 사용자 id와 이메일**을 돌려준다 —
  승인·정지할 대상을 가리켜야 하기 때문이다. 대화 원문·첨삭 문장은 여전히 없다.
- 모든 호출은 자료를 읽거나 바꾸기 **전에** `admin_access_log`에 남긴다 (기록에 실패하면 멈춘다).
- 자기 자신은 정지·강등·삭제할 수 없다 (마지막 관리자가 스스로를 잠그는 것을 막는다).
- 정지하면 그 사용자의 세션을 모두 폐기한다. pending → active가 승인이다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.core.errors import InvalidInputError
from app.domain.auth import Role, User, UserStatus
from app.repositories.auth import AuthRepo
from app.services.auth_service import AuthService
from app.services.deletion_service import AccountDeletion, DeletionService

# (action, detail)을 관리자 접근 기록에 남기는 함수. 라우트가 지금 관리자를 묶어서 넘긴다.
Recorder = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class MemberView:
    user: User
    is_admin: bool
    providers: list[str]


class MemberAdminService:
    def __init__(
        self, auth: AuthRepo, auth_service: AuthService, deletion: DeletionService, record: Recorder
    ) -> None:
        self._auth = auth
        self._auth_service = auth_service
        self._deletion = deletion
        self._record = record

    async def _view(self, user: User) -> MemberView:
        return MemberView(
            user=user,
            is_admin=self._auth_service.is_admin(user),
            providers=await self._auth_service.providers_of(user),
        )

    async def list_members(
        self, *, query: str, status: UserStatus | None, limit: int, offset: int
    ) -> tuple[int, list[MemberView]]:
        # 검색어는 기록하지 않는다 (이메일 조각이 남을 수 있다) — 걸렀는지만 적는다.
        await self._record(
            "members", f"limit={limit} offset={offset} status={status or '-'} query={'y' if query else 'n'}"
        )
        total, users = await self._auth.list_users(query=query, status=status, limit=limit, offset=offset)
        linked = await self._auth.list_providers([u.id for u in users])
        return total, [
            MemberView(
                user=u,
                is_admin=self._auth_service.is_admin(u),
                providers=(["password"] if u.has_password else []) + linked.get(u.id, []),
            )
            for u in users
        ]

    async def update_member(
        self, admin_id: UUID, user_id: UUID, *, status: UserStatus | None, role: Role | None
    ) -> MemberView:
        if status is None and role is None:
            raise InvalidInputError("바꿀 값이 없습니다 (status 또는 role).")
        if status == "pending":
            raise InvalidInputError("승인 대기로 되돌릴 수 없습니다. 정지(suspended)를 쓰세요.")
        target = await self._auth.get_user(user_id)  # 없으면 404
        if user_id == admin_id and (status == "suspended" or role == "user"):
            raise InvalidInputError("자기 자신을 정지하거나 관리자에서 내릴 수 없습니다.")
        await self._record(
            "member_update", f"target={target.username} status={status or '-'} role={role or '-'}"
        )
        updated = await self._auth.update_user(user_id, status=status, role=role)
        if status == "suspended":
            await self._auth.revoke_all_sessions(user_id, datetime.now(UTC))
        return await self._view(updated)

    async def revoke_sessions(self, user_id: UUID) -> int:
        target = await self._auth.get_user(user_id)
        await self._record("member_revoke_sessions", f"target={target.username}")
        return await self._auth.revoke_all_sessions(user_id, datetime.now(UTC))

    async def delete_member(self, admin_id: UUID, user_id: UUID) -> AccountDeletion:
        if user_id == admin_id:
            raise InvalidInputError("자기 자신은 여기서 지울 수 없습니다 (내 계정 삭제를 쓰세요).")
        target = await self._auth.get_user(user_id)
        await self._record("member_delete", f"target={target.username}")
        return await self._deletion.delete_account_as_admin(user_id)
