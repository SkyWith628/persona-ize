"""운영 대시보드 API (와이어프레임 A1).

`ADMIN_USERNAMES`에 적힌 아이디로 로그인한 세션만 볼 수 있다. 헤더 스텁(`X-User-Id`)으로는 열리지 않는다.
응답에는 집계와 가린 사용자 표시만 있고 대화 원문·첨삭 문장·피드백 사유 글은 없다.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import State, current_session
from app.services.admin_service import AdminOverview, AdminService
from app.services.auth_service import ForbiddenError

router = APIRouter(prefix="/v1/admin", tags=["admin"])


async def admin_username(request: Request, state: State) -> str:
    session = await current_session(request, state)
    user = await state.auth_service.me(session)
    allowed = {name.strip() for name in state.settings.admin_usernames.split(",") if name.strip()}
    if user.username not in allowed:
        raise ForbiddenError("운영 권한이 없는 계정입니다.")
    return user.username


Admin = Annotated[str, Depends(admin_username)]


@router.get("/overview", response_model=AdminOverview, summary="품질·지연·토큰·수집 대기 집계")
async def overview(_: Admin, state: State, days: int = Query(7, ge=1, le=90)) -> AdminOverview:
    service = AdminService(
        state.rewrites,
        state.conversations,
        state.usage,
        daily_rewrite_limit=state.settings.usage_daily_rewrite_limit,
        graph_enabled=state.graph_enabled,
    )
    return await service.overview(days)
