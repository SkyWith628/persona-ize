"""예시 걸러내기 (graph-redesign 5.4, 경로 X): 문체 예시를 현재 말투 구간의 묶음으로 좁힌다.

- 저장소: `search_episodes(after=...)`는 end_at >= after 인 묶음만 남긴다 (인메모리·PostgreSQL 같은 규칙).
- 검색기: `gate_by_register=True`일 때만 좁힌다. 말투가 바뀐 적이 없거나, 두 단계를 섞어 쓰거나, 남는 예시가
  2개 미만이거나, 프로필 계산이 실패하면 좁히지 않은 결과와 같다.
- 기본값에서는 검색 호출도 버전 문자열도 예전과 같다.

합성 대화 c3(이서연)는 10/7~10/17 반말(m02, m04, m06) 뒤 10/21부터 합쇼체(m08, m10)다.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from app.ai.embeddings import FakeEmbedder
from app.ai.retrieval.vector import VectorStyleRetriever
from app.domain.conversation import EpisodeHit, Message
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data" / "synthetic" / "sample_conversations.json"
M08 = datetime.fromisoformat("2026-10-21T10:05:00+09:00")  # 합쇼체 구간의 첫 메시지
M10 = datetime.fromisoformat("2026-10-21T10:22:00+09:00")
NOW = datetime(2026, 11, 1, tzinfo=UTC)
BEFORE_CHANGE = datetime(2026, 10, 20, tzinfo=UTC)  # m06(10/17)과 m08(10/21) 사이
DRAFT = "금요일까지 문서 정리할게"
# 묶음의 출처 = 앞 맥락 + 본인 발화. 반말 시기 셋, 합쇼체 시기 둘.
OLD = [
    ["c3:m01", "c3:m02"],
    ["c3:m01", "c3:m02", "c3:m03", "c3:m04"],
    ["c3:m03", "c3:m04", "c3:m05", "c3:m06"],
]
NEW = [["c3:m05", "c3:m06", "c3:m07", "c3:m08"], ["c3:m07", "c3:m08", "c3:m09", "c3:m10"]]
NEW_TEXTS = {"서연 파트장님, 오늘 스탠드업 회의록 공유드립니다.", "네, 금요일 오전까지 정리하겠습니다."}


def _payload(conversation_key: str) -> ImportPayload:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    conv = next(c for c in data["conversations"] if c["conversation_id"] == conversation_key)
    return ImportPayload(
        messages=[
            ImportMessage(id=m["id"], speaker=m["speaker"], sent_at=m["sent_at"], text=m["text"])
            for m in conv["messages"]
        ]
    )


class SpyConversations(InMemoryConversationRepo):
    """검색기가 저장소를 어떻게 불렀는지 적어 둔다 (기본값에서 호출이 예전과 같은지 보려고)."""

    def __init__(self) -> None:
        super().__init__()
        self.searches: list[dict[str, Any]] = []
        self.message_reads = 0

    async def search_episodes(self, *args: Any, **kwargs: Any) -> list[EpisodeHit]:
        self.searches.append(kwargs)
        return await super().search_episodes(*args, **kwargs)

    async def list_messages(self, *args: Any, **kwargs: Any) -> list[Message]:
        self.message_reads += 1
        return await super().list_messages(*args, **kwargs)


async def _setup() -> tuple[SpyConversations, FakeEmbedder, UUID, UUID]:
    """(저장소, 임베더, 이서연 id(c3, 말투가 바뀐 상대), 박지현 id(c1, 바뀐 적 없는 상대))."""
    contacts = InMemoryContactRepo()
    conversations = SpyConversations()
    embedder = FakeEmbedder(dim=128)
    svc = ImportService(contacts, conversations, embedder)
    seoyeon = await contacts.create("u", "이서연", "colleague")
    boss = await contacts.create("u", "박지현", "superior")
    c3 = await svc.create_conversation("u", seoyeon.id, None, "json", "c3")
    c1 = await svc.create_conversation("u", boss.id, None, "json", "c1")
    await svc.import_messages("u", c3.id, _payload("c3"))
    await svc.import_messages("u", c1.id, _payload("c1"))
    conversations.searches.clear()
    conversations.message_reads = 0
    return conversations, embedder, seoyeon.id, boss.id


def _profile(
    start: datetime | None,
    *,
    previous: bool = True,
    two_valued: dict[str, int] | None = None,
    status: str = "confirmed",
    calls: list[tuple[Any, ...]] | None = None,
) -> Any:
    """`register.build_profile` 자리에 끼우는 가짜. 검색기가 읽는 필드만 채운다."""

    def build(messages: Any, contact_alias: str, *, as_of: datetime | None = None) -> SimpleNamespace:
        if calls is not None:
            calls.append((list(messages), contact_alias, as_of))
        current = None if start is None else SimpleNamespace(value="hapsyo", start=start, status=status)
        return SimpleNamespace(
            current_speech=current,
            previous_speech=SimpleNamespace(value="hae") if previous else None,
            two_valued=two_valued,
        )

    return build


async def _ids(retriever: VectorStyleRetriever, contact_id: UUID, **kwargs: Any) -> list[list[str]]:
    kwargs.setdefault("as_of", NOW)
    got = await retriever.retrieve(
        user_id="u", contact_id=contact_id, draft_text=DRAFT, situation="업무", **kwargs
    )
    return [e.source_message_ids for e in got]


# ---------- 저장소: after 경계 ----------


async def test_memory_after_keeps_episodes_whose_last_own_utterance_is_at_or_after_the_boundary() -> None:
    conversations, embedder, seoyeon, _ = await _setup()
    query = await embedder.embed_query(DRAFT)

    async def keys(**kwargs: Any) -> list[list[str]]:
        hits = await conversations.search_episodes("u", seoyeon, query, 10, **kwargs)
        return sorted(h.episode.source_message_ids for h in hits)

    assert await keys() == sorted(OLD + NEW)
    assert await keys(after=None) == sorted(OLD + NEW)
    # 경계와 같은 시각에 끝난 묶음은 남는다 (end_at >= after). 앞 맥락(m05~m07)이 경계보다 이르러도 남는다.
    assert await keys(after=M08) == sorted(NEW)
    assert await keys(after=M08 + timedelta(seconds=1)) == [NEW[1]]
    assert await keys(after=M10 + timedelta(seconds=1)) == []
    # before와 함께 쓰면 [after, before) 구간이다.
    assert await keys(after=M08, before=M10) == [NEW[0]]
    # 유사도 순서는 after와 무관하다 — 좁힌 결과는 전체 결과에서 걸러낸 것과 순서까지 같다.
    every = await conversations.search_episodes("u", seoyeon, query, 10)
    gated = await conversations.search_episodes("u", seoyeon, query, 10, after=M08)
    assert [h.episode.id for h in gated] == [h.episode.id for h in every if h.episode.end_at >= M08]


# ---------- 검색기: 기본값은 예전과 같다 ----------


async def test_default_retriever_is_unchanged() -> None:
    conversations, embedder, seoyeon, _ = await _setup()
    retriever = VectorStyleRetriever(conversations, embedder)
    assert retriever.version == f"knn-style-v1/{embedder.version}"
    got = await retriever.retrieve(user_id="u", contact_id=seoyeon, draft_text=DRAFT, situation="", as_of=NOW)
    direct = await InMemoryConversationRepo.search_episodes(
        conversations, "u", seoyeon, await embedder.embed_query(DRAFT), 4, before=NOW
    )
    assert len(got) == 4
    assert [(e.id, e.text) for e in got] == [(str(h.episode.id), h.episode.style_text) for h in direct]
    # 저장소 호출도 예전 그대로다: after를 넘기지 않고 메시지를 읽지 않는다.
    assert conversations.searches == [{"before": NOW}]
    assert conversations.message_reads == 0


# ---------- 검색기: 걸러내기 (프로필을 바꿔 끼운 경우) ----------


async def test_gate_keeps_only_the_current_segment() -> None:
    conversations, embedder, seoyeon, _ = await _setup()
    calls: list[tuple[Any, ...]] = []
    ungated = VectorStyleRetriever(conversations, embedder)
    gated = VectorStyleRetriever(
        conversations, embedder, gate_by_register=True, profile_fn=_profile(M08, calls=calls)
    )
    assert gated.version == f"knn-style-v1/{embedder.version}+gate"

    assert any(ids in OLD for ids in await _ids(ungated, seoyeon))  # 좁히지 않으면 반말 시기 예시가 섞인다
    got = await gated.retrieve(user_id="u", contact_id=seoyeon, draft_text=DRAFT, situation="업무", as_of=NOW)
    assert sorted(e.source_message_ids for e in got) == sorted(NEW)
    assert {e.text for e in got} == NEW_TEXTS
    # 프로필에는 본인 메시지만, 묶음 출처와 같은 id 꼴로, as_of와 함께 넘어간다.
    messages, _alias, as_of = calls[0]
    assert [m[0] for m in messages] == ["c3:m02", "c3:m04", "c3:m06", "c3:m08", "c3:m10"]
    assert messages[3][1] == M08 and messages[3][2].startswith("서연 파트장님")
    assert as_of == NOW


@pytest.mark.parametrize(
    "profile_fn",
    [
        _profile(M08, previous=False),  # 말투가 바뀐 적이 없다
        _profile(None),  # 확정된 현재 구간이 없다
        _profile(M08, status="candidate"),
        _profile(M08, two_valued={"hapsyo": 2, "hae": 2}),  # 상황에 따라 두 말투를 쓴다
    ],
)
async def test_gate_is_off_without_a_confirmed_change(profile_fn: Any) -> None:
    conversations, embedder, seoyeon, _ = await _setup()
    ungated = VectorStyleRetriever(conversations, embedder)
    gated = VectorStyleRetriever(conversations, embedder, gate_by_register=True, profile_fn=profile_fn)
    assert await _ids(gated, seoyeon) == await _ids(ungated, seoyeon)
    assert all("after" not in call for call in conversations.searches)


async def test_gate_falls_back_when_fewer_than_two_examples_remain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversations, embedder, seoyeon, _ = await _setup()
    ungated = VectorStyleRetriever(conversations, embedder)
    # 현재 구간이 m10에서 시작한다고 하면 남는 묶음은 하나뿐이다.
    gated = VectorStyleRetriever(conversations, embedder, gate_by_register=True, profile_fn=_profile(M10))
    with caplog.at_level(logging.DEBUG, logger="app.ai.retrieval.vector"):
        assert await _ids(gated, seoyeon) == await _ids(ungated, seoyeon)
    assert any("좁히지 않고" in r.getMessage() for r in caplog.records)
    # 한 건만 달라고 했으면 한 건으로 충분하다.
    assert await _ids(gated, seoyeon, top_k=1) == [NEW[1]]


async def test_gate_failure_falls_back_to_the_ungated_search() -> None:
    conversations, embedder, seoyeon, _ = await _setup()

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("프로필 계산 실패")

    ungated = VectorStyleRetriever(conversations, embedder)
    gated = VectorStyleRetriever(conversations, embedder, gate_by_register=True, profile_fn=broken)
    assert await _ids(gated, seoyeon) == await _ids(ungated, seoyeon)


# ---------- 검색기: 실제 규칙 프로필(app.ai.register)로 ----------


async def test_gate_with_the_rule_profile_on_synthetic_c3() -> None:
    pytest.importorskip("app.ai.register")  # register-core 패키지가 아직 없으면 건너뛴다
    conversations, embedder, seoyeon, boss = await _setup()
    ungated = VectorStyleRetriever(conversations, embedder)
    gated = VectorStyleRetriever(conversations, embedder, gate_by_register=True)

    # 변화 뒤: 합쇼체 시기의 묶음만
    assert sorted(await _ids(gated, seoyeon)) == sorted(NEW)
    # 변화 전 시점: 반말 구간 하나뿐이라 좁히지 않은 결과와 같다
    before = await _ids(ungated, seoyeon, as_of=BEFORE_CHANGE)
    assert before and await _ids(gated, seoyeon, as_of=BEFORE_CHANGE) == before
    # 말투가 바뀐 적 없는 상대(c1 박지현)도 같다
    same = await _ids(ungated, boss)
    assert same and await _ids(gated, boss) == same


# ---------- PostgreSQL: after 필터가 SQL에서도 같은가 ----------


async def test_postgres_after_filter(pg_url: str) -> None:
    """Postgres가 없으면 `pg_url` 픽스처가 건너뛴다 (tests/test_repos_postgres.py와 같은 방식)."""
    from app.db.session import create_engine, create_session_factory
    from app.repositories.postgres import PostgresContactRepo, PostgresConversationRepo

    engine = create_engine(pg_url)
    sessions = create_session_factory(engine)
    try:
        contacts, conversations = PostgresContactRepo(sessions), PostgresConversationRepo(sessions)
        embedder = FakeEmbedder(dim=384)
        user = f"u-{uuid.uuid4().hex[:8]}"
        seoyeon = await contacts.create(user, "이서연", "colleague")
        svc = ImportService(contacts, conversations, embedder)
        conv = await svc.create_conversation(user, seoyeon.id, None, "json", "c3")
        job = await svc.import_messages(user, conv.id, _payload("c3"))
        assert job.status == "completed" and job.episodes == 5
        query = await embedder.embed_query(DRAFT)

        async def keys(**kwargs: Any) -> list[list[str]]:
            hits = await conversations.search_episodes(user, seoyeon.id, query, 10, **kwargs)
            return sorted(h.episode.source_message_ids for h in hits)

        assert await keys() == sorted(OLD + NEW)
        assert await keys(after=M08) == sorted(NEW)  # 경계와 같은 시각은 남는다 (end_at >= after)
        assert await keys(after=M08 + timedelta(seconds=1)) == [NEW[1]]
        assert await keys(after=M08, before=M10) == [NEW[0]]
        assert await keys(after=M10 + timedelta(seconds=1)) == []
        every = await conversations.search_episodes(user, seoyeon.id, query, 10)
        gated = await conversations.search_episodes(user, seoyeon.id, query, 10, after=M08)
        assert [h.episode.id for h in gated] == [h.episode.id for h in every if h.episode.end_at >= M08]

        # 검색기까지: 좁힌 검색이 실제 SQL로도 합쇼체 시기 묶음만 준다
        retriever = VectorStyleRetriever(
            conversations, embedder, gate_by_register=True, profile_fn=_profile(M08)
        )
        got = await retriever.retrieve(
            user_id=user, contact_id=seoyeon.id, draft_text=DRAFT, situation="", as_of=NOW
        )
        assert {e.text for e in got} == NEW_TEXTS
    finally:
        await engine.dispose()


# ---------- 검색기: 상대 별칭, 메시지 상한 (리뷰 2026-11-16) ----------


async def test_gate_passes_the_contact_alias_and_a_bounded_recent_window() -> None:
    """걸러내기는 제안·상태 블록과 같은 입력으로 프로필을 계산한다: 상대 별칭, as_of 이전의 최근 메시지."""
    conversations, embedder, seoyeon, _ = await _setup()
    calls: list[tuple[Any, ...]] = []

    async def alias_of(user_id: str, contact_id: UUID) -> str:
        assert (user_id, contact_id) == ("u", seoyeon)
        return "이서연"

    gated = VectorStyleRetriever(
        conversations,
        embedder,
        gate_by_register=True,
        profile_fn=_profile(M08, calls=calls),
        alias_of=alias_of,
    )
    await _ids(gated, seoyeon, as_of=M10)
    [(messages, alias, as_of)] = calls
    assert alias == "이서연" and as_of == M10
    # as_of 이전의 내 메시지만, 시간 순으로 (m10은 as_of 그 자체라 빠진다)
    assert [mid for mid, _, _ in messages] == ["c3:m02", "c3:m04", "c3:m06", "c3:m08"]


async def test_gate_sees_a_shift_that_only_the_name_vocative_reveals() -> None:
    """어미가 안 읽히는 반말("서연아 ㅇㅋ?")은 별칭의 이름이 있어야 읽힌다 — 별칭 없이는 걸러내지 못한다."""
    contacts = InMemoryContactRepo()
    conversations = SpyConversations()
    embedder = FakeEmbedder(dim=128)
    svc = ImportService(contacts, conversations, embedder)
    seoyeon = await contacts.create("u", "이서연", "colleague")
    conv = await svc.create_conversation("u", seoyeon.id, None, "json", "c9")
    texts = ["알겠습니다", "감사합니다", "서연아 오늘 국밥 ㄱ?", "서연아 ㅇㅋ?", "서연아 내일 ㄱㄱ?"]
    await svc.import_messages(
        "u",
        conv.id,
        ImportPayload(
            messages=[
                ImportMessage(
                    id=f"m{i:02d}", speaker="me", sent_at=f"2026-10-{i + 1:02d}T10:00:00+09:00", text=text
                )
                for i, text in enumerate(texts, start=1)
            ]
        ),
    )

    async def alias_of(user_id: str, contact_id: UUID) -> str:
        return (await contacts.get(user_id, contact_id)).alias

    with_alias = VectorStyleRetriever(conversations, embedder, gate_by_register=True, alias_of=alias_of)
    without = VectorStyleRetriever(conversations, embedder, gate_by_register=True)
    start = datetime.fromisoformat("2026-10-04T10:00:00+09:00")  # 반말 구간의 첫 메시지(m03)
    assert await with_alias._segment_start("u", seoyeon.id, NOW) == start
    assert await without._segment_start("u", seoyeon.id, NOW) is None
