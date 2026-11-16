"""묶음을 다시 만들 때 본문이 그대로인 묶음은 벡터를 다시 계산하지 않는다 (2026-11-11 검토 D7).

메시지를 조금 더 가져올 때마다 방 전체를 다시 임베딩하면 실제 모델(e5)에서는 가져오기가 분 단위로
느려진다. 같은 임베딩 버전의 벡터만 다시 쓰고, 버전이 바뀌면 전부 새로 계산한다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.ai.embeddings import FakeEmbedder
from app.ai.retrieval.vector import VectorStyleRetriever
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService

pytestmark = pytest.mark.anyio


class CountingEmbedder(FakeEmbedder):
    def __init__(self, dim: int = 384) -> None:
        super().__init__(dim=dim)
        self.embedded: list[str] = []

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return await super().embed_passages(texts)


def _file(day: int, n: int = 6) -> ImportPayload:
    base = datetime.fromisoformat(f"2026-10-{day:02d}T09:00:00+09:00")
    return ImportPayload(
        messages=[
            ImportMessage(
                id=f"d{day}-{i}",
                speaker="me" if i % 2 else "contact",
                sent_at=base + timedelta(minutes=i),
                text=f"{day}일의 {i}번째 메시지입니다",
            )
            for i in range(n)
        ]
    )


@pytest.fixture(params=["memory", "postgres"])
async def setup(request: pytest.FixtureRequest):  # noqa: ANN201
    embedder = CountingEmbedder()
    if request.param == "memory":
        contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
        engine = None
    else:
        from app.db.session import create_engine, create_session_factory
        from app.repositories.postgres import PostgresContactRepo, PostgresConversationRepo

        engine = create_engine(request.getfixturevalue("pg_url"))
        sessions = create_session_factory(engine)
        contacts, conversations = PostgresContactRepo(sessions), PostgresConversationRepo(sessions)
    try:
        svc = ImportService(contacts, conversations, embedder)
        user = f"u-{uuid.uuid4().hex[:8]}"
        boss = await contacts.create(user, "박지현", "superior")
        conv = await svc.create_conversation(user, boss.id, None, "json", f"c-{uuid.uuid4().hex[:6]}")
        yield conversations, svc, embedder, user, boss, conv
    finally:
        if engine is not None:
            await engine.dispose()


async def test_only_new_or_changed_episodes_are_embedded(setup) -> None:  # noqa: ANN001
    conversations, svc, embedder, user, boss, conv = setup
    first = await svc.import_messages(user, conv.id, _file(5))
    assert first.episodes == 3 and len(embedder.embedded) == 3

    # 뒤에 메시지가 더 들어온다: 앞의 묶음 3개는 본문이 그대로다 → 새 묶음 3개만 임베딩한다
    second = await svc.import_messages(user, conv.id, _file(6))
    assert second.episodes == 6
    assert len(embedder.embedded) == 6  # 예전에는 3 + 6 = 9번 (방 전체를 매번 다시)
    assert all("6일의" in text for text in embedder.embedded[3:])

    # 다시 쓴 벡터로도 검색이 그대로 된다
    vectors = await conversations.episode_vectors(user, conv.id)
    assert len(vectors) == 6 and all(len(v) == 384 for v in vectors.values())
    assert all(key.startswith(f"{embedder.version}\n") for key in vectors)
    hits = await VectorStyleRetriever(conversations, embedder).retrieve(
        user_id=user, contact_id=boss.id, draft_text="5일의 1번째 메시지입니다", situation="", top_k=6
    )
    mine = {f"{day}일의 {i}번째 메시지입니다" for day in (5, 6) for i in (1, 3, 5)}
    assert {h.text for h in hits} == mine  # 묶음 6개가 모두 벡터를 갖고 검색된다

    # 묶음 다시 만들기만 돌려도(새 메시지 없음) 임베딩 호출이 없다
    assert await svc.reindex(user, conv) == 6
    assert len(embedder.embedded) == 6

    # 앞쪽에 메시지가 끼어들면 그 뒤 묶음의 맥락이 달라진다 → 달라진 것만 다시 임베딩한다
    earlier = ImportPayload(
        messages=[
            ImportMessage(id="late", speaker="contact", sent_at="2026-10-05T09:00:30+09:00", text="끼어든 말")
        ]
    )
    await svc.import_messages(user, conv.id, earlier)
    changed = embedder.embedded[6:]
    assert 1 <= len(changed) < 6 and all("끼어든 말" in text for text in changed)


async def test_a_different_embedding_version_recomputes_everything(setup) -> None:  # noqa: ANN001
    conversations, svc, embedder, user, _, conv = setup
    await svc.import_messages(user, conv.id, _file(5))
    assert len(embedder.embedded) == 3
    embedder.version = "fake:char-2gram:v2"  # 모델을 바꿨다 — 옛 벡터는 다시 쓰면 안 된다
    assert await svc.reindex(user, conv) == 3
    assert len(embedder.embedded) == 6
    vectors = await conversations.episode_vectors(user, conv.id)
    assert all(key.startswith("fake:char-2gram:v2\n") for key in vectors)
