"""회원관리 (관리자): 목록·검색, 승인·정지·권한, 세션 폐기, 계정 삭제.

`design/iphone-mobile` c001cbb의 `user_admin_service.py`를 main의 관리자 구조에 맞춰 옮겼다 (2026-11-16).
- 기록: 새 표를 만들지 않고 main의 `admin_access_log`에 남긴다. 부르는 쪽(관리자 라우트)이
  `log(action, detail)`을 넘긴다. **바꾸기 전에** 적는다 — 기록에 실패하면 아무것도 바꾸지 않는다(main의
  관리자 서비스와 같은 규칙). 삭제는 지운 뒤 결과(지운 수)를 한 줄 더 남긴다. 대상은 줄인 id + 그때의
  아이디로 적는다(계정이 지워져도 읽히게).
- 응답에 대화 원문·첨삭 문장은 없다. 회원을 다루는 데 필요한 것만 나간다(아이디·이메일·표시 이름·상태·권한·
  로그인 수단).
- 정지하면 그 사용자의 세션(쿠키·Bearer)을 모두 폐기한다. 폐기를 놓쳐도 `Session.is_active`가 상태를 본다.

자기 보호 규칙 (관리자가 0명이 되지 않게):
1. 관리자는 **자기 자신**을 정지·권한 내리기·삭제할 수 없다 (409 `cannot_modify_self`). 다른 관리자를 내리거나
   지워도 그 일을 한 관리자 자신은 남으므로, 이 API로는 관리자가 0명이 될 수 없다.
2. `ADMIN_USERNAMES`에 적힌 계정(운영자가 설정으로 지정한 관리자)은 이 API로 정지·삭제할 수 없다
   (409 `protected_admin`). 권한 열을 바꾸는 것은 되지만 그 계정은 설정 때문에 여전히 관리자다 — 빼려면
   환경변수에서 이름을 지운다. 화면에서 올린 관리자가 설정상의 관리자를 잠그는 일을 막는다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.errors import DomainError, InvalidInputError
from app.domain.auth import Role, User, UserStatus
from app.repositories.auth import AuthRepo
from app.services.admin_service import short_user_id
from app.services.auth_service import AuthService
from app.services.deletion_service import AccountDeletion, DeletionService

# (action, detail) → admin_access_log 한 줄. 라우트가 관리자 신원을 묶어서 넘긴다.
LogFn = Callable[[str, str], Awaitable[None]]


class CannotModifySelfError(DomainError):
    """관리자가 자기 자신을 정지·권한 내리기·삭제하려 했다."""

    status_code = 409
    code = "cannot_modify_self"


class ProtectedAdminError(DomainError):
    """`ADMIN_USERNAMES`로 지정된 관리자는 이 API로 정지·삭제할 수 없다."""

    status_code = 409
    code = "protected_admin"


class MemberOut(BaseModel):
    user_id: str = Field(description="users.id 전체 (회원관리 호출의 경로 값)")
    username: str
    email: str | None
    email_verified: bool = Field(description="소셜 제공자가 확인해 준 이메일인가 (이메일 가입은 false)")
    display_name: str | None
    role: Role
    status: UserStatus
    is_admin: bool = Field(description="role이 admin이거나 ADMIN_USERNAMES에 있다")
    config_admin: bool = Field(description="ADMIN_USERNAMES에 있는 계정 — 정지·삭제할 수 없다")
    providers: list[str] = Field(description="로그인 수단: password, google, kakao")
    created_at: datetime
    last_login_at: datetime | None
    consent_version: str | None


class MemberCounts(BaseModel):
    active: int = 0
    pending: int = 0
    suspended: int = 0


class MembersPage(BaseModel):
    total: int = Field(description="검색·필터 조건에 맞는 전체 수 (쪽 크기와 무관)")
    counts: MemberCounts = Field(description="필터와 무관한 상태별 전체 수 (승인 대기 배지용)")
    items: list[MemberOut]


class MemberUpdate(BaseModel, extra="forbid"):
    """바꿀 값만 보낸다. status는 active(승인·정지 해제)·suspended(정지)만 — pending으로 되돌리지 않는다."""

    status: UserStatus | None = None
    role: Role | None = None


class RevokeSessionsOut(BaseModel):
    revoked_sessions: int


def _target(user: User) -> str:
    return f"target={short_user_id(str(user.id))} name={user.username}"


class MemberAdminService:
    def __init__(
        self, settings: Settings, auth: AuthRepo, auth_service: AuthService, deletion: DeletionService
    ) -> None:
        self._settings = settings
        self._auth = auth
        self._auth_service = auth_service
        self._deletion = deletion

    async def list_members(
        self,
        log: LogFn,
        *,
        query: str = "",
        status: UserStatus | None = None,
        role: Role | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MembersPage:
        # 검색어는 기록에 적지 않는다(이메일일 수 있다) — 검색했는지만 남긴다.
        searched = "y" if query.strip() else "n"
        await log(
            "members", f"status={status or '-'} role={role or '-'} q={searched} limit={limit} offset={offset}"
        )
        total, users = await self._auth.list_users(
            query=query, status=status, role=role, limit=limit, offset=offset
        )
        counts = await self._auth.count_by_status()
        return MembersPage(
            total=total,
            counts=MemberCounts(**{k: counts.get(k, 0) for k in ("active", "pending", "suspended")}),
            items=await self._views(users),
        )

    async def update_member(
        self,
        log: LogFn,
        admin_id: UUID,
        user_id: UUID,
        *,
        status: UserStatus | None = None,
        role: Role | None = None,
    ) -> MemberOut:
        """상태·권한 변경. pending → active가 승인이다. 정지하면 세션을 모두 폐기한다."""
        if status is None and role is None:
            raise InvalidInputError("바꿀 값(status 또는 role)이 없습니다.")
        if status == "pending":
            raise InvalidInputError("승인 대기로 되돌릴 수는 없습니다 (active 또는 suspended).")
        target = await self._auth.get_user(user_id)
        new_status = status if status is not None and status != target.status else None
        new_role = role if role is not None and role != target.role else None
        if target.id == admin_id and (new_status == "suspended" or new_role == "user"):
            raise CannotModifySelfError("자기 자신은 정지하거나 권한을 내릴 수 없습니다.")
        if new_status == "suspended" and self._settings.is_admin_name(target.username):
            raise ProtectedAdminError("ADMIN_USERNAMES로 지정된 관리자는 정지할 수 없습니다.")
        if new_status is None and new_role is None:
            return (await self._views([target]))[0]  # 바뀌는 것이 없다 — 기록도 남기지 않는다

        if new_status == "suspended":
            await log("member_suspend", f"{_target(target)} {target.status}→suspended")
        elif new_status == "active":
            action = "member_approve" if target.status == "pending" else "member_activate"
            await log(action, f"{_target(target)} {target.status}→active")
        if new_role is not None:
            await log("member_role", f"{_target(target)} {target.role}→{new_role}")
        updated = await self._auth.update_user(user_id, status=new_status, role=new_role)
        if new_status == "suspended":
            await self._auth.revoke_all_sessions(user_id, datetime.now(UTC))
        return (await self._views([updated]))[0]

    async def revoke_sessions(self, log: LogFn, user_id: UUID) -> int:
        """그 사용자의 세션(쿠키·Bearer)을 전부 폐기한다. 자기 자신에게 쓰면 모든 기기에서 로그아웃된다."""
        target = await self._auth.get_user(user_id)
        await log("member_revoke_sessions", _target(target))
        return await self._auth.revoke_all_sessions(user_id, datetime.now(UTC))

    async def delete_member(self, log: LogFn, admin_id: UUID, user_id: UUID) -> AccountDeletion:
        """계정과 그 사용자의 자료(상대·대화·첨삭 이력·그래프·사용량·세션)를 지운다. 되돌릴 수 없다."""
        target = await self._auth.get_user(user_id)
        if target.id == admin_id:
            raise CannotModifySelfError("자기 자신은 여기서 삭제할 수 없습니다. 내 계정 삭제를 쓰세요.")
        if self._settings.is_admin_name(target.username):
            raise ProtectedAdminError("ADMIN_USERNAMES로 지정된 관리자는 삭제할 수 없습니다.")
        await log("member_delete", f"{_target(target)} status={target.status} role={target.role}")
        deleted = await self._deletion.delete_account_as_admin(user_id)
        await log(
            "member_delete_done",
            f"{_target(target)} contacts={deleted.contacts} conversations={deleted.conversations} "
            f"messages={deleted.messages} graph_error={'y' if deleted.graph_error else 'n'}",
        )
        return deleted

    async def _views(self, users: list[User]) -> list[MemberOut]:
        providers = await self._auth.list_providers([u.id for u in users])
        return [
            MemberOut(
                user_id=str(u.id),
                username=u.username,
                email=u.email,
                email_verified=u.email_verified,
                display_name=u.display_name,
                role=u.role,
                status=u.status,
                is_admin=self._auth_service.is_admin(u),
                config_admin=self._settings.is_admin_name(u.username),
                providers=(["password"] if u.has_password else []) + providers.get(u.id, []),
                created_at=u.created_at,
                last_login_at=u.last_login_at,
                consent_version=u.consent_version,
            )
            for u in users
        ]
