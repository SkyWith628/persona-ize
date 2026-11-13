"""가져오기의 묶음 생성·임베딩을 요청 밖에서 끝내기 (2026-11-13).

메시지 저장은 요청 안에서 끝나고, 묶음 생성이 `index_wait` 안에 끝나지 않으면 `processing` 작업을 돌려준다.
나머지는 뒤에서 끝나 작업이 `completed`/`failed`로 닫힌다. `index_wait=None`(스크립트·평가)은 예전처럼
끝날 때까지 기다린다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.ai.embeddings import FakeEmbedder
from app.core.errors import NotFoundError
from app.domain.conversation import ImportJob, NewMessage
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService, source_hash

pytestmark = pytest.mark.anyio


def _file(k: int = 0, n: int = 40) -> ImportPayload:
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
    """임베딩에 시간이 걸리고, 시키면 실패한다."""

    def __init__(self, delay: float = 0.05) -> None:
        super().__init__(dim=32)
        self.delay = delay
        self.calls = 0
        self.fail = False

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("임베딩 장애 — 이 문장은 기록에 남으면 안 된다")
        return await super().embed_passages(texts)


async def _setup(embedder: FakeEmbedder, index_wait: float | None):  # noqa: ANN202
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    svc = ImportService(contacts, conversations, embedder, index_wait=index_wait)
    boss = await contacts.create("u", "박지현", "superior")
    conv = await svc.create_conversation("u", boss.id, "방", "json", "c1")
    return conversations, svc, conv


async def test_a_long_indexing_returns_processing_and_finishes_later() -> None:
    conversations, svc, conv = await _setup(SlowEmbedder(), index_wait=0.0)
    job = await svc.import_messages("u", conv.id, _file())
    # 메시지는 이미 저장됐고 묶음만 아직이다
    assert (job.status, job.imported, job.episodes) == ("processing", 40, 0)
    stats = await conversations.conversation_stats("u", conv.id)
    assert (stats.messages, stats.episodes) == (40, 0)
    assert (await conversations.get_job("u", job.id)).status == "processing"

    await svc.wait_graph_tasks()
    done = await conversations.get_job("u", job.id)
    assert (done.id, done.status, done.imported, done.episodes) == (job.id, "completed", 40, 20)
    assert (await conversations.conversation_stats("u", conv.id)).episodes == 20
    assert not svc._index_tasks and len(svc._conv_locks) == 0


async def test_a_short_indexing_still_returns_completed() -> None:
    _, svc, conv = await _setup(FakeEmbedder(dim=32), index_wait=5.0)
    job = await svc.import_messages("u", conv.id, _file())
    assert (job.status, job.imported, job.episodes) == ("completed", 40, 20)


async def test_without_a_wait_limit_the_caller_gets_the_finished_job() -> None:
    _, svc, conv = await _setup(SlowEmbedder(), index_wait=None)  # 스크립트·평가의 기본
    job = await svc.import_messages("u", conv.id, _file())
    assert (job.status, job.episodes) == ("completed", 20)


async def test_uploading_the_same_file_while_processing_joins_the_running_job() -> None:
    embedder = SlowEmbedder()
    conversations, svc, conv = await _setup(embedder, index_wait=0.0)
    first = await svc.import_messages("u", conv.id, _file())
    again = await svc.import_messages("u", conv.id, _file())
    assert (again.id, again.status, again.imported) == (first.id, "processing", 40)
    await svc.wait_graph_tasks()
    assert embedder.calls == 1  # 묶음은 한 번만 만든다
    done = await conversations.get_job("u", first.id)
    assert (done.status, done.imported, done.duplicates, done.episodes) == ("completed", 40, 0, 20)


async def test_a_failure_in_the_background_is_recorded_and_retried_by_uploading_again() -> None:
    embedder = SlowEmbedder(delay=0.01)
    embedder.fail = True
    conversations, svc, conv = await _setup(embedder, index_wait=0.0)
    job = await svc.import_messages("u", conv.id, _file())
    await svc.wait_graph_tasks()
    failed = await conversations.get_job("u", job.id)
    assert (failed.status, failed.error_code, failed.graph_status) == ("failed", "reindex_failed", "skipped")
    assert failed.error_message == "RuntimeError"  # 종류만 — 예외 문장은 남기지 않는다

    embedder.fail = False
    await svc.import_messages("u", conv.id, _file())
    await svc.wait_graph_tasks()
    retried = await conversations.get_job("u", job.id)
    assert (retried.status, retried.episodes, retried.error_code) == ("completed", 20, None)


async def test_resume_indexing_finishes_jobs_left_processing_by_a_dead_server() -> None:
    conversations, svc, conv = await _setup(FakeEmbedder(dim=32), index_wait=0.0)
    # 죽은 프로세스의 흔적: 메시지는 저장됐고 작업은 processing, 묶음은 없다
    base = datetime(2026, 10, 5, tzinfo=UTC)
    messages = [
        NewMessage(
            source_key=f"m{i}",
            speaker_role="me" if i % 2 else "contact",
            sent_at=base + timedelta(minutes=i),
            text=f"{i}번째 메시지입니다",
        )
        for i in range(10)
    ]
    await conversations.add_messages("u", conv.id, messages)
    left = ImportJob(
        id=uuid4(),
        user_id="u",
        conversation_id=conv.id,
        source_hash=source_hash(messages),
        status="processing",
        imported=10,
        created_at=datetime.now(UTC) - timedelta(days=400),
    )
    await conversations.save_job(left)
    assert await conversations.purge_jobs(datetime.now(UTC)) == 0  # 정리 작업이 지우지 않는다

    assert await svc.resume_indexing() == 1
    await svc.wait_graph_tasks()
    done = await conversations.get_job("u", left.id)
    assert (done.status, done.imported, done.episodes) == ("completed", 10, 5)
    assert await svc.resume_indexing() == 0  # 남은 것이 없다


async def test_deleting_the_conversation_while_processing_leaves_nothing_behind() -> None:
    conversations, svc, conv = await _setup(SlowEmbedder(), index_wait=0.0)
    job = await svc.import_messages("u", conv.id, _file())
    deleted = await svc.delete_conversation("u", conv.id)
    assert (deleted.messages, deleted.episodes) == (40, 0)  # 임베딩이 끝나기를 기다리지 않는다
    await svc.wait_graph_tasks()  # 뒤에서 돌던 묶음 생성은 조용히 끝난다
    with pytest.raises(NotFoundError):
        await conversations.get_job("u", job.id)
    with pytest.raises(NotFoundError):
        await conversations.get("u", conv.id)
    assert not svc._index_tasks
