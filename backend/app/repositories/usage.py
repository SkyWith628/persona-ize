"""사용량 카운터 저장소 (인메모리 / PostgreSQL).

`reserve_rewrite`는 원자적이어야 한다 — 같은 사용자의 동시 요청이 한도를 함께 넘지 않게.
PostgreSQL은 `INSERT ... ON CONFLICT DO UPDATE ... WHERE rewrites < :limit RETURNING` 한 문장으로 처리한다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol

from app.domain.usage import UsageCounter


class UsageRepo(Protocol):
    async def get(self, user_id: str, day: date) -> UsageCounter: ...

    async def reserve_rewrite(self, user_id: str, day: date, limit: int) -> UsageCounter | None:
        """요청 수를 1 올린다. 이미 한도면 올리지 않고 None."""
        ...

    async def add_tokens(
        self, user_id: str, day: date, input_tokens: int, output_tokens: int
    ) -> UsageCounter: ...


class InMemoryUsageRepo:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, date], UsageCounter] = {}

    def _row(self, user_id: str, day: date) -> UsageCounter:
        return self._rows.setdefault((user_id, day), UsageCounter(user_id=user_id, day=day))

    async def get(self, user_id: str, day: date) -> UsageCounter:
        return self._row(user_id, day).model_copy()

    async def reserve_rewrite(self, user_id: str, day: date, limit: int) -> UsageCounter | None:
        row = self._row(user_id, day)
        if row.rewrites >= limit:
            return None
        row.rewrites += 1
        return row.model_copy()

    async def add_tokens(
        self, user_id: str, day: date, input_tokens: int, output_tokens: int
    ) -> UsageCounter:
        row = self._row(user_id, day)
        row.input_tokens += input_tokens
        row.output_tokens += output_tokens
        return row.model_copy()


class PostgresUsageRepo:
    def __init__(self, sessions) -> None:  # noqa: ANN001 - SessionFactory (db extra)
        self._sessions = sessions

    @staticmethod
    def _counter(row) -> UsageCounter:  # noqa: ANN001
        return UsageCounter(
            user_id=row.user_id,
            day=row.day,
            rewrites=row.rewrites,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
        )

    async def get(self, user_id: str, day: date) -> UsageCounter:
        from sqlalchemy import select

        from app.db.models import UsageCounterRow

        async with self._sessions() as s:
            row = await s.scalar(
                select(UsageCounterRow).where(UsageCounterRow.user_id == user_id, UsageCounterRow.day == day)
            )
        return self._counter(row) if row is not None else UsageCounter(user_id=user_id, day=day)

    async def reserve_rewrite(self, user_id: str, day: date, limit: int) -> UsageCounter | None:
        from sqlalchemy.dialects.postgresql import insert

        from app.db.models import UsageCounterRow

        now = datetime.now(UTC)
        stmt = (
            insert(UsageCounterRow)
            .values(user_id=user_id, day=day, rewrites=1, input_tokens=0, output_tokens=0, updated_at=now)
            .on_conflict_do_update(
                index_elements=[UsageCounterRow.user_id, UsageCounterRow.day],
                set_={"rewrites": UsageCounterRow.rewrites + 1, "updated_at": now},
                where=UsageCounterRow.rewrites < limit,
            )
            .returning(UsageCounterRow)
        )
        async with self._sessions.begin() as s:
            row = (await s.execute(stmt)).scalar_one_or_none()
        # 한도에 걸리면 DO UPDATE의 WHERE가 거짓 → 아무 행도 돌아오지 않는다.
        if row is None:
            return None
        if limit <= 0:
            return None
        return self._counter(row)

    async def add_tokens(
        self, user_id: str, day: date, input_tokens: int, output_tokens: int
    ) -> UsageCounter:
        from sqlalchemy.dialects.postgresql import insert

        from app.db.models import UsageCounterRow

        now = datetime.now(UTC)
        stmt = (
            insert(UsageCounterRow)
            .values(
                user_id=user_id,
                day=day,
                rewrites=0,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[UsageCounterRow.user_id, UsageCounterRow.day],
                set_={
                    "input_tokens": UsageCounterRow.input_tokens + input_tokens,
                    "output_tokens": UsageCounterRow.output_tokens + output_tokens,
                    "updated_at": now,
                },
            )
            .returning(UsageCounterRow)
        )
        async with self._sessions.begin() as s:
            row = (await s.execute(stmt)).scalar_one()
        return self._counter(row)
