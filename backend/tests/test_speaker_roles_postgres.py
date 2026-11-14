"""화자 역할 고치기 — PostgreSQL에서 (저장소 SQL + 서비스 + HTTP). Postgres가 없으면 건너뛴다.

인메모리 쪽 규칙(`test_speaker_roles.py`)이 실제 SQL에서도 같은지 본다: 맞바꾸는 UPDATE가 이 방에만 닿는지,
묶음과 그래프 수집 표시가 다시 만들어지는지.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("sqlalchemy")
pytest.importorskip("asyncpg")

from app.ai.embeddings import FakeEmbedder  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.errors import NotFoundError  # noqa: E402
from app.db.session import create_engine, create_session_factory  # noqa: E402
from app.domain.conversation import NewMessage  # noqa: E402
from app.graph.ingest import GraphIngestService  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.postgres import PostgresContactRepo, PostgresConversationRepo  # noqa: E402
from app.services.import_service import ImportService  # noqa: E402
from tests.test_graph import FakeGraphStore  # noqa: E402
from tests.test_speaker_roles import ANSWERS, ASKS, RAW, WRONG  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
async def repos(pg_url: str):  # noqa: ANN201
    engine = create_engine(pg_url)
    sessions = create_session_factory(engine)
    try:
        yield PostgresContactRepo(sessions), PostgresConversationRepo(sessions)
    finally:
        await engine.dispose()


def _user() -> str:
    return f"u-{uuid.uuid4().hex[:8]}"


def _msg(key: str, role: str, minute: int) -> NewMessage:
    return NewMessage(
        source_key=key,
        speaker_role=role,  # type: ignore[arg-type]
        sent_at=datetime(2026, 10, 5, 0, minute, tzinfo=UTC),
        text=f"{key}의 합성 문장",
    )


async def test_repo_swaps_only_this_room(repos) -> None:  # noqa: ANN001
    contacts, convs = repos
    a, b = _user(), _user()
    boss = await contacts.create(a, "박지현", "superior")
    other_boss = await contacts.create(b, "박지현", "superior")
    c1 = await convs.create(a, boss.id, "방1", "json", "c1")
    c2 = await convs.create(a, boss.id, "방2", "json", "c2")
    cb = await convs.create(b, other_boss.id, "남의 방", "json", "c1")
    batch = [_msg("m1", "me", 1), _msg("m2", "contact", 2), _msg("m3", "me", 3)]
    for owner, conv in ((a, c1), (a, c2), (b, cb)):
        await convs.add_messages(owner, conv.id, batch)

    async def roles(owner: str, conv_id: uuid.UUID) -> list[str]:
        return [m.speaker_role for m in await convs.list_messages(owner, conv_id)]

    first = (await convs.list_messages(a, c1.id))[0]
    got = await convs.get_message(a, c1.id, first.id)
    assert (got.id, got.source_key, got.speaker_role) == (first.id, "m1", "me")
    for owner, conv_id in ((a, c2.id), (b, cb.id), (b, c1.id)):  # 다른 방·남의 방·남의 계정
        with pytest.raises(NotFoundError):
            await convs.get_message(owner, conv_id, first.id)
    with pytest.raises(NotFoundError):
        await convs.get_message(a, c1.id, uuid.uuid4())

    with pytest.raises(NotFoundError):
        await convs.swap_speaker_roles(b, c1.id)
    assert await convs.swap_speaker_roles(a, c1.id) == 3
    assert await roles(a, c1.id) == ["contact", "me", "contact"]
    assert await roles(a, c2.id) == ["me", "contact", "me"]
    assert await roles(b, cb.id) == ["me", "contact", "me"]
    assert (await convs.conversation_stats(a, c1.id)).my_messages == 1
    assert await convs.swap_speaker_roles(a, c1.id) == 3  # 한 번 더 하면 원래대로
    assert await roles(a, c1.id) == ["me", "contact", "me"]


async def test_service_fixes_roles_episodes_and_graph_on_postgres(repos) -> None:  # noqa: ANN001
    contacts, convs = repos
    user = _user()
    store = FakeGraphStore()
    embedder = FakeEmbedder(dim=384)
    svc = ImportService(
        contacts,
        convs,
        embedder,
        graph_ingest=GraphIngestService(store, convs, contacts),
        graph_mode="sync",
    )
    boss = await contacts.create(user, "박지현", "superior")
    conv = await svc.create_conversation(user, boss.id, "팀장 방", "json", "c1", me_name="박지현")
    await svc.import_messages(user, conv.id, WRONG)

    async def style_lines() -> set[str]:
        hits = await convs.search_episodes(user, boss.id, await embedder.embed_query("수요일"), 50)
        return {line for h in hits for line in h.episode.style_text.split("\n")}

    assert await style_lines() == set(ASKS)
    assert len(store.added) == 4
    m2 = next(m for m in await convs.list_messages(user, conv.id) if m.source_key == "m2")

    r = await svc.set_speaker_role(user, conv.id, m2.id, "me", me_name="김민수")
    assert (r.changed, r.messages, r.my_messages, r.me_name) == (True, 4, 2, "김민수")
    assert (r.graph_removed, r.graph_requeued) == (4, 4)
    assert r.job is not None
    assert (r.job.status, r.job.episodes, r.job.graph_status, r.job.graph_episodes) == (
        "completed",
        2,
        "completed",
        4,
    )
    assert [m.speaker_role for m in await convs.list_messages(user, conv.id)] == [
        "contact",
        "me",
        "contact",
        "me",
    ]
    assert await style_lines() == set(ANSWERS)
    assert (await convs.get(user, conv.id)).me_name == "김민수"
    bodies = {a["name"]: a["body"] for a in store.added}
    assert len(bodies) == 4 and "김민수 → 박지현: 수요일(=" in bodies["c1:m2"]
    stats = await convs.conversation_stats(user, conv.id)
    assert (stats.messages, stats.my_messages, stats.episodes, stats.graph_ingested) == (4, 2, 2, 4)
    assert (await convs.latest_job(user, conv.id)).id == r.job.id

    again = await svc.set_speaker_role(user, conv.id, m2.id, "me")
    assert (again.changed, again.job) == (False, None)
    assert len(store.added) == 4 and await style_lines() == set(ANSWERS)


def test_api_flow_on_postgres(pg_url: str) -> None:
    settings = Settings(_env_file=None, llm_provider="fake", database_url=pg_url, embedding_provider="fake")  # type: ignore[call-arg]
    h, stranger = {"X-User-Id": _user()}, {"X-User-Id": _user()}
    with TestClient(create_app(settings)) as client:
        cid = client.post(
            "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=h
        ).json()["id"]
        conv_id = client.post(
            "/v1/conversations", json={"contact_id": cid, "source_key": "c1"}, headers=h
        ).json()["id"]
        job = client.post(f"/v1/conversations/{conv_id}/imports/raw", json={"text": RAW}, headers=h)
        assert job.status_code == 202, job.text
        messages = client.get(f"/v1/conversations/{conv_id}/messages", headers=h).json()
        url = f"/v1/conversations/{conv_id}/speaker-roles"
        body = {"message_id": messages[1]["id"], "speaker_role": "me"}

        assert client.put(url, json=body, headers=stranger).status_code == 404
        r = client.put(url, json=body, headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["changed"], out["messages"], out["my_messages"]) == (True, 4, 2)
        done = client.get(f"/v1/imports/{out['job']['id']}", headers=h).json()
        assert done["status"] in ("processing", "completed")
        after = client.get(f"/v1/conversations/{conv_id}/messages", headers=h).json()
        assert [m["speaker_role"] for m in after] == ["contact", "me", "contact", "me"]
        assert client.put(url, json=body, headers=h).json()["changed"] is False
        assert client.delete(f"/v1/conversations/{conv_id}", headers=h).status_code == 200


async def test_repo_speaker_names_on_postgres(repos) -> None:  # noqa: ANN001
    """0011: 이름 저장·채우기·화자별 역할·제외, 일부 메시지만 그래프 표시 비우기 — 이 방에만 닿는다."""
    contacts, convs = repos
    a = _user()
    boss = await contacts.create(a, "박지현", "superior")
    c1 = await convs.create(a, boss.id, "방1", "json", "c1")
    c2 = await convs.create(a, boss.id, "방2", "json", "c2")
    named = [
        _msg("m1", "me", 1).model_copy(update={"speaker_name": "김민수"}),
        _msg("m2", "contact", 2).model_copy(update={"speaker_name": "박지현"}),
        _msg("m3", "contact", 3).model_copy(update={"speaker_name": "최유진"}),
        _msg("m4", "contact", 4),  # 이름 없이 들어온 메시지
    ]
    for conv in (c1, c2):
        await convs.add_messages(a, conv.id, named)

    async def speakers(conv_id: uuid.UUID) -> list[tuple[str | None, str, int]]:
        return [(sp.name, sp.speaker_role, sp.messages) for sp in await convs.speakers(a, conv_id)]

    everyone = [("김민수", "me", 1), ("박지현", "contact", 1), ("최유진", "contact", 1), (None, "contact", 1)]
    assert await speakers(c1.id) == everyone
    assert [m.speaker_name for m in await convs.list_messages(a, c1.id)] == [
        "김민수",
        "박지현",
        "최유진",
        None,
    ]

    # 빈 이름만 채운다 — 이미 있는 이름, 다른 방, 모르는 키는 그대로
    filled = await convs.fill_speaker_names(a, c1.id, {"m1": "다른이름", "m4": "이서연", "없는키": "누구"})
    assert filled == 1
    assert [m.speaker_name for m in await convs.list_messages(a, c1.id)] == [
        "김민수",
        "박지현",
        "최유진",
        "이서연",
    ]
    assert await convs.fill_speaker_names(a, c1.id, {}) == 0

    msgs = await convs.list_messages(a, c1.id)
    await convs.mark_graph_ingested(a, [m.id for m in msgs], datetime(2026, 10, 6, tzinfo=UTC))
    assert await convs.reset_graph_ingested(a, c1.id, []) == 0
    assert await convs.reset_graph_ingested(a, c1.id, [msgs[2].id]) == 1
    assert [m.graph_ingested_at is None for m in await convs.list_messages(a, c1.id)] == [
        False,
        False,
        True,
        False,
    ]

    with pytest.raises(NotFoundError):
        await convs.apply_speaker_roles("someone-else", c1.id, {"김민수": "contact"}, set())
    changed, removed = await convs.apply_speaker_roles(
        a, c1.id, {"김민수": "contact", "박지현": "me", "이서연": "contact"}, {"최유진"}
    )
    assert (changed, removed) == (2, 1)  # 이서연은 이미 상대다
    assert await speakers(c1.id) == [("김민수", "contact", 1), ("박지현", "me", 1), ("이서연", "contact", 1)]
    assert await speakers(c2.id) == everyone
    assert (await convs.conversation_stats(a, c1.id)).messages == 3
