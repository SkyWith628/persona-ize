"""관리자 API (`/v1/admin/*`) — 품질·비용·수집 작업 현황. 대화 원문과 첨삭 문장은 돌려주지 않는다.

접근: 세션 로그인 + 이름이 `ADMIN_USERNAMES`에 있는 사용자만. 접근은 `admin_access_log`에 남긴다.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v1/admin", tags=["admin"])
