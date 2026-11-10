"""내 데이터 API — 원문 파싱 미리보기, 데이터 요약, 내보내기, 관계별 기본 페르소나.

경로는 접두사 없이 전체를 적는다:
`/v1/imports/parse`, `/v1/data/summary`, `/v1/export`, `/v1/persona-defaults`.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["data"])
