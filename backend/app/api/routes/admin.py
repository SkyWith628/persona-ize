"""관리자 API (`/v1/admin/*`) — 품질·비용·수집 작업 현황. 대화 원문과 첨삭 문장은 돌려주지 않는다.

접근: 로그인 세션(쿠키, 또는 `current_session`이 받아 주는 Bearer) + 이름이 `ADMIN_USERNAMES`에 있는 사용자만
(대소문자까지 같아야 한다 — `admin_service.is_admin_username`).
- 로그인하지 않았으면 401, 관리자가 아니면 403 `forbidden`. 개발용 헤더 스텁(`X-User-Id`)은 세션이
  아니므로 401이다 — 누구나 아무 id로 관리자 화면을 열 수 없게.
- 저장소가 PostgreSQL이 아니면(인메모리) 503 `admin_unavailable` — 집계 SQL을 돌릴 곳이 없다.
- 통과한 호출은 전부 `admin_access_log`에 남는다(서비스가 자료를 읽거나 바꾸기 전에 기록한다).
- 쓰기는 `POST /graph/retry-held` 하나다. 쿠키 인증이면 `X-CSRF-Token`이 맞아야 한다(403, 관리자 검사 뒤).

검사 순서는 401 → 403 → 503이다. 로그인하지 않은 요청에는 서버 구성(저장소 종류)도 알려 주지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.deps import SAFE_METHODS, AppState, State, current_session, require_csrf
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
    is_admin_username,
    short_user_id,
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
    if not is_admin_username(user.username, state.settings):
        # 대소문자만 다른 이름이면 운영자가 설정을 의심할 수 있게 로그에 남긴다 (응답에는 알리지 않는다).
        near = user.username.lower() in state.settings.admin_username_set
        log.warning(
            "admin access denied user=%s path=%s%s",
            short_user_id(str(user.id)),
            request.url.path,
            " (ADMIN_USERNAMES와 대소문자만 다른 이름)" if near else "",
        )
        raise AdminForbiddenError("관리자만 볼 수 있습니다.")
    if request.method not in SAFE_METHODS:  # 쓰기(보류 다시 시도)는 쿠키 인증이면 CSRF 토큰이 맞아야 한다
        require_csrf(request, state, session)
    if state.engine is None:
        raise AdminUnavailableError("관리자 화면은 PostgreSQL 저장소에서만 볼 수 있습니다.")
    return AdminAccess(
        admin=AdminIdentity(user_id=str(user.id), username=user.username),
        service=AdminService(
            state.settings, _admin_repo(request, state), state.graph_store, state.graph_ingest
        ),
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
