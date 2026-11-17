"""관리자 API (`/v1/admin/*`) — 품질·비용·수집 작업 현황. 대화 원문과 첨삭 문장은 돌려주지 않는다.

접근: 로그인 세션(쿠키, 또는 `current_session`이 받아 주는 Bearer) + 관리자만. 관리자는 이름이
`ADMIN_USERNAMES`에 있거나(대소문자까지 같아야 한다) 권한이 admin인 사용자다
(`AuthService.is_admin`, 2026-11-16).
- 로그인하지 않았으면 401, 관리자가 아니면 403 `forbidden`. 개발용 헤더 스텁(`X-User-Id`)은 세션이
  아니므로 401이다 — 누구나 아무 id로 관리자 화면을 열 수 없게.
- 저장소가 PostgreSQL이 아니면(인메모리) 503 `admin_unavailable` — 집계 SQL을 돌릴 곳이 없다.
- 통과한 호출은 전부 `admin_access_log`에 남는다(서비스가 자료를 읽거나 바꾸기 전에 기록한다). 회원관리의
  쓰기는 결과(`*_done`·`*_failed`)와 서버 규칙에 걸린 시도(`member_refused`)도 남긴다.
- 쓰기(`POST /graph/retry-held`, 회원관리의 PATCH·POST·DELETE)는 쿠키 인증이면 `X-CSRF-Token`이 맞아야
  한다(403, 관리자 검사 뒤). 안전하지 않은 메서드 전부에 `require_admin`이 한 곳에서 건다.
- 회원관리(`/members*`, 2026-11-16): 목록·승인·정지·권한·세션 폐기·삭제. 규칙은 `member_admin_service.py`,
  기록은 같은 `admin_access_log`. 여기서만 사용자 id 전체와 이메일이 나간다(회원을 다루는 데 필요하다).

검사 순서는 401 → 403 → 503이다. 로그인하지 않은 요청에는 서버 구성(저장소 종류)도 알려 주지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field

from app.api.deps import SAFE_METHODS, AppState, State, current_session, require_csrf
from app.domain.auth import Role, UserStatus
from app.services.admin_service import (
    AccessLogOut,
    AdminForbiddenError,
    AdminIdentity,
    AdminService,
    AdminUnavailableError,
    JobOut,
    OverviewOut,
    RejectedFeedbackOut,
    RetryHeldOut,
    UsersPage,
    short_user_id,
)
from app.services.deletion_service import AccountDeletion
from app.services.member_admin_service import (
    LogFn,
    MemberAdminService,
    MemberOut,
    MembersPage,
    MemberUpdate,
    RevokeSessionsOut,
)

log = logging.getLogger(__name__)

MAX_LIST_LIMIT = 200
MAX_OFFSET = 10_000_000  # 터무니없이 큰 값이 DB까지 가서 500이 되지 않게 입구에서 막는다


def _no_store(response: Response) -> None:
    """운영 수치는 브라우저·프록시에 남기지 않는다 (공통 미들웨어가 같은 값을 넣어도 덮어쓸 뿐이다)."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(_no_store)])


@dataclass(frozen=True)
class AdminAccess:
    """접근 검사를 통과한 요청: 누구인지와, 그 요청에 쓸 서비스."""

    admin: AdminIdentity
    service: AdminService
    members: MemberAdminService

    @property
    def log(self) -> LogFn:
        """이 관리자의 이름으로 `admin_access_log`에 한 줄 남기는 함수 (회원관리 서비스에 넘긴다)."""
        return partial(self.service.record, self.admin)

    @property
    def admin_id(self) -> UUID:
        return UUID(self.admin.user_id)


def _admin_repo(request: Request, state: AppState):  # noqa: ANN202 - AdminRepo (db extra가 있을 때만 import)
    """엔진마다 세션 팩토리를 한 번 만들어 앱 상태에 두고 다시 쓴다 (`api/deps.py`는 건드리지 않는다)."""
    cached = getattr(request.app.state, "admin_repo", None)
    if cached is not None and cached[0] is state.engine:
        return cached[1]
    # sqlalchemy는 PostgreSQL 저장소일 때만 있어도 되므로 여기서 import한다 (deps._build_repos와 같은 이유).
    from app.db.session import create_session_factory
    from app.repositories.admin import AdminRepo

    repo = AdminRepo(create_session_factory(state.engine))
    request.app.state.admin_repo = (state.engine, repo)
    return repo


async def require_admin(request: Request, state: State) -> AdminAccess:
    session = await current_session(request, state)  # 세션이 없으면 401
    user = await state.auth_service.me(session)
    if not state.auth_service.is_admin(user):
        # 대소문자만 다른 이름이면 운영자가 설정을 의심할 수 있게 로그에 남긴다 (응답에는 알리지 않는다).
        near = user.username.lower() in state.settings.admin_username_set
        log.warning(
            "admin access denied user=%s path=%s%s",
            short_user_id(str(user.id)),
            request.url.path,
            " (ADMIN_USERNAMES와 대소문자만 다른 이름)" if near else "",
        )
        raise AdminForbiddenError("관리자만 볼 수 있습니다.")
    if (
        request.method not in SAFE_METHODS
    ):  # 쓰기(보류 다시 시도·회원관리)는 쿠키 인증이면 CSRF 토큰이 맞아야 한다
        require_csrf(request, state, session)
    if state.engine is None:
        raise AdminUnavailableError("관리자 화면은 PostgreSQL 저장소에서만 볼 수 있습니다.")
    return AdminAccess(
        admin=AdminIdentity(user_id=str(user.id), username=user.username),
        service=AdminService(
            state.settings, _admin_repo(request, state), state.graph_store, state.graph_ingest
        ),
        members=state.member_admin,  # type: ignore[arg-type] - build_state가 항상 채운다
    )


Admin = Annotated[AdminAccess, Depends(require_admin)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)]


@router.get("/overview", response_model=OverviewOut, summary="기간 요약 (품질·응답 시간·피드백·비용·수집)")
async def overview(access: Admin, days: int = 7) -> OverviewOut:
    """`days`는 1(오늘)·7·30. 날짜 경계는 사용량 한도와 같은 하루 경계(기본 한국 시간)."""
    return await access.service.overview(access.admin, days)


@router.get("/users", response_model=UsersPage, summary="사용자 목록 (최근 가입 순)")
async def list_users(
    access: Admin, limit: Limit = 50, offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0
) -> UsersPage:
    return await access.service.users(access.admin, limit, offset)


@router.get("/jobs", response_model=list[JobOut], summary="최근 가져오기 작업")
async def list_jobs(access: Admin, limit: Limit = 50) -> list[JobOut]:
    return await access.service.jobs(access.admin, limit)


@router.get("/feedback", response_model=list[RejectedFeedbackOut], summary="최근 거절 피드백 (선택형 사유만)")
async def list_rejected_feedback(access: Admin, limit: Limit = 50) -> list[RejectedFeedbackOut]:
    return await access.service.rejected_feedback(access.admin, limit)


@router.get("/access-log", response_model=list[AccessLogOut], summary="관리자 접근 기록")
async def list_access_log(access: Admin, limit: Limit = 50) -> list[AccessLogOut]:
    return await access.service.access_log(access.admin, limit)


@router.post("/graph/retry-held", response_model=RetryHeldOut, summary="보류된 그래프 메시지 다시 시도")
async def retry_held_graph(access: Admin) -> RetryHeldOut:
    """거듭 실패해 보류된 메시지를 풀고 수집을 백그라운드로 다시 시작한다 (본문 없음, 바로 응답).

    그래프는 지우지 않는다. 메시지마다 모델 호출 비용이 들고, 수집 상한(`GRAPH_INGEST_*_CAP`)을 따른다.
    그래프가 꺼진 서버는 409 `graph_disabled`.
    """
    return await access.service.retry_held(access.admin)


# ---------- 회원관리 (2026-11-16) ----------


class MemberDeletionOut(AccountDeletion):
    """지운 것의 수. `user_id`는 응답에서 뺀다(경로에 이미 있다)."""

    user_id: str = Field(exclude=True)


@router.get("/members", response_model=MembersPage, summary="회원 목록·검색 (최근 가입 순)")
async def list_members(
    access: Admin,
    query: Annotated[str, Query(max_length=100, description="아이디·이메일·표시 이름 부분 일치")] = "",
    status: UserStatus | None = None,
    role: Role | None = None,
    limit: Limit = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> MembersPage:
    """`counts`는 필터와 무관한 상태별 전체 수다(승인 대기 배지). `total`은 조건에 맞는 수."""
    return await access.members.list_members(
        access.log, query=query, status=status, role=role, limit=limit, offset=offset
    )


@router.patch("/members/{user_id}", response_model=MemberOut, summary="승인·정지·정지 해제·권한 변경")
async def update_member(user_id: UUID, body: MemberUpdate, access: Admin) -> MemberOut:
    """본문 `{"status": "active"|"suspended"}` 그리고/또는 `{"role": "user"|"admin"}`.

    pending → active가 승인이다. 정지하면 그 사용자의 세션·토큰이 모두 폐기된다. 자기 자신 정지·권한 내리기는
    409 `cannot_modify_self`, `ADMIN_USERNAMES`의 계정 정지는 409 `protected_admin`, 이 변경으로 활성
    관리자가 0명이 되면 409 `last_admin`(두 관리자가 동시에 서로를 내릴 때 진 쪽).

    `expected_status`(선택)는 화면이 그 회원을 그릴 때 본 상태다. 지금 상태와 다르면 아무것도 바꾸지 않고
    409 `member_changed` — 오래된 화면의 "승인"이 그 사이 걸린 정지를 풀지 않게, 화면은 이 값을 늘 보낸다.
    """
    return await access.members.update_member(
        access.log,
        access.admin_id,
        user_id,
        status=body.status,
        role=body.role,
        expected_status=body.expected_status,
    )


@router.post(
    "/members/{user_id}/revoke-sessions", response_model=RevokeSessionsOut, summary="그 회원의 세션 전부 폐기"
)
async def revoke_member_sessions(user_id: UUID, access: Admin) -> RevokeSessionsOut:
    return RevokeSessionsOut(revoked_sessions=await access.members.revoke_sessions(access.log, user_id))


@router.delete("/members/{user_id}", response_model=MemberDeletionOut, summary="회원 삭제 (되돌릴 수 없다)")
async def delete_member(user_id: UUID, access: Admin) -> AccountDeletion:
    """계정과 상대·대화·첨삭 이력·그래프·사용량·세션을 지운다. 자기 자신은 409 `cannot_modify_self`,
    `ADMIN_USERNAMES`의 계정은 409 `protected_admin`, 하나뿐인 활성 관리자는 409 `last_admin`."""
    return await access.members.delete_member(access.log, access.admin_id, user_id)
