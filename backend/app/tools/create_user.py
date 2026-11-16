"""서버에서 계정 만들기 — 가입(`AUTH_ALLOW_REGISTER`)을 닫아 둔 배포용.

    python -m app.tools.create_user <아이디>

비밀번호는 명령줄에 쓰지 않는다(프로세스 목록·셸 기록에 남는다). 물어보는 대로 두 번 입력하거나,
자동화할 때만 환경변수 `NEW_USER_PASSWORD`로 넘긴다.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys

from app.api.deps import build_state
from app.core.config import Settings
from app.core.errors import DomainError


def read_password() -> str:
    from_env = os.environ.get("NEW_USER_PASSWORD")
    if from_env:
        return from_env
    first = getpass.getpass("비밀번호: ")
    if first != getpass.getpass("비밀번호 확인: "):
        raise SystemExit("두 비밀번호가 다릅니다.")
    return first


async def run(username: str, password: str) -> int:
    settings = Settings()
    if not settings.database_url:
        print("DATABASE_URL이 비어 있습니다. 인메모리 저장소에는 계정이 남지 않습니다.")
        return 2
    # 계정만 다루므로 그래프 연결은 만들지 않는다.
    state = build_state(settings.model_copy(update={"neo4j_uri": ""}))
    try:
        user = await state.auth_service.create_account(username, password)
    except DomainError as exc:
        print(f"만들지 못했습니다: {exc.message}")
        return 1
    finally:
        await state.close()
    print(f"계정을 만들었습니다: {user.username}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    return asyncio.run(run(argv[1], read_password()))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
