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

    async def delete_user(self, user_id: str) -> None: ...

    async def purge_before(self, day: date) -> int:
        """day 이전(미포함) 날짜의 카운터를 지운다. 지운 수."""
        ...

    async def totals(self, day: date) -> UsageCounter:
        """그날 모든 사용자의 합계 (전체 차단기용). user_id는 '*'."""
        ...


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

    async def delete_user(self, user_id: str) -> None:
        for key in [k for k in self._rows if k[0] == user_id]:
            del self._rows[key]

    async def purge_before(self, day: date) -> int:
        gone = [k for k in self._rows if k[1] < day]
        for k in gone:
            del self._rows[k]
        return len(gone)

    async def totals(self, day: date) -> UsageCounter:
        rows = [r for (_, d), r in self._rows.items() if d == day]
        return UsageCounter(
            user_id="*",
            day=day,
            rewrites=sum(r.rewrites for r in rows),
            input_tokens=sum(r.input_tokens for r in rows),
            output_tokens=sum(r.output_tokens for r in rows),
        )


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
            graph_episodes=row.graph_episodes,
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

    async def delete_user(self, user_id: str) -> None:
        from sqlalchemy import delete

        from app.db.models import UsageCounterRow

        async with self._sessions.begin() as s:
            await s.execute(delete(UsageCounterRow).where(UsageCounterRow.user_id == user_id))

    async def purge_before(self, day: date) -> int:
        from sqlalchemy import delete

        from app.db.models import UsageCounterRow

        async with self._sessions.begin() as s:
            result = await s.execute(delete(UsageCounterRow).where(UsageCounterRow.day < day))
        return int(result.rowcount or 0)

    async def totals(self, day: date) -> UsageCounter:
        from sqlalchemy import func, select

        from app.db.models import UsageCounterRow

        stmt = select(
            func.coalesce(func.sum(UsageCounterRow.rewrites), 0),
            func.coalesce(func.sum(UsageCounterRow.input_tokens), 0),
            func.coalesce(func.sum(UsageCounterRow.output_tokens), 0),
        ).where(UsageCounterRow.day == day)
        async with self._sessions() as s:
            rewrites, input_tokens, output_tokens = (await s.execute(stmt)).one()
        return UsageCounter(
            user_id="*",
            day=day,
            rewrites=int(rewrites),
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
        )
