"""서버에서 계정 만들기·관리자 지정·비밀번호 재설정 — 가입을 닫았거나 승인제로 둔 배포용.

    cd backend && python -m app.tools.create_user <아이디> [--admin] [--reset-password]

- 인자 없이: 그 아이디로 계정을 만든다. 승인제여도 바로 쓸 수 있다(active). 가입이 닫혀 있어도 된다.
- `--admin`: 관리자(role=admin)로 만든다. 이미 있는 아이디면 비밀번호는 건드리지 않고 권한만 올린다.
- `--reset-password`: 이미 있는 아이디의 비밀번호를 새로 정한다 (그 계정의 세션·토큰은 모두 폐기).
  `--admin`과 함께 쓰면 재설정하고 권한도 올린다.

`design/iphone-mobile` c001cbb에서 옮겼다 (2026-11-16). `scripts/reset_password.py`(이름으로 지정하는
`ADMIN_USERNAMES` 관리자를 `--create`로 만드는 길)는 그대로 있다 — 이 도구는 **권한(role)** 으로
관리자를 만든다.

비밀번호는 명령 인자로도 환경변수로도 받지 않는다(셸 기록·프로세스 목록에 남는다). 터미널이면 화면에 보이지
않게 두 번 묻고, 파이프면 표준입력 첫 줄을 읽는다. 권한만 올릴 때는 비밀번호를 묻지 않는다.
종료 코드: 0 성공, 1 규칙 위반·없는 사용자·이미 있는 이름, 2 설정·입력 문제.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable
from typing import TextIO

from app.core.config import Settings
from app.core.errors import DomainError
from app.repositories.auth import AuthRepo
from app.services.auth_service import AuthService


class InputError(Exception):
    """비밀번호를 읽지 못했다 (빈 입력, 두 번 입력이 다름)."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.create_user", description="계정 만들기·관리자 지정·비밀번호 재설정"
    )
    parser.add_argument("username", help="아이디 (3~32자의 영문·숫자·_·-, 대소문자 구분)")
    parser.add_argument("--admin", action="store_true", help="관리자(role=admin)로 만들거나 올린다")
    parser.add_argument(
        "--reset-password", action="store_true", help="기존 계정의 비밀번호를 새로 정한다 (세션 전부 폐기)"
    )
    return parser.parse_args(argv)


def read_password(stdin: TextIO | None = None, prompt: Callable[[str], str] = getpass.getpass) -> str:
    """터미널이면 화면에 보이지 않게 두 번 묻고, 아니면 표준입력 첫 줄을 읽는다."""
    stdin = sys.stdin if stdin is None else stdin
    if stdin.isatty():
        first = prompt("새 비밀번호: ")
        if first != prompt("한 번 더: "):
            raise InputError("두 번 입력한 비밀번호가 서로 다릅니다.")
        password = first
    else:
        password = stdin.readline().rstrip("\r\n")
    if not password:
        raise InputError("비밀번호가 비어 있습니다.")
    return password


async def needs_password(repo: AuthRepo, username: str, *, reset_password: bool) -> bool:
    """비밀번호를 물어야 하는가 — 이미 있는 계정의 권한만 올릴 때는 묻지 않는다."""
    return reset_password or await repo.find_user_by_username(username.strip()) is None


async def apply(
    service: AuthService,
    repo: AuthRepo,
    username: str,
    password: str | None,
    *,
    admin: bool = False,
    reset_password: bool = False,
) -> str:
    """만들기·재설정·권한 올리기를 하고 운영자에게 보여 줄 한 줄을 돌려준다.

    규칙 위반·없는 사용자는 `DomainError`로 올라간다. 비밀번호는 어디에도 출력하지 않는다.
    """
    username = username.strip()
    exists = await repo.find_user_by_username(username) is not None
    if reset_password:
        revoked = await service.reset_password(username, password or "")
        role = (await service.set_role(username, "admin")).role if admin else "그대로"
        return f"비밀번호를 바꿨습니다: {username} (role={role}, 로그아웃된 세션 {revoked}개)"
    if exists:
        if not admin:
            raise DomainError("이미 있는 아이디입니다. 비밀번호를 바꾸려면 --reset-password를 쓰세요.")
        user = await service.set_role(username, "admin")
        return f"관리자로 올렸습니다: {user.username} (role={user.role})"
    user = await service.create_account(username, password or "", role="admin" if admin else "user")
    return f"계정을 만들었습니다: {user.username} (role={user.role}, status={user.status})"


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        print("DATABASE_URL이 비어 있습니다. 인메모리 저장소의 계정은 서버 프로세스 안에만 있습니다.")
        return 2

    # 계정 저장소만 만든다 — 그래프·임베딩·모델 클라이언트는 필요 없다.
    from app.db.session import create_engine, create_session_factory
    from app.repositories.auth import PostgresAuthRepo

    engine = create_engine(settings.database_url)
    repo = PostgresAuthRepo(create_session_factory(engine))
    try:
        password = None
        if await needs_password(repo, args.username, reset_password=args.reset_password):
            password = read_password()
        print(
            await apply(
                AuthService(settings, repo),
                repo,
                args.username,
                password,
                admin=args.admin,
                reset_password=args.reset_password,
            )
        )
    except InputError as exc:
        print(str(exc))
        return 2
    except DomainError as exc:
        print(f"실패: {exc.message}")
        return 1
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
