"""같은 대화방에 가져오기가 겹칠 때 (2026-11-11 정확성 검토 D5).

저장과 묶음 다시 만들기를 대화방 단위로 한 번에 하나씩 한다. 그전에는
- 서로 다른 파일 두 건이 겹치면 묶음 교체가 엇갈려 묶음이 두 벌 남거나(PostgreSQL) 늦게 끝난 쪽의
  낡은 묶음이 남았고,
- 같은 파일을 동시에 올리면 유일 제약에 걸려 한쪽이 500이었다.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.ai.embeddings import FakeEmbedder
from app.core.errors import NotFoundError, VersionConflictError
from app.domain.conversation import ImportJob
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService

pytestmark = pytest.mark.anyio


def _file(k: int, n: int = 40) -> ImportPayload:
    base = datetime.fromisoformat(f"2026-10-{5 + k:02d}T09:00:00+09:00")
    return ImportPayload(
        messages=[
            ImportMessage(
                id=f"f{k}-{i:03d}",
                speaker="me" if i % 2 else "contact",
                sent_at=base + timedelta(minutes=i),
                text=f"파일{k}의 {i}번째 메시지입니다",
            )
            for i in range(n)
        ]
    )


class SlowEmbedder(FakeEmbedder):
    """임베딩에 시간이 걸린다 — 먼저 시작한 가져오기가 더 늦게 끝나게 만들 수 있다."""

    def __init__(self, delays: list[float]) -> None:
        super().__init__(dim=32)
        self._delays = delays
        self.batches: list[int] = []

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        await asyncio.sleep(self._delays.pop(0) if self._delays else 0)
        return await super().embed_passages(texts)


async def _memory(embedder: FakeEmbedder | None = None):  # noqa: ANN202
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    svc = ImportService(contacts, conversations, embedder or FakeEmbedder(dim=32))
    boss = await contacts.create("u", "박지현", "superior")
    conv = await svc.create_conversation("u", boss.id, "방", "json", "c1")
    return conversations, svc, conv


async def test_overlapping_imports_leave_one_complete_set_of_episodes() -> None:
    # 첫 가져오기의 임베딩이 더 오래 걸린다: 잠금이 없으면 늦게 끝난 첫 작업이 낡은 묶음으로 덮어쓴다
    conversations, svc, conv = await _memory(SlowEmbedder([0.05, 0.0]))
    first, second = await asyncio.gather(
        svc.import_messages("u", conv.id, _file(0)), svc.import_messages("u", conv.id, _file(1))
    )
    assert (first.status, first.imported, second.status, second.imported) == (
        "completed",
        40,
        "completed",
        40,
    )
    stats = await conversations.conversation_stats("u", conv.id)
    assert (stats.messages, stats.my_messages) == (80, 40)
    assert stats.episodes == 40  # 내 발화 구간 하나에 묶음 하나 — 빠진 것도 겹친 것도 없다
    assert (first.episodes, second.episodes) == (20, 40)  # 차례대로 끝났다
    assert len(svc._conv_locks) == 0  # 잠금은 쓰는 동안만 남는다


async def test_the_same_file_uploaded_at_once_is_stored_once() -> None:
    conversations, svc, conv = await _memory(SlowEmbedder([0.02, 0.0, 0.0]))
    jobs = await asyncio.gather(*(svc.import_messages("u", conv.id, _file(0)) for _ in range(3)))
    assert len({j.id for j in jobs}) == 1  # 뒤의 두 요청은 먼저 끝난 작업을 돌려받는다
    assert [j.imported for j in jobs] == [40, 40, 40]
    assert (await conversations.conversation_stats("u", conv.id)).messages == 40
    assert svc._embedder.batches == [20]  # 임베딩도 한 번만


async def test_deleting_a_conversation_waits_for_the_import_in_progress() -> None:
    conversations, svc, conv = await _memory(SlowEmbedder([0.05]))
    importing = asyncio.create_task(svc.import_messages("u", conv.id, _file(0)))
    await asyncio.sleep(0.01)  # 가져오기가 묶음을 만드는 중
    deleted = await svc.delete_conversation("u", conv.id)
    job = await importing
    # 가져오기가 끝난 뒤에 지워진다: 반쯤 저장된 채 지워지거나, 지운 방에 묶음이 다시 생기지 않는다
    assert (job.status, job.imported) == ("completed", 40)
    assert (deleted.messages, deleted.episodes) == (40, 20)
    with pytest.raises(NotFoundError):
        await conversations.get("u", conv.id)
    with pytest.raises(NotFoundError):
        await svc.import_messages("u", conv.id, _file(1))


# ---- PostgreSQL: 실제로 요청이 엇갈린다 ------------------------------------------------------------


@pytest.fixture
async def pg(pg_url: str):  # noqa: ANN201
    from app.db.session import create_engine, create_session_factory
    from app.repositories.postgres import PostgresContactRepo, PostgresConversationRepo

    engine = create_engine(pg_url)
    sessions = create_session_factory(engine)
    try:
        contacts, conversations = PostgresContactRepo(sessions), PostgresConversationRepo(sessions)
        svc = ImportService(contacts, conversations, FakeEmbedder(dim=384))
        user = f"race-{uuid.uuid4().hex[:8]}"
        boss = await contacts.create(user, "박지현", "superior")
        conv = await svc.create_conversation(user, boss.id, "방", "json", f"c-{uuid.uuid4().hex[:6]}")
        yield conversations, svc, user, conv
    finally:
        await engine.dispose()


async def test_concurrent_imports_do_not_duplicate_episodes_on_postgres(pg) -> None:  # noqa: ANN001
    conversations, svc, user, conv = pg
    jobs = await asyncio.gather(*(svc.import_messages(user, conv.id, _file(k)) for k in range(4)))
    assert [(j.status, j.imported) for j in jobs] == [("completed", 40)] * 4
    stats = await conversations.conversation_stats(user, conv.id)
    assert (stats.messages, stats.my_messages, stats.episodes) == (160, 80, 80)  # 예전: 묶음이 여러 벌


async def test_same_file_three_times_at_once_on_postgres(pg) -> None:  # noqa: ANN001
    conversations, svc, user, conv = pg
    jobs = await asyncio.gather(
        *(svc.import_messages(user, conv.id, _file(0)) for _ in range(3)), return_exceptions=True
    )
    assert not any(isinstance(j, Exception) for j in jobs), jobs  # 예전: 한쪽이 IntegrityError(500)
    assert len({j.id for j in jobs}) == 1
    assert (await conversations.conversation_stats(user, conv.id)).messages == 40


async def test_a_conflict_with_another_server_process_returns_the_existing_job(pg) -> None:  # noqa: ANN001
    """프로세스가 여럿이면 잠금이 서로를 막지 못한다 — 유일 제약 충돌을 500 대신 기존 작업으로 돌려준다."""
    conversations, svc, user, conv = pg
    first = await svc.import_messages(user, conv.id, _file(0))

    # 다른 프로세스가 방금 저장했다고 치자: 이 요청이 처음 찾았을 때는 작업이 아직 없었다
    real_find = conversations.find_job
    calls = {"n": 0}

    async def find_late(user_id, conversation_id, digest):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_find(user_id, conversation_id, digest)

    conversations.find_job = find_late  # type: ignore[method-assign]
    again = await svc.import_messages(user, conv.id, _file(0))
    assert again.id == first.id and calls["n"] == 2
    assert (await conversations.conversation_stats(user, conv.id)).messages == 40

    # 저장소 수준: 같은 (사용자, 대화방, 내용 해시)의 다른 작업 → 409, 지워진 대화방의 작업 → 404
    def job(conversation_id: uuid.UUID, digest: str) -> ImportJob:
        return ImportJob(
            id=uuid.uuid4(),
            user_id=user,
            conversation_id=conversation_id,
            source_hash=digest,
            status="completed",
            created_at=datetime.now(UTC),
        )

    with pytest.raises(VersionConflictError):
        await conversations.save_job(job(conv.id, first.source_hash))
    with pytest.raises(NotFoundError):
        await conversations.save_job(job(uuid.uuid4(), "d" * 64))
