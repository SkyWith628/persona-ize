"""가져오기의 말투 분석(묶음 임베딩) 진행률 (2026-11-14).

임베딩을 `EMBED_BATCH`개씩 나눠 돌리고, 배치가 끝날 때마다 "전체 묶음 N개 중 M개"를 프로세스 메모리에 적는다.
화면이 이미 읽는 `GET /v1/conversations/{id}/stats`의 `latest_job`과 `GET /v1/imports/{id}`에
`index_done`·`index_total`로 실려 나간다. 서버가 모르면(끝난 작업, 재시작 직후) 둘 다 null이다.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai.embeddings import FakeEmbedder
from app.core.config import Settings
from app.main import create_app
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import EMBED_BATCH, ImportMessage, ImportPayload, ImportService


def _messages(k: int, n: int) -> list[dict[str, str]]:
    """나와 상대가 번갈아 말한다 — 메시지 n개가 묶음 n/2개가 된다."""
    base = datetime.fromisoformat(f"2026-10-{5 + k:02d}T09:00:00+09:00")
    return [
        {
            "id": f"f{k}-{i:04d}",
            "speaker": "me" if i % 2 else "contact",
            "sent_at": (base + timedelta(minutes=i)).isoformat(),
            "text": f"파일{k}의 {i}번째 메시지입니다",
        }
        for i in range(n)
    ]


def _file(k: int, n: int) -> ImportPayload:
    return ImportPayload(messages=[ImportMessage(**m) for m in _messages(k, n)])  # type: ignore[arg-type]


class GatedEmbedder(FakeEmbedder):
    """배치마다 허락(`permits`)을 기다린다 — 테스트가 임베딩을 한 배치씩 넘긴다."""

    def __init__(self) -> None:
        super().__init__(dim=32)
        self.permits = 0
        self.sizes: list[int] = []
        self.fail = False

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        turn = len(self.sizes)
        self.sizes.append(len(texts))
        while self.permits <= turn:
            await asyncio.sleep(0.002)
        if self.fail:
            raise RuntimeError("임베딩 장애 — 이 문장은 기록에 남으면 안 된다")
        return await super().embed_passages(texts)


async def _setup(embedder: FakeEmbedder, index_wait: float | None = 0.0):  # noqa: ANN202
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    svc = ImportService(contacts, conversations, embedder, index_wait=index_wait)
    boss = await contacts.create("u", "박지현", "superior")
    conv = await svc.create_conversation("u", boss.id, "방", "json", "c1")
    return conversations, svc, conv


async def _until(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "기다리던 상태가 오지 않았다"
        await asyncio.sleep(0.002)


@pytest.mark.anyio
async def test_progress_counts_episodes_batch_by_batch_and_is_forgotten_when_done() -> None:
    embedder = GatedEmbedder()
    conversations, svc, conv = await _setup(embedder)
    job = await svc.import_messages("u", conv.id, _file(0, 300))
    assert (job.status, job.episodes) == ("processing", 0)

    await _until(lambda: svc.index_progress(job.id) is not None)
    assert svc.index_progress(job.id) == (0, 150)  # 묶음을 세고 나면 전체 수를 안다
    embedder.permits = 1
    await _until(lambda: svc.index_progress(job.id) == (EMBED_BATCH, 150))
    embedder.permits = 2
    await _until(lambda: svc.index_progress(job.id) == (2 * EMBED_BATCH, 150))
    embedder.permits = 99
    await svc.wait_graph_tasks()

    assert embedder.sizes == [64, 64, 22]
    assert svc.index_progress(job.id) is None and not svc._index_progress  # 끝난 작업은 기억하지 않는다
    done = await conversations.get_job("u", job.id)
    assert (done.status, done.episodes) == ("completed", 150)


@pytest.mark.anyio
async def test_progress_starts_from_the_vectors_that_are_reused() -> None:
    embedder = GatedEmbedder()
    embedder.permits = 99
    _, svc, conv = await _setup(embedder, index_wait=None)
    assert (await svc.import_messages("u", conv.id, _file(0, 300))).episodes == 150

    embedder.permits, embedder.sizes = 0, []
    svc._index_wait = 0.0
    job = await svc.import_messages("u", conv.id, _file(1, 200))  # 묶음 100개가 더 생긴다
    await _until(lambda: svc.index_progress(job.id) is not None)
    assert svc.index_progress(job.id) == (150, 250)  # 본문이 그대로인 묶음은 이미 끝난 것으로 센다
    embedder.permits = 99
    await svc.wait_graph_tasks()
    assert embedder.sizes == [64, 36]


@pytest.mark.anyio
async def test_a_small_import_is_embedded_in_one_call_in_the_original_order() -> None:
    seen: list[list[str]] = []

    class Recording(FakeEmbedder):
        async def embed_passages(self, texts: list[str]) -> list[list[float]]:
            seen.append(list(texts))
            return await super().embed_passages(texts)

    conversations, svc, conv = await _setup(Recording(dim=32), index_wait=None)
    payload = ImportPayload(
        messages=[
            ImportMessage(
                id=f"m{i}",
                speaker="me" if i % 2 else "contact",
                sent_at=datetime.fromisoformat("2026-10-05T09:00:00+09:00") + timedelta(minutes=i),
                text="가" * (40 - i),  # 뒤로 갈수록 짧다 — 길이순으로 다시 줄 세우면 순서가 뒤집힌다
            )
            for i in range(40)
        ]
    )
    job = await svc.import_messages("u", conv.id, payload)
    assert job.episodes == 20 and len(seen) == 1
    stored = [key.split("\n", 1)[1] for key in await conversations.episode_vectors("u", conv.id)]
    assert len(stored) == 20 and seen[0] == stored  # 한 배치면 예전과 같은 호출 (묶음 순서 그대로)


@pytest.mark.anyio
async def test_large_batches_are_cut_by_length_and_every_episode_gets_its_own_vector() -> None:
    embedder = FakeEmbedder(dim=32)
    conversations, svc, conv = await _setup(embedder, index_wait=None)
    payload = ImportPayload(
        messages=[
            ImportMessage(
                id=f"m{i}",
                speaker="me" if i % 2 else "contact",
                sent_at=datetime.fromisoformat("2026-10-05T09:00:00+09:00") + timedelta(minutes=i),
                text=f"{i}번 " + "나" * (i * 7 % 23),
            )
            for i in range(400)
        ]
    )
    job = await svc.import_messages("u", conv.id, payload)
    assert job.episodes == 200
    stored = await conversations.episode_vectors("u", conv.id)
    assert len(stored) == 200
    for key, vector in stored.items():  # 줄 세워 나눠도 본문과 벡터의 짝이 어긋나지 않는다
        assert vector == embedder._encode_one(key.split("\n", 1)[1])


@pytest.mark.anyio
async def test_progress_is_forgotten_when_indexing_fails() -> None:
    embedder = GatedEmbedder()
    embedder.fail = True
    conversations, svc, conv = await _setup(embedder)
    job = await svc.import_messages("u", conv.id, _file(0, 300))
    await _until(lambda: svc.index_progress(job.id) == (0, 150))
    embedder.permits = 99
    await svc.wait_graph_tasks()
    assert (await conversations.get_job("u", job.id)).status == "failed"
    assert svc.index_progress(job.id) is None and not svc._index_progress


@pytest.mark.anyio
async def test_unknown_jobs_have_no_progress() -> None:
    _, svc, conv = await _setup(FakeEmbedder(dim=32), index_wait=None)
    job = await svc.import_messages("u", conv.id, _file(0, 40))
    assert job.status == "completed"
    assert svc.index_progress(job.id) is None  # 끝난 작업
    assert svc.index_progress(uuid.uuid4()) is None  # 이 프로세스가 돌린 적 없는 작업 (재시작 직후)


# ---- API: 화면이 읽는 응답에 실려 나간다 ---------------------------------------------------------


def _poll(read, wanted, timeout: float = 10.0):  # noqa: ANN001, ANN202
    deadline = time.monotonic() + timeout
    while True:
        got = read()
        if wanted(got):
            return got
        assert time.monotonic() < deadline, got
        time.sleep(0.01)


def _api_flow(settings: Settings) -> None:
    headers = {"X-User-Id": f"progress-{uuid.uuid4().hex[:8]}"}
    with TestClient(create_app(settings)) as client:
        state = client.app.state.container
        gate = GatedEmbedder()
        gate.dim = state.embedder.dim
        state.import_service._embedder = gate
        try:
            contact = client.post(
                "/v1/contacts",
                json={"alias": "박지현", "relationship_type": "superior", "use_default_persona": True},
                headers=headers,
            ).json()["id"]
            created = client.post("/v1/conversations", json={"contact_id": contact}, headers=headers)
            conv = created.json()["id"]
            posted = client.post(
                f"/v1/conversations/{conv}/imports", json={"messages": _messages(0, 300)}, headers=headers
            )
            assert posted.status_code == 202, posted.text
            job = posted.json()
            assert (job["status"], job["imported"]) == ("processing", 300)

            def latest() -> dict:
                r = client.get(f"/v1/conversations/{conv}/stats", headers=headers)
                assert r.status_code == 200, r.text
                return r.json()["latest_job"]

            first = _poll(latest, lambda j: j["index_total"] is not None)
            assert (first["id"], first["status"]) == (job["id"], "processing")
            assert (first["index_done"], first["index_total"]) == (0, 150)
            gate.permits = 1
            second = _poll(latest, lambda j: j["index_done"] == EMBED_BATCH)
            assert (second["status"], second["index_total"]) == ("processing", 150)
            direct = client.get(f"/v1/imports/{job['id']}", headers=headers).json()
            assert (direct["index_done"], direct["index_total"]) == (EMBED_BATCH, 150)

            gate.permits = 99
            done = _poll(latest, lambda j: j["status"] != "processing")
            assert (done["status"], done["episodes"]) == ("completed", 150)
            assert (done["index_done"], done["index_total"]) == (None, None)  # 끝나면 싣지 않는다
            again = client.get(f"/v1/imports/{job['id']}", headers=headers).json()
            assert (again["index_done"], again["index_total"]) == (None, None)
        finally:
            gate.permits = 10**6  # 실패해도 뒤에서 도는 묶음 생성이 끝나야 앱이 닫힌다


def _settings(**overrides: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="fake",
        embedding_provider="fake",
        import_index_wait_seconds=0.0,
        **overrides,
    )


def test_stats_and_job_responses_carry_the_progress_while_processing() -> None:
    _api_flow(_settings())


def test_stats_and_job_responses_carry_the_progress_on_postgres(pg_url: str) -> None:
    _api_flow(_settings(database_url=pg_url))


def test_a_job_left_processing_by_another_process_has_null_progress(
    client: TestClient, user_a: dict[str, str]
) -> None:
    """진행률을 모르는 작업(재시작 직후 등): 수치 없이 `processing`만 나간다 — 화면은 예전 문구를 보인다."""
    from app.domain.conversation import ImportJob

    contact = client.post(
        "/v1/contacts",
        json={"alias": "박지현", "relationship_type": "superior", "use_default_persona": True},
        headers=user_a,
    ).json()["id"]
    conv = client.post("/v1/conversations", json={"contact_id": contact}, headers=user_a).json()["id"]
    state = client.app.state.container
    left = ImportJob(
        id=uuid.uuid4(),
        user_id="user-a",
        conversation_id=uuid.UUID(conv),
        source_hash="0" * 64,
        status="processing",
        imported=10,
        created_at=datetime.fromisoformat("2026-11-14T09:00:00+09:00"),
    )
    asyncio.run(state.conversations.save_job(left))
    latest = client.get(f"/v1/conversations/{conv}/stats", headers=user_a).json()["latest_job"]
    assert (latest["id"], latest["status"]) == (str(left.id), "processing")
    assert (latest["index_done"], latest["index_total"]) == (None, None)
