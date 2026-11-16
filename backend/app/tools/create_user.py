"""서버에서 계정 만들기·비밀번호 재설정 — 가입을 닫았거나 승인제로 둔 배포용.

    python -m app.tools.create_user <아이디> [--admin] [--reset-password]

- `--admin`: 관리자(role=admin)로 만든다. `--reset-password`와 함께 쓰면 기존 계정을 관리자로 올린다.
- `--reset-password`: 이미 있는 아이디의 비밀번호를 새로 정한다 (그 계정의 세션은 모두 로그아웃).

여기서 만든 계정은 승인 대기 없이 바로 쓸 수 있다(active).
비밀번호는 명령줄에 쓰지 않는다(프로세스 목록·셸 기록에 남는다). 물어보는 대로 두 번 입력하거나,
자동화할 때만 환경변수 `NEW_USER_PASSWORD`로 넘긴다.
"""

from __future__ import annotations

import argparse
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


async def run(username: str, password: str, *, admin: bool = False, reset_password: bool = False) -> int:
    settings = Settings()
    if not settings.database_url:
        print("DATABASE_URL이 비어 있습니다. 인메모리 저장소에는 계정이 남지 않습니다.")
        return 2
    # 계정만 다루므로 그래프 연결은 만들지 않는다.
    state = build_state(settings.model_copy(update={"neo4j_uri": ""}))
    try:
        if reset_password:
            user = await state.auth_service.reset_password(username, password, make_admin=admin)
        else:
            user = await state.auth_service.create_account(
                username, password, role="admin" if admin else "user"
            )
    except DomainError as exc:
        print(f"{'바꾸지' if reset_password else '만들지'} 못했습니다: {exc.message}")
        return 1
    finally:
        await state.close()
    done = "비밀번호를 바꿨습니다" if reset_password else "계정을 만들었습니다"
    print(f"{done}: {user.username} (role={user.role})")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.tools.create_user", description=__doc__)
    parser.add_argument("username", help="아이디 (3~32자의 영문·숫자·_·-)")
    parser.add_argument("--admin", action="store_true", help="관리자(role=admin)로 만든다")
    parser.add_argument("--reset-password", action="store_true", help="기존 계정의 비밀번호를 새로 정한다")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    if len(argv) < 2:  # noqa: PLR2004
        print(__doc__)
        return 2
    args = parse_args(argv[1:])
    return asyncio.run(
        run(args.username, read_password(), admin=args.admin, reset_password=args.reset_password)
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
