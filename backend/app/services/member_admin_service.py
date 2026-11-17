"""회원관리 (관리자): 목록·검색, 승인·정지·권한, 세션 폐기, 계정 삭제.

`design/iphone-mobile` c001cbb의 `user_admin_service.py`를 main의 관리자 구조에 맞춰 옮겼다 (2026-11-16).
- 응답에 대화 원문·첨삭 문장은 없다. 회원을 다루는 데 필요한 것만 나간다(아이디·이메일·표시 이름·상태·권한·
  로그인 수단).
- 정지하면 그 사용자의 세션(쿠키·Bearer)을 모두 폐기한다. 폐기를 놓쳐도 `Session.is_active`가 상태를 본다.

기록 (2026-11-17 보안 검토에서 고침): 새 표를 만들지 않고 main의 `admin_access_log`에 남긴다. 부르는 쪽
(관리자 라우트)이 `log(action, detail)`을 넘긴다.
- **바꾸기 전에** 한 줄(`member_suspend` 등) — 이 기록에 실패하면 아무것도 바꾸지 않는다(main의 관리자
  서비스와 같은 규칙).
- **끝난 뒤** 결과 한 줄: `<action>_done`(정지·세션 폐기는 `sessions=<폐기한 수>`, 삭제는 지운 수) 또는
  `<action>_failed reason=<오류 코드 | 예외 종류>`. 결과 줄을 못 적어도 이미 한 일은 되돌릴 수 없으므로
  서버 로그에 경고만 남기고 응답은 그대로 낸다.
- **거절된 시도**(409 `cannot_modify_self`·`protected_admin`·`member_changed`)도 `member_refused`로 남긴다:
  `target=… op=update|delete status=… role=… reason=<코드>`. 형식이 틀린 요청(422)·없는 회원(404)은 대상이
  없어 남기지 않는다.
- 대상은 **줄인 사용자 id**(`u_abc…12`)로만 적는다. 아이디·이메일·표시 이름은 적지 않는다 — 이메일로 가입한
  회원의 아이디는 이메일 앞부분이라, 계정을 지운 뒤에도 기록에 개인정보가 남기 때문이다(main의 규칙:
  관리자 기록의 `detail`에는 매개변수·코드·수만 들어간다).

자기 보호 규칙 (활성 관리자가 0명이 되지 않게):
1. 관리자는 **자기 자신**을 정지·권한 내리기·삭제할 수 없다 (409 `cannot_modify_self`).
2. `ADMIN_USERNAMES`에 적힌 계정(운영자가 설정으로 지정한 관리자)은 이 API로 정지·삭제할 수 없다
   (409 `protected_admin`). 권한 열을 바꾸는 것은 되지만 그 계정은 설정 때문에 여전히 관리자다 — 빼려면
   환경변수에서 이름을 지운다. 화면에서 올린 관리자가 설정상의 관리자를 잠그는 일을 막는다.
3. 위 둘만으로는 두 관리자가 **동시에** 서로를 내리거나 지울 때 둘 다 통과한다. 그래서 저장소가 변경과
   같은 트랜잭션에서 "이 일 뒤에도 다른 활성 관리자가 남는가"를 다시 본다 — 안 남으면 409 `last_admin`
   (`repositories/auth.py`의 `is_active_admin`: 상태 active + 권한 admin 또는 `ADMIN_USERNAMES`의 이름,
   실제로 있는 계정만 센다). 본인 탈퇴(`DELETE /v1/auth/me`)도 같은 규칙이다(`DeletionService`).

오래된 화면 (`expected_status`): 요청에 이 값을 실으면 그 회원의 지금 상태가 같을 때만 바꾼다. 다르면
409 `member_changed` — 승인 대기로 보이던 줄의 "승인"이, 그 사이 다른 관리자가 건 정지를 풀지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.errors import DomainError, InvalidInputError, MemberChangedError
from app.domain.auth import Role, User, UserStatus
from app.repositories.auth import MEMBER_CHANGED, AuthRepo
from app.services.admin_service import short_user_id
from app.services.auth_service import AuthService
from app.services.deletion_service import AccountDeletion, DeletionService

log = logging.getLogger(__name__)

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
    expected_status: UserStatus | None = Field(
        None,
        description="화면이 그 회원을 그릴 때 본 상태. 지금 상태와 다르면 409 `member_changed`",
    )


class RevokeSessionsOut(BaseModel):
    revoked_sessions: int


def _target(user: User) -> str:
    """기록에 적는 대상 — 줄인 사용자 id만. 아이디·이메일은 적지 않는다(모듈 머리말)."""
    return f"target={short_user_id(str(user.id))}"


def _reason(exc: BaseException) -> str:
    """실패 사유: 도메인 오류는 코드, 그 밖에는 예외 종류만 (예외 문장은 적지 않는다)."""
    return exc.code if isinstance(exc, DomainError) else type(exc).__name__


async def _outcome(record: LogFn, action: str, detail: str) -> None:
    """결과 줄을 남긴다. 실패해도 예외를 올리지 않는다 — 이미 한 일(또는 이미 난 오류)을 가리지 않게."""
    try:
        await record(action, detail)
    except Exception as exc:  # noqa: BLE001 - 기록 실패의 종류만 서버 로그에 남긴다
        log.warning("admin access log outcome not recorded action=%s kind=%s", action, type(exc).__name__)


async def _refuse(record: LogFn, who: str, op: str, error: DomainError) -> NoReturn:
    """서버 규칙에 걸린 시도를 기록하고 그 오류를 올린다."""
    await record("member_refused", f"{who} {op} reason={error.code}")
    raise error


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
        expected_status: UserStatus | None = None,
    ) -> MemberOut:
        """상태·권한 변경. pending → active가 승인이다. 정지하면 세션을 모두 폐기한다.

        `expected_status`를 주면 지금 상태가 그 값일 때만 바꾼다(아니면 409 `member_changed`).
        """
        if status is None and role is None:
            raise InvalidInputError("바꿀 값(status 또는 role)이 없습니다.")
        if status == "pending":
            raise InvalidInputError("승인 대기로 되돌릴 수는 없습니다 (active 또는 suspended).")
        target = await self._auth.get_user(user_id)
        who = _target(target)
        op = f"op=update status={status or '-'} role={role or '-'}"
        if expected_status is not None and target.status != expected_status:
            await _refuse(log, who, op, MemberChangedError(MEMBER_CHANGED))
        new_status = status if status is not None and status != target.status else None
        new_role = role if role is not None and role != target.role else None
        if target.id == admin_id and (new_status == "suspended" or new_role == "user"):
            await _refuse(
                log, who, op, CannotModifySelfError("자기 자신은 정지하거나 권한을 내릴 수 없습니다.")
            )
        if new_status == "suspended" and self._settings.is_admin_name(target.username):
            await _refuse(
                log, who, op, ProtectedAdminError("ADMIN_USERNAMES로 지정된 관리자는 정지할 수 없습니다.")
            )
        if new_status is None and new_role is None:
            return (await self._views([target]))[0]  # 바뀌는 것이 없다 — 기록도 남기지 않는다

        # 하려는 일을 먼저 적는다 (적지 못하면 바꾸지 않는다). 끝나면 같은 이름에 _done·_failed를 붙여 적는다.
        actions: list[str] = []
        if new_status == "suspended":
            actions.append("member_suspend")
            await log("member_suspend", f"{who} {target.status}→suspended")
        elif new_status == "active":
            actions.append("member_approve" if target.status == "pending" else "member_activate")
            await log(actions[-1], f"{who} {target.status}→active")
        if new_role is not None:
            actions.append("member_role")
            await log("member_role", f"{who} {target.role}→{new_role}")
        try:
            # 기대 상태·마지막 관리자 검사는 저장소가 변경과 같은 트랜잭션에서 한다 (동시 요청에도 맞게).
            updated = await self._auth.update_user(
                user_id,
                status=new_status,
                role=new_role,
                expected_status=expected_status,
                admin_names=self._settings.admin_name_list,
            )
        except Exception as exc:
            for action in actions:
                await _outcome(log, f"{action}_failed", f"{who} reason={_reason(exc)}")
            raise
        # 여기부터는 상태·권한이 이미 바뀌었다. 세션 폐기가 실패해도 결과 줄은 "바뀌었다"로 남긴다
        # (정지된 계정의 세션은 폐기를 놓쳐도 `Session.is_active`에서 끊긴다).
        sessions, revoke_error = "", None
        if new_status == "suspended":
            try:
                sessions = f" sessions={await self._auth.revoke_all_sessions(user_id, datetime.now(UTC))}"
            except Exception as exc:  # noqa: BLE001 - 결과 줄을 남긴 뒤 다시 올린다
                sessions, revoke_error = f" sessions=error:{_reason(exc)}", exc
        for action in actions:
            await _outcome(log, f"{action}_done", f"{who}{sessions if action == 'member_suspend' else ''}")
        if revoke_error is not None:
            raise revoke_error
        return (await self._views([updated]))[0]

    async def revoke_sessions(self, log: LogFn, user_id: UUID) -> int:
        """그 사용자의 세션(쿠키·Bearer)을 전부 폐기한다. 자기 자신에게 쓰면 모든 기기에서 로그아웃된다."""
        target = await self._auth.get_user(user_id)
        who = _target(target)
        await log("member_revoke_sessions", who)
        try:
            revoked = await self._auth.revoke_all_sessions(user_id, datetime.now(UTC))
        except Exception as exc:
            await _outcome(log, "member_revoke_sessions_failed", f"{who} reason={_reason(exc)}")
            raise
        await _outcome(log, "member_revoke_sessions_done", f"{who} sessions={revoked}")
        return revoked

    async def delete_member(self, log: LogFn, admin_id: UUID, user_id: UUID) -> AccountDeletion:
        """계정과 그 사용자의 자료(상대·대화·첨삭 이력·그래프·사용량·세션)를 지운다. 되돌릴 수 없다."""
        target = await self._auth.get_user(user_id)
        who = _target(target)
        if target.id == admin_id:
            await _refuse(
                log,
                who,
                "op=delete",
                CannotModifySelfError("자기 자신은 여기서 삭제할 수 없습니다. 내 계정 삭제를 쓰세요."),
            )
        if self._settings.is_admin_name(target.username):
            await _refuse(
                log,
                who,
                "op=delete",
                ProtectedAdminError("ADMIN_USERNAMES로 지정된 관리자는 삭제할 수 없습니다."),
            )
        await log("member_delete", f"{who} status={target.status} role={target.role}")
        try:
            # 대상이 하나뿐인 활성 관리자면 `LastAdminError` (동시에 서로를 지울 때 진 쪽) — DeletionService
            deleted = await self._deletion.delete_account_as_admin(user_id)
        except Exception as exc:
            await _outcome(log, "member_delete_failed", f"{who} reason={_reason(exc)}")
            raise
        await _outcome(
            log,
            "member_delete_done",
            f"{who} contacts={deleted.contacts} conversations={deleted.conversations} "
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
