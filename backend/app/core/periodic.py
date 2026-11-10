"""서버 안에서 도는 주기 작업 (계약 §4.1).

`HOUSEKEEPING_INTERVAL_HOURS>0`이면 lifespan이 `housekeeping_loop`를 백그라운드 작업으로 띄운다. 무엇을
지울지는 `app.services.housekeeping`(data 담당)이 정한다 — 그 모듈이 `run_housekeeping(state)`를 제공하면
그것을, 없으면 기존 `Housekeeping` 클래스를 쓴다. 한 번의 실패는 로그만 남기고 서버는 계속 돈다.
cron으로 `scripts/housekeeping.py`를 돌리는 배포라면 이 설정은 0으로 둔다(둘 다 돌아도 해는 없다).

같은 주기에 그래프 미수집분도 이어서 넣는다(`resume_graph_once`). 하루 상한에 걸려 남은 메시지나 일시 장애로
실패한 수집이 다음 가져오기·서버 재시작을 기다리지 않게 하려는 것이다. 모델 비용이 드는 자동 작업이므로
`GRAPH_RESUME_ON_START`가 켜져 있을 때만 돌고, 수집량은 `GRAPH_INGEST_*_CAP` 상한 안에 머문다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

FIRST_DELAY_SECONDS = 60.0  # 시작 직후의 워밍업·마이그레이션과 겹치지 않게 잠깐 기다렸다가 첫 정리


async def run_housekeeping_once(state: Any) -> dict[str, int]:
    """정리 한 번. 지운 수를 이름별로 돌려준다. `state`는 `AppState`."""
    # 서비스 계층은 호출할 때 가져온다 (core 모듈이 import 시점에 services에 기대지 않게).
    from app.services import housekeeping

    runner = getattr(housekeeping, "run_housekeeping", None)
    if runner is not None:
        removed = {str(name): int(count) for name, count in dict(await runner(state)).items()}
    else:
        result = await housekeeping.Housekeeping(state.auth, state.conversations, state.usage).run(
            day_offset_hours=state.settings.usage_day_offset_hours
        )
        removed = {
            "sessions": result.sessions,
            "import_jobs": result.import_jobs,
            "usage_rows": result.usage_rows,
        }
    # 로그인 실패 기록은 프로세스 메모리에만 있다 — 창이 지난 키를 같이 비운다.
    prune = getattr(getattr(state, "auth_service", None), "prune_throttle", None)
    if prune is not None:
        removed["login_throttle_keys"] = int(prune())
    return removed


async def resume_graph_once(state: Any) -> dict[str, int]:
    """그래프 미수집 메시지를 이어서 넣는다. 사용자별로 넣은 수를 돌려준다 (그래프를 안 쓰면 빈 dict).

    상한에 닿은 사용자는 `GraphIngestService.resume_all`이 건너뛴다. 모델 호출이 드는 작업이라
    `GRAPH_RESUME_ON_START`로 함께 켜고 끈다.
    """
    ingest = getattr(state, "graph_ingest", None)
    settings = getattr(state, "settings", None)
    if ingest is None or settings is None or not settings.graph_resume_on_start:
        return {}
    return dict(await ingest.resume_all())


async def housekeeping_loop(
    state: Any,
    interval_seconds: float,
    *,
    first_delay: float = FIRST_DELAY_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """스스로 끝나지 않는다 — lifespan이 서버를 내릴 때 취소한다."""
    await sleep(min(first_delay, interval_seconds))
    while True:
        try:
            removed = await run_housekeeping_once(state)
            log.info("housekeeping: %s", ", ".join(f"{k}={v}" for k, v in removed.items()))
        except Exception:  # noqa: BLE001 - 정리 실패가 서버를 내리면 안 된다
            log.warning("housekeeping failed (다음 주기에 다시 시도)", exc_info=True)
        try:
            resumed = await resume_graph_once(state)
            if resumed:
                log.info("graph resume: %s", resumed)
        except Exception:  # noqa: BLE001 - 수집 실패도 서버를 내리지 않는다
            log.warning("graph resume failed (다음 주기에 다시 시도)", exc_info=True)
        await sleep(interval_seconds)
