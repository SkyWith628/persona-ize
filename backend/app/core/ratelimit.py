"""IP 기준 요청 제한 (프로세스 메모리 — 워커 1개 전제. 워커가 여럿이면 각자 센다).

- 미끄러지는 1분 창: 어느 60초 구간을 잡아도 허용된 요청 수가 한도를 넘지 않는다. 거절된 요청은 세지 않는다.
- `RATE_LIMIT_PER_MINUTE`는 `/v1/*` 전체, `RATE_LIMIT_AUTH_PER_MINUTE`는 비밀번호를 확인·저장하는 경로
  (로그인·가입·토큰 발급·비밀번호 변경·계정 삭제)에 추가로 건다. 0이면 그 한도는 끈다.
  `/healthz`는 쿼리가 없을 때만 세지 않는다. `/healthz?deep=true`는 `/v1/*`와 같은 한도로 센다.
- 키는 클라이언트 주소다. IPv6는 /64로 묶는다(한 가입자가 주소를 2^64개 쓸 수 있다).
  프록시 뒤에서는 uvicorn `--proxy-headers`가 채운 주소를 쓴다 — 여기서 `X-Forwarded-For`를 직접 읽지 않는다.
- 창이 지난 키는 창마다 한 번 쓸어 낸다. 그래도 `max_keys`를 넘으면 오래된 키부터 버린다(메모리 상한).
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import deque
from collections.abc import Callable

WINDOW_SECONDS = 60.0
MAX_KEYS = 50_000
UNKNOWN_CLIENT = "unknown"

API_PREFIX = "/v1/"
# 비밀번호를 확인하거나 새로 저장하는 경로 — 추측 공격과 scrypt 연산 남용을 함께 막는다.
AUTH_LIMITED: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/register"),
        ("POST", "/v1/auth/token"),
        ("POST", "/v1/auth/password"),
        ("DELETE", "/v1/auth/me"),
    }
)


def client_key(host: str | None) -> str:
    """요청 제한에 쓰는 클라이언트 키. IPv4는 그대로, IPv6는 /64, IPv4-매핑 IPv6는 IPv4로."""
    if not host:
        return UNKNOWN_CLIENT
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host  # 테스트 클라이언트("testclient")나 유닉스 소켓 이름
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network((addr, 64), strict=False))
    return str(addr)


class SlidingWindowLimiter:
    """키마다 최근 `window` 초 동안 허용한 요청 시각을 기억한다 (키당 최대 `limit`개)."""

    def __init__(
        self,
        limit: int,
        window_seconds: float = WINDOW_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_KEYS,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit는 1 이상이어야 합니다 (0은 '끔'이므로 만들지 않는다).")
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep: float | None = None

    def __len__(self) -> int:
        return len(self._hits)

    def hit(self, key: str) -> float | None:
        """요청 1건을 센다. 허용이면 None, 한도에 닿았으면 다시 시도할 수 있을 때까지 남은 초."""
        now = self.clock()
        self._sweep_if_due(now)
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        cutoff = now - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return hits[0] + self.window - now
        hits.append(now)
        return None

    def sweep(self, now: float | None = None) -> int:
        """창을 벗어난 기록만 남은 키를 지운다. 그래도 너무 많으면 오래된 키부터 버린다. 지운 키 수."""
        now = self.clock() if now is None else now
        cutoff = now - self.window
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        removed = len(stale)
        overflow = len(self._hits) - self.max_keys
        if overflow > 0:  # dict는 넣은 순서를 지킨다 → 앞쪽이 가장 오래 전에 처음 본 키
            for key in list(self._hits)[:overflow]:
                del self._hits[key]
            removed += overflow
        self._last_sweep = now
        return removed

    def _sweep_if_due(self, now: float) -> None:
        if self._last_sweep is None:
            self._last_sweep = now
        elif now - self._last_sweep >= self.window or len(self._hits) > self.max_keys:
            self.sweep(now)


class RateLimiter:
    """`/v1/*` 전체 한도와 인증 경로 한도. `clock`을 바꿔 끼우면 테스트에서 시간을 직접 움직일 수 있다."""

    def __init__(
        self,
        per_minute: int,
        auth_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        exempt_paths: frozenset[str] = frozenset({"/healthz"}),
    ) -> None:
        self.clock = clock
        self.exempt_paths = exempt_paths
        self.general = SlidingWindowLimiter(per_minute, clock=self._now) if per_minute > 0 else None
        self.auth = SlidingWindowLimiter(auth_per_minute, clock=self._now) if auth_per_minute > 0 else None

    def _now(self) -> float:
        return self.clock()

    @property
    def enabled(self) -> bool:
        return self.general is not None or self.auth is not None

    def check(self, method: str, path: str, client_host: str | None, query: bytes = b"") -> int | None:
        """허용이면 None, 막혔으면 `Retry-After`에 넣을 초(1 이상, 올림).

        예외 경로(`/healthz`)는 **쿼리가 없을 때만** 세지 않는다 — 컨테이너·프록시 헬스체크가 그렇게 부른다.
        `/healthz?deep=true`는 DB·Neo4j에 실제로 붙으므로 `/v1/*` 요청과 같은 한도로 센다.
        """
        if path in self.exempt_paths:
            if not query:
                return None
        elif not path.startswith(API_PREFIX):
            return None
        key = client_key(client_host)
        if self.general is not None:
            wait = self.general.hit(key)
            if wait is not None:
                return _seconds(wait)
        if self.auth is not None and (method.upper(), path.rstrip("/")) in AUTH_LIMITED:
            wait = self.auth.hit(key)
            if wait is not None:
                return _seconds(wait)
        return None


def _seconds(wait: float) -> int:
    return max(math.ceil(wait), 1)
