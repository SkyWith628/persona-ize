"""운영 대시보드·회원관리 API (와이어프레임 A1).

관리자(`role == admin` 또는 `ADMIN_USERNAMES`의 아이디)로 로그인한 세션만 쓸 수 있다 — 아니면 403,
로그인 전·헤더 스텁(`X-User-Id`)은 401. 쓰기 요청은 `X-CSRF-Token` 필수.
응답에는 집계와 회원 정보(이메일 포함)만 있고 대화 원문·첨삭 문장·피드백 사유 글은 없다.
상태·권한 변경, 세션 폐기, 삭제, 회원 목록 조회는 `admin_audit_log`에 남는다.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import AppState, State, current_session, require_csrf
from app.api.schemas import (
    AccountDeletionOut,
    AdminUserList,
    AdminUserOut,
    AdminUserPatch,
    AuditList,
    AuditOut,
    RevokeSessionsOut,
)
from app.domain.auth import Role, User, UserStatus
from app.services.admin_service import AdminOverview, AdminService
from app.services.auth_service import ForbiddenError
from app.services.user_admin_service import AdminUserView, UserAdminService

router = APIRouter(prefix="/v1/admin", tags=["admin"])


async def admin_user(request: Request, state: State) -> User:
    """관리자 세션의 사용자. 쓰기 요청이면 CSRF도 여기서 확인한다."""
    session = await current_session(request, state)
    user = await state.auth_service.me(session)
    if not state.auth_service.is_admin(user):
        raise ForbiddenError("운영 권한이 없는 계정입니다.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        require_csrf(request, state, session)
    return user


Admin = Annotated[User, Depends(admin_user)]


def _users(state: AppState) -> UserAdminService:
    return UserAdminService(
        state.settings, state.auth, state.auth_service, state.contacts, state.usage, state.deletion_service
    )


def _user_out(view: AdminUserView) -> AdminUserOut:
    u = view.user
    return AdminUserOut(
        user_id=str(u.id),
        username=u.username,
        email=u.email,
        display_name=u.display_name,
        role=u.role,
        status=u.status,
        is_admin=view.is_admin,
        providers=view.providers,  # type: ignore[arg-type]
        created_at=u.created_at,
        last_login_at=u.last_login_at,
        rewrites_today=view.rewrites_today,
        contacts=view.contacts,
    )


@router.get("/overview", response_model=AdminOverview, summary="품질·지연·토큰·수집 대기·회원 수 집계")
async def overview(_: Admin, state: State, days: int = Query(7, ge=1, le=90)) -> AdminOverview:
    service = AdminService(
        state.rewrites,
        state.conversations,
        state.usage,
        daily_rewrite_limit=state.settings.usage_daily_rewrite_limit,
        graph_enabled=state.graph_enabled,
        auth=state.auth,
    )
    return await service.overview(days)


@router.get("/users", response_model=AdminUserList, summary="회원 목록·검색 (최신 가입순)")
async def list_users(
    admin: Admin,
    state: State,
    query: str = Query("", max_length=100, description="아이디·이메일·표시 이름 부분 일치"),
    status: UserStatus | None = None,
    role: Role | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AdminUserList:
    total, views = await _users(state).list_users(
        admin, query=query, status=status, role=role, limit=limit, offset=offset
    )
    return AdminUserList(total=total, items=[_user_out(v) for v in views])


@router.patch("/users/{user_id}", response_model=AdminUserOut, summary="승인·정지·권한 변경")
async def patch_user(user_id: UUID, body: AdminUserPatch, admin: Admin, state: State) -> AdminUserOut:
    """pending → active가 승인. 정지하면 그 사용자의 세션이 모두 폐기된다. 자기 자신의 정지·강등은 400."""
    view = await _users(state).update_user(admin, user_id, status=body.status, role=body.role)
    return _user_out(view)


@router.post("/users/{user_id}/revoke-sessions", response_model=RevokeSessionsOut, summary="강제 로그아웃")
async def revoke_sessions(user_id: UUID, admin: Admin, state: State) -> RevokeSessionsOut:
    return RevokeSessionsOut(revoked=await _users(state).revoke_sessions(admin, user_id))


@router.delete("/users/{user_id}", response_model=AccountDeletionOut, summary="계정과 모든 데이터 삭제")
async def delete_user(user_id: UUID, admin: Admin, state: State) -> AccountDeletionOut:
    """상대·대화·첨삭 이력·그래프·사용량·세션을 지운다 (되돌릴 수 없다). 자기 자신은 400."""
    d = await _users(state).delete_user(admin, user_id)
    return AccountDeletionOut(
        contacts=d.contacts,
        conversations=d.conversations,
        messages=d.messages,
        graph_nodes_removed=d.graph_nodes_removed,
        graph_error=d.graph_error,
    )


@router.get("/audit", response_model=AuditList, summary="관리자 작업 기록 (최근 순)")
async def audit(_: Admin, state: State, limit: int = Query(100, ge=1, le=500)) -> AuditList:
    entries = await _users(state).list_audit(limit)
    return AuditList(
        items=[
            AuditOut(
                id=e.id,
                at=e.at,
                admin_username=e.admin_username,
                action=e.action,
                target_username=e.target_username,
                detail=e.detail,
            )
            for e in entries
        ]
    )
