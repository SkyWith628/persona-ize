"""서버에서 계정 만들기·관리자 지정·비밀번호 재설정 — 가입을 닫았거나 승인제로 둔 배포용.

    cd backend && python -m app.tools.create_user <아이디> [--admin] [--reset-password]

- 인자 없이: 그 아이디로 계정을 만든다. 승인제여도 바로 쓸 수 있다(active). 가입이 닫혀 있어도 된다.
- `--admin`: 관리자(role=admin)로 만든다. 이미 있는 아이디면 비밀번호는 건드리지 않고 권한만 올린다.
- `--reset-password`: 이미 있는 아이디의 비밀번호를 새로 정한다 (그 계정의 세션·토큰은 모두 폐기).
  `--admin`과 함께 쓰면 재설정하고 권한도 올린다.

`design/iphone-mobile` c001cbb에서 옮겼다 (2026-11-16). `scripts/reset_password.py`(이름으로 지정하는
`ADMIN_USERNAMES` 관리자를 `--create`로 만드는 길)는 그대로 있다 — 이 도구는 **권한(role)** 으로
관리자를 만든다.

비밀번호는 명령 인자로도 환경변수로도 받지 않는다(셸 기록·프로세스 목록에 남는다). 권한만 올릴 때는
비밀번호를 묻지 않는다. 읽는 길은 표준입력이 무엇이냐로 정한다 (`read_password`):
- 콘솔(터미널)이면 `getpass`로 화면에 보이지 않게 두 번 묻는다.
- 파이프·리다이렉트면 표준입력 첫 줄을 읽는다 (`printf '%s\n' "$PW" | python -m app.tools.create_user …`).
- Git Bash(mintty)처럼 **콘솔 없이 파이프로 터미널을 흉내 내는 창**에서는 묻지 않고 끝낸다(종료 코드 2).
  그런 창은 파이썬에게 "터미널이 아님"으로 보여서, 예전 코드는 표준입력을 그냥 읽었고 치는 글자가 화면에
  그대로 보였다(2026-11-17 보안 검토). 입력을 가릴 방법이 없으므로 `winpty python -m …`(콘솔을 붙여 준다)
  이나 PowerShell·cmd에서 실행하거나, 표준입력으로 넘기라고 안내한다.
종료 코드: 0 성공, 1 규칙 위반·없는 사용자·이미 있는 이름, 2 설정·입력 문제.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
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


# MSYS2·Cygwin 터미널(mintty)이 자식 프로세스에 넘기는 표준입력 파이프의 이름:
#   \\msys-<16진수>-pty<번호>-from-master  (Git for Windows도 같다. Cygwin은 cygwin-…)
# 셸 파이프라인(`printf … | python`)의 파이프는 `…-pipe-0x…`라 여기에 맞지 않는다.
_TERMINAL_PIPE_RE = re.compile(r"(?:msys|cygwin)-[0-9a-f]+-pty[0-9]+-(?:from|to)-master")
_FILE_TYPE_PIPE = 3
_FILE_NAME_INFO = 2  # GetFileInformationByHandleEx의 FileNameInfo
_MAX_PIPE_NAME = 1024


def is_terminal_pipe(stream: TextIO) -> bool:
    """표준입력이 "터미널 창에 연결된 파이프"인가 — 콘솔은 아닌데 사람이 치는 글자가 들어오는 곳.

    Windows의 mintty(Git Bash 기본 창)만 해당한다. 그 밖의 운영체제, 진짜 파이프·파일, 알아낼 수 없는
    경우는 False다(그러면 예전처럼 표준입력을 읽는다).
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class FileNameInfo(ctypes.Structure):
            _fields_ = (("length", wintypes.DWORD), ("name", wintypes.WCHAR * _MAX_PIPE_NAME))

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileType.argtypes = (wintypes.HANDLE,)
        kernel32.GetFileType.restype = wintypes.DWORD
        kernel32.GetFileInformationByHandleEx.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        if kernel32.GetFileType(handle) != _FILE_TYPE_PIPE:
            return False
        info = FileNameInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle, _FILE_NAME_INFO, ctypes.byref(info), ctypes.sizeof(info)
        ):
            return False
        # length는 바이트 수(UTF-16) — 끝에 NUL이 붙는다는 보장이 없어 길이만큼만 읽는다.
        name = ctypes.wstring_at(ctypes.addressof(info) + FileNameInfo.name.offset, info.length // 2)
    except (OSError, ValueError, AttributeError, ImportError):  # fileno 없음, 닫힌 스트림, 다른 플랫폼
        return False
    return _TERMINAL_PIPE_RE.search(name) is not None


def is_console(stream: TextIO) -> bool:
    """사람이 치는 콘솔(터미널)인가 — `getpass`가 글자를 가리고 받을 수 있는 곳.

    Windows에서는 NUL 장치(`< NUL`, 예약 작업·CI)도 `isatty()`가 참이다(문자 장치라서). 그대로 믿으면
    `getpass`가 오지 않을 키 입력을 끝없이 기다리므로, 진짜 콘솔 핸들인지(`GetConsoleMode`) 한 번 더 본다.
    """
    if not stream.isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        return bool(kernel32.GetConsoleMode(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError, ImportError):  # 핸들을 알 수 없으면 isatty()를 믿는다
        return True


def read_password(
    stdin: TextIO | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
    terminal_pipe: Callable[[TextIO], bool] = is_terminal_pipe,
) -> str:
    """콘솔이면 화면에 보이지 않게 두 번 묻고, 파이프·리다이렉트면 표준입력 첫 줄을 읽는다.

    입력을 가릴 수 없는 터미널 창(`is_terminal_pipe`)에서는 읽지 않고 `InputError` — 치는 비밀번호가 화면에
    보이는 채로 받지 않는다. 명령 인자·환경변수로는 어떤 경우에도 받지 않는다.
    """
    stdin = sys.stdin if stdin is None else stdin
    if is_console(stdin):
        first = prompt("새 비밀번호: ")
        if first != prompt("한 번 더: "):
            raise InputError("두 번 입력한 비밀번호가 서로 다릅니다.")
        password = first
    elif terminal_pipe(stdin):
        raise InputError(
            "이 터미널 창(Git Bash·mintty)에서는 입력한 비밀번호를 가릴 수 없어 받지 않습니다.\n"
            "  - `winpty python -m app.tools.create_user …`로 실행하거나 PowerShell·cmd에서 실행하세요.\n"
            "  - 또는 표준입력으로 넘기세요: `python -m app.tools.create_user … < 비밀번호파일`"
        )
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
