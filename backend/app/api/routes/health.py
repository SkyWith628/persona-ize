"""헬스체크. 기본은 가볍게(설정 정보), `?deep=true`면 DB·Neo4j 연결까지 확인한다 (Docker·배포 점검용)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.deps import State

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 3.0


async def _check(coro) -> str:  # noqa: ANN001
    try:
        await asyncio.wait_for(coro, CHECK_TIMEOUT_SECONDS)
        return "ok"
    except TimeoutError:
        return "error: timeout"
    except Exception as exc:  # noqa: BLE001 - 상태 문자열로 보고
        return f"error: {type(exc).__name__}"


async def _check_postgres(state) -> None:  # noqa: ANN001
    from sqlalchemy import text

    async with state.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@router.get("/healthz")
async def healthz(state: State, deep: bool = False) -> JSONResponse:
    body: dict[str, Any] = {
        "status": "ok",
        "llm_provider": state.settings.llm_provider,
        "llm_model": state.settings.llm_model if state.settings.llm_provider != "fake" else "fake-echo",
        "prompt_version": state.settings.prompt_version,
        "storage": state.storage,
        "graph": state.graph_enabled,
    }
    if deep:
        checks: dict[str, str] = {}
        checks["postgres"] = await _check(_check_postgres(state)) if state.engine is not None else "skipped"
        checks["neo4j"] = (
            await _check(state.graph_store.ping()) if state.graph_store is not None else "skipped"
        )
        body["checks"] = checks
        if any(v.startswith("error") for v in checks.values()):
            body["status"] = "degraded"
            return JSONResponse(status_code=503, content=body)
    return JSONResponse(content=body)
