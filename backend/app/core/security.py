"""비밀번호 해시와 토큰 생성 (project-design §7 인증).

- 비밀번호: 표준 라이브러리 `hashlib.scrypt` (의존성 없음). 형식 `scrypt$n$r$p$salt$hash` (base64).
  argon2가 더 낫지만 추가 패키지 없이 시작한다. 바꾸면 접두로 구분해 기존 해시도 검증한다.
- 세션 토큰: `secrets.token_urlsafe(32)`. DB에는 SHA-256 해시만 둔다 — DB가 새도 쿠키를 위조할 수 없게.
- 비교는 전부 `hmac.compare_digest` (타이밍 공격 방지).
- scrypt 한 번은 수십 ms의 CPU와 16MB 메모리를 쓴다. 요청 처리 중에는 `*_async`로 전용 스레드 풀에서 돌린다 —
  이벤트 루프를 막지 않고, 로그인 요청이 몰려도 동시에 도는 해시 수(=메모리)가 풀 크기로 묶인다.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
from concurrent.futures import ThreadPoolExecutor

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_LEN = 32

# hashlib.scrypt는 GIL을 놓으므로 코어 수만큼 병렬로 돈다. 스레드는 처음 쓸 때 만들어진다.
_HASH_WORKERS = max(2, min(4, os.cpu_count() or 2))
_hash_pool = ThreadPoolExecutor(max_workers=_HASH_WORKERS, thread_name_prefix="pz-scrypt")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


async def hash_password_async(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_hash_pool, hash_password, password)


async def verify_password_async(password: str, stored: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(_hash_pool, verify_password, password, stored)


def new_token() -> str:
    """쿠키에 넣는 세션 토큰 (원문). DB에는 `token_hash()`만 저장한다."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def tokens_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
