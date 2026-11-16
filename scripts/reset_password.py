"""운영자용 비밀번호 재설정. 이메일이 없어 사용자가 스스로 재설정할 수 없으므로 서버에서 운영자가 한다.

무엇을 하나: 그 사용자의 비밀번호 해시를 새로 저장하고 **세션(쿠키·Bearer 토큰)을 전부 폐기**한다.
`--create`를 주면 계정이 없을 때 새로 만든다 — 가입이 닫혀 있거나 초대 코드가 있어도 된다
(운영을 시작할 때 `ADMIN_USERNAMES`의 관리자 계정을 남보다 먼저 만드는 데 쓴다).

새 비밀번호는 명령 인자로 받지 않는다(셸 기록에 남는다).
터미널이면 화면에 보이지 않게 두 번 묻고, 파이프면 표준입력 첫 줄을 읽는다.

사용 예 (저장소 루트, DATABASE_URL은 .env 또는 환경변수):
    .venv/Scripts/python.exe scripts/reset_password.py --username minsu
    printf '%s\n' "$NEW_PASSWORD" | python scripts/reset_password.py --username minsu
    python scripts/reset_password.py --username admin --create
컨테이너 안에서는 `docker compose exec backend python /path/to/scripts/reset_password.py --username …`.
이름은 대소문자를 구분한다. 종료 코드: 0 성공, 1 사용자 없음·비밀번호 규칙 위반, 2 설정·입력 문제.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.core.errors import DomainError  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402


class InputError(Exception):
    """비밀번호를 읽지 못했다 (빈 입력, 두 번 입력이 다름)."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="사용자 비밀번호 재설정 (세션 전부 폐기)")
    p.add_argument("--username", required=True, help="대상 사용자 이름 (대소문자 구분)")
    p.add_argument("--create", action="store_true", help="계정이 없으면 이 이름으로 새로 만든다")
    return p.parse_args(argv)


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


async def apply(service: AuthService, repo, username: str, password: str, *, create: bool = False) -> str:  # noqa: ANN001
    """재설정(또는 `create`이고 계정이 없으면 생성)하고 운영자에게 보여 줄 한 줄을 돌려준다.

    규칙 위반·없는 사용자는 `DomainError`로 올라간다. 비밀번호는 어디에도 출력하지 않는다.
    """
    username = username.strip()
    if create and await repo.find_user_by_username(username) is None:
        user = await service.create_account(username, password)
        return f"계정을 만들었습니다: {user.username}"
    revoked = await service.reset_password(username, password)
    return f"비밀번호를 바꿨습니다: {username} (로그아웃된 세션 {revoked}개)"


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        print("DATABASE_URL이 비어 있습니다. 인메모리 저장소의 계정은 서버 프로세스 안에만 있습니다.")
        return 2
    try:
        password = read_password()
    except InputError as exc:
        print(str(exc))
        return 2

    # 계정 저장소만 만든다 — 그래프·임베딩·모델 클라이언트는 필요 없다.
    from app.db.session import create_engine, create_session_factory
    from app.repositories.auth import PostgresAuthRepo

    engine = create_engine(settings.database_url)
    repo = PostgresAuthRepo(create_session_factory(engine))
    try:
        print(await apply(AuthService(settings, repo), repo, args.username, password, create=args.create))
    except DomainError as exc:
        print(f"실패: {exc.message}")
        return 1
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
