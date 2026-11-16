"""헬스체크. 기본은 가볍게(설정 정보), `?deep=true`면 DB·Neo4j 연결까지 확인한다 (Docker·배포 점검용).

운영 모드(`APP_ENV=production`)에서는 모델 이름·저장소 종류 같은 설정 정보를 빼고 `status`만 준다
(`deep`이면 `checks`도). 이 경로는 Host 검사를 받지 않는다 — 컨테이너·프록시가 내부 주소로 부른다.

`?deep=true`는 로그인 없이 부를 수 있는데 DB·Neo4j에 질의를 일으킨다. 그래서 (1) 쿼리가 붙은 `/healthz`는
요청 제한에 들어가고(`core/ratelimit.py` — 쿼리 없는 `/healthz`만 예외) (2) 확인 결과를 몇 초 동안 다시 쓴다.
동시에 여러 요청이 와도 실제 확인은 한 번에 하나만 돌고, 나머지는 그 결과를 받는다.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import State

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 3.0
DEEP_CACHE_SECONDS = 5.0  # 깊은 확인 결과를 다시 쓰는 시간 — 배포 점검·모니터링에는 충분히 새롭다


def _now() -> float:
    """깊은 확인 캐시가 보는 시계 (테스트가 바꿔 끼운다)."""
    return time.monotonic()


class _DeepCheckCache:
    """앱 하나에 하나. 마지막 깊은 확인 결과와 그것을 믿을 수 있는 시각."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.checks: dict[str, str] | None = None
        self.fresh_until = 0.0


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


async def _run_deep_checks(state) -> dict[str, str]:  # noqa: ANN001
    checks: dict[str, str] = {}
    checks["postgres"] = await _check(_check_postgres(state)) if state.engine is not None else "skipped"
    checks["neo4j"] = await _check(state.graph_store.ping()) if state.graph_store is not None else "skipped"
    return checks


async def _deep_checks(request: Request, state) -> dict[str, str]:  # noqa: ANN001
    cache: _DeepCheckCache | None = getattr(request.app.state, "deep_health_cache", None)
    if cache is None:
        cache = request.app.state.deep_health_cache = _DeepCheckCache()
    async with cache.lock:  # 기다리던 요청은 앞선 요청이 방금 얻은 결과를 그대로 쓴다
        if cache.checks is None or _now() >= cache.fresh_until:
            cache.checks = await _run_deep_checks(state)
            cache.fresh_until = _now() + DEEP_CACHE_SECONDS
        return dict(cache.checks)


@router.get("/healthz")
async def healthz(request: Request, state: State, deep: bool = False) -> JSONResponse:
    body: dict[str, Any] = {"status": "ok"}
    if not state.settings.is_production:
        body.update(
            llm_provider=state.settings.llm_provider,
            llm_model=state.settings.llm_model if state.settings.llm_provider != "fake" else "fake-echo",
            prompt_version=state.settings.prompt_version,
            storage=state.storage,
            graph=state.graph_enabled,
        )
    if deep:
        checks = await _deep_checks(request, state)
        body["checks"] = checks
        if any(v.startswith("error") for v in checks.values()):
            body["status"] = "degraded"
            return JSONResponse(status_code=503, content=body)
    return JSONResponse(content=body)
