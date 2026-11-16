"""테스트 컨테이너 진입점 (docker-compose.yml의 `test` 서비스).

pytest를 돌리기 전에 `TEST_DATABASE_URL`이 가리키는 DB가 없으면 만든다. 통합 테스트는 그 DB가 미리
있어야 하고(`backend/tests/pg_support.py`), 없으면 실패가 아니라 조용히 건너뛰기 때문이다.
그 뒤 받은 명령을 그대로 실행한다. pytest가 아닌 명령이면 DB를 건드리지 않는다.

    docker compose --profile test run --rm test                      # pytest -q -rs
    docker compose --profile test run --rm test pytest -q -k auth
    docker compose --profile test run --rm --no-deps test ruff check /repo
"""

from __future__ import annotations

import asyncio
import os
import sys

RETRIES = 10


async def _ensure(url: str) -> str | None:
    """DB가 없으면 만든다. 만들었으면 이름을, 이미 있으면 None을 돌려준다."""
    import asyncpg

    dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    server, _, name = dsn.rpartition("/")
    name = name.split("?")[0]
    if not name.endswith("_test"):
        raise SystemExit("TEST_DATABASE_URL의 DB 이름은 _test로 끝나야 합니다 (테스트가 스키마를 지웁니다).")
    conn = await asyncpg.connect(f"{server}/postgres", timeout=5)
    try:
        if await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            return None
        await conn.execute(f'CREATE DATABASE "{name}"')
        return name
    finally:
        await conn.close()


def ensure_test_db(url: str) -> None:
    last: Exception | None = None
    for _ in range(RETRIES):
        try:
            created = asyncio.run(_ensure(url))
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - PostgreSQL이 막 뜨는 중일 수 있다 → 잠깐 뒤 다시
            last = exc
            asyncio.run(asyncio.sleep(1))
            continue
        if created:
            print(f"[test] 테스트 DB를 만들었습니다: {created}", file=sys.stderr)
        return
    raise SystemExit(f"[test] 테스트 DB를 준비하지 못했습니다 ({type(last).__name__}: {last})")


def main() -> None:
    command = sys.argv[1:] or ["pytest", "-q", "-rs", "-p", "no:cacheprovider"]
    url = os.environ.get("TEST_DATABASE_URL", "")
    if url and any("pytest" in part for part in command[:3]):
        ensure_test_db(url)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
