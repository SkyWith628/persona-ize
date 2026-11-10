"""키별 잠금 (프로세스 안에서만 유효).

같은 대화방으로 들어온 가져오기, 같은 멱등 키의 첨삭 요청처럼 "같은 대상에 대한 일은 한 번에 하나씩"
해야 하는 곳에 쓴다. 서버 프로세스가 여럿이면 프로세스 사이는 막지 못한다 — 그때는 DB의 유일 제약이
마지막 방어선이다 (저장소가 충돌을 `VersionConflictError`로 바꾼다).

잠금 객체는 쓰는 동안만 사전에 남는다. 키가 끝없이 쌓이지 않고, 테스트처럼 이벤트 루프가 바뀌어도
옛 루프에 묶인 잠금이 남지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager


class KeyedLocks:
    def __init__(self) -> None:
        self._entries: dict[Hashable, list] = {}  # 키 → [잠금, 쓰는 중 + 기다리는 수]

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._entries.get(key) is entry:
                del self._entries[key]

    def busy(self, key: Hashable) -> bool:
        """지금 누가 이 키를 쥐고 있거나 기다리는가."""
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)
