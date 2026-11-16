"""저장소가 달라도 날짜·요일은 한국 시간으로 읽는다 (2026-11-11 정확성 검토 #1).

인메모리는 가져온 시각의 시간대(+09:00)를 그대로 들고 있고, PostgreSQL(timestamptz)은 UTC로 돌려준다.
그 datetime의 자기 시간대로 날짜를 읽으면 화요일 08:01(KST)의 "내일"이 `[월 23:01] … 내일(=화요일)`로
그래프에 들어간다. 날짜·요일을 글자로 만드는 세 곳 — 그래프 에피소드 본문, 상대 날짜 풀이, 프롬프트 예시의
날짜 — 이 같은 결과를 내는지 **PostgreSQL 저장소로** 확인한다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.ai.embeddings import FakeEmbedder
from app.ai.prompt import _examples_block
from app.ai.retrieval.vector import VectorStyleRetriever
from app.domain.conversation import local_time
from app.domain.rewrite import RetrievedExample
from app.graph.dates import annotate_relative_dates, resolve_relative_dates
from app.graph.episodes import format_body
from app.graph.ingest import GraphIngestService
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService
from tests.test_graph import FakeGraphStore

pytestmark = pytest.mark.anyio

KST = timezone(timedelta(hours=9))
# 화요일 아침 8시 1분(KST) = 월요일 23시 1분(UTC)
EARLY = [
    ImportMessage(id="m01", speaker="contact", sent_at="2026-10-06T08:00:00+09:00", text="자료 언제 돼요?"),
    ImportMessage(
        id="m02", speaker="me", sent_at="2026-10-06T08:01:00+09:00", text="내일 오전까지 보내드리겠습니다."
    ),
    ImportMessage(
        id="m03",
        speaker="me",
        sent_at="2026-10-06T08:02:00+09:00",
        text="이번 주 금요일 회의 때 설명드릴게요.",
    ),
]
EXPECTED_BODIES = [
    "[2026-10-06 화 08:00] 박지현 → 김민수: 자료 언제 돼요?",
    "[2026-10-06 화 08:01] 김민수 → 박지현: 내일(=2026-10-07) 오전까지 보내드리겠습니다.",
    "[2026-10-06 화 08:02] 김민수 → 박지현: 이번 주 금요일(=2026-10-09) 회의 때 설명드릴게요.",
]


def test_local_time_reads_any_timezone_as_korean_time() -> None:
    utc = datetime(2026, 10, 5, 23, 1, tzinfo=UTC)
    assert local_time(utc) == utc and local_time(utc).isoformat() == "2026-10-06T08:01:00+09:00"
    assert local_time(datetime(2026, 10, 6, 8, 1, tzinfo=KST)).isoformat() == "2026-10-06T08:01:00+09:00"
    assert local_time(datetime(2026, 10, 5, 23, 1)).day == 6  # 시간대가 없으면 UTC로 본다


def test_format_body_and_relative_dates_do_not_depend_on_the_stored_timezone() -> None:
    as_utc = datetime(2026, 10, 5, 23, 1, tzinfo=UTC)  # PostgreSQL이 돌려주는 모양
    as_kst = as_utc.astimezone(KST)  # 인메모리가 들고 있는 모양
    for moment in (as_utc, as_kst):
        assert (
            format_body(moment, "김민수", "박지현", "내일 오전까지 보내드리겠습니다.") == EXPECTED_BODIES[1]
        )
        assert annotate_relative_dates("내일 보자", moment) == "내일(=2026-10-07) 보자"
        assert resolve_relative_dates("이번 주 금요일", moment) == [("이번 주 금요일", "2026-10-09")]
    # 09시 이후(UTC와 날짜가 같은 시각)는 예전과 같다
    late = datetime(2026, 10, 5, 9, 31, tzinfo=KST)
    assert format_body(late, "김민수", "박지현", "네").startswith("[2026-10-05 월 09:31] ")
    assert format_body(late.astimezone(UTC), "김민수", "박지현", "네").startswith("[2026-10-05 월 09:31] ")


def test_prompt_example_dates_are_korean_dates() -> None:
    def example(moment: datetime) -> RetrievedExample:
        return RetrievedExample(id="e1", purpose="style", text="내일 봬요", speaker_role="me", sent_at=moment)

    utc = datetime(2026, 10, 5, 23, 1, tzinfo=UTC)
    assert _examples_block([example(utc)], "style") == '- id=e1 (10/06) "내일 봬요"'
    assert _examples_block([example(utc.astimezone(KST))], "style") == '- id=e1 (10/06) "내일 봬요"'


async def _import_and_collect(contacts, conversations) -> tuple[list[str], str]:  # noqa: ANN001
    user = f"tz-{uuid.uuid4().hex[:8]}"
    store = FakeGraphStore()
    ingest = GraphIngestService(store, conversations, contacts)
    embedder = FakeEmbedder(dim=384)
    svc = ImportService(contacts, conversations, embedder, graph_ingest=ingest, graph_mode="sync")
    boss = await contacts.create(user, "박지현", "superior")
    conv = await svc.create_conversation(
        user, boss.id, "방", "json", f"c-{uuid.uuid4().hex[:6]}", me_name="김민수"
    )
    await svc.import_messages(user, conv.id, ImportPayload(messages=EARLY))
    examples = await VectorStyleRetriever(conversations, embedder).retrieve(
        user_id=user, contact_id=boss.id, draft_text="내일 오전까지 보내드릴게요", situation=""
    )
    bodies = [a["body"] for a in store.added]
    block = _examples_block(examples, "style")
    return bodies, block.replace(examples[0].id, "ID")


async def test_graph_bodies_and_prompt_dates_are_the_same_on_memory_and_postgres(pg_url: str) -> None:
    from app.db.session import create_engine, create_session_factory
    from app.repositories.postgres import PostgresContactRepo, PostgresConversationRepo

    mem_bodies, mem_block = await _import_and_collect(InMemoryContactRepo(), InMemoryConversationRepo())
    engine = create_engine(pg_url)
    sessions = create_session_factory(engine)
    try:
        pg_bodies, pg_block = await _import_and_collect(
            PostgresContactRepo(sessions), PostgresConversationRepo(sessions)
        )
    finally:
        await engine.dispose()
    assert pg_bodies == EXPECTED_BODIES  # PostgreSQL: 예전에는 `[2026-10-05 월 23:01] … 내일(=2026-10-06)`
    assert mem_bodies == EXPECTED_BODIES
    assert (
        pg_block
        == mem_block
        == '- id=ID (10/06) "내일 오전까지 보내드리겠습니다.\n이번 주 금요일 회의 때 설명드릴게요."'
    )
