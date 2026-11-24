"""수집 뒤 이름 대조: 추출 모델이 상대 노드 이름을 다르게 적어도 앱이 그 노드를 찾게 한다.

`GraphIngestService.reconcile_names` — 이름이 정확히 같은 노드가 없을 때만, 그 상대의 대화방 에피소드가
언급한 Person 노드 중 경칭·공백만 다른 이름이 정확히 하나면 `contacts.graph_name`을 맞춘다.
"""

from __future__ import annotations

import pytest

from app.ai.embeddings import FakeEmbedder
from app.graph.ingest import GraphIngestService, same_person_name
from app.graph.schema import EXTRACTION_INSTRUCTIONS, Relationship
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportPayload, ImportService
from tests.test_graph import MESSAGES_C1, FakeGraphStore

pytestmark = pytest.mark.anyio

EPISODES = ["c1:m01", "c1:m02"]


async def _setup(alias: str = "김상무님", nodes: tuple[str, ...] = ("김상무",)):  # noqa: ANN202
    """상대 `alias`의 방 c1(메시지 2건). 그래프에는 나(김민수)와 `nodes` 이름의 Person 노드가 생긴다."""
    contacts = InMemoryContactRepo()
    conversations = InMemoryConversationRepo()
    store = FakeGraphStore()
    store.entities = {"김민수": "node-me"} | {name: f"node-{i}" for i, name in enumerate(nodes)}
    store.mentions = {e: ["김민수", *nodes] for e in EPISODES}
    ingest = GraphIngestService(store, conversations, contacts)
    svc = ImportService(contacts, conversations, FakeEmbedder(dim=32), graph_ingest=ingest, graph_mode="off")
    contact = await contacts.create("u", alias, "superior")
    conv = await svc.create_conversation("u", contact.id, None, "json", "c1", me_name="김민수")
    await svc.import_messages("u", conv.id, ImportPayload(messages=MESSAGES_C1[:1]))
    return contacts, store, ingest, svc, contact, conv


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("김상무님", "김상무", True),
        ("김상무", "김상무 님", True),
        ("박영수", "박영수 씨", True),
        ("Michael Brown", "MichaelBrown", True),
        ("김 상무", "김상무", True),
        ("김상무님", "김상무씨", False),  # 경칭을 바꿔 끼우지는 않는다
        ("김상무", "김철수 상무", False),
        ("김상무", "김상무님님", False),
        ("이과장", "이과", False),
        ("님", "", False),
    ],
)
def test_same_person_name(a: str, b: str, same: bool) -> None:
    assert same_person_name(a, b) is same and same_person_name(b, a) is same


async def test_honorific_alias_is_matched_to_the_node_the_model_made() -> None:
    contacts, store, ingest, svc, contact, conv = await _setup()
    assert await store.find_entity_uuid("u", "김상무님") is None  # 어긋난 채로는 못 찾는다
    assert await ingest.ingest_pending("u") == 1

    fixed = await contacts.get("u", contact.id)
    assert (fixed.alias, fixed.graph_name) == ("김상무님", "김상무")
    assert await store.find_entity_uuid("u", fixed.name_in_graph) == "node-0"  # 이후 조회가 된다

    # 다음 수집의 에피소드 본문도 맞춘 이름을 쓴다 (노드가 둘로 갈리지 않게)
    await svc.import_messages("u", conv.id, ImportPayload(messages=MESSAGES_C1))
    assert await ingest.ingest_user("u") == 1
    assert store.added[-1]["body"].startswith("[2026-10-05 월 09:31] 김민수 → 김상무:")
    assert (await contacts.get("u", contact.id)).graph_name == "김상무"


async def test_exact_node_is_left_alone() -> None:
    # 이름이 같은 노드가 있으면 비슷한 이름의 노드가 더 있어도 건드리지 않는다
    contacts, store, ingest, _, contact, _ = await _setup(nodes=("김상무님", "김상무"))
    assert await ingest.ingest_pending("u") == 1
    assert (await contacts.get("u", contact.id)).graph_name == "김상무님"
    assert store.person_name_calls == 0  # 후보 조회도 하지 않는다


async def test_two_candidates_change_nothing(caplog: pytest.LogCaptureFixture) -> None:
    contacts, _, ingest, _, contact, _ = await _setup(nodes=("김상무", "김상무 님"))
    with caplog.at_level("WARNING", logger="app.graph.ingest"):
        assert await ingest.ingest_pending("u") == 1
    assert (await contacts.get("u", contact.id)).graph_name == "김상무님"
    assert "graph name mismatch" in caplog.text
    assert MESSAGES_C1[0].text not in caplog.text  # 대화 본문은 로그에 없다


async def test_no_candidate_changes_nothing(caplog: pytest.LogCaptureFixture) -> None:
    contacts, _, ingest, _, contact, _ = await _setup(nodes=("김철수 상무", "부장님"))
    with caplog.at_level("WARNING", logger="app.graph.ingest"):
        assert await ingest.ingest_pending("u") == 1
    assert (await contacts.get("u", contact.id)).graph_name == "김상무님"
    assert "graph name mismatch" in caplog.text


async def test_already_correct_graph_name_stays() -> None:
    # 손으로 고쳐 둔 상대(별칭 '김상무님', graph_name '김상무')는 그대로다
    contacts, store, ingest, _, contact, _ = await _setup()
    await contacts.remember_graph_name("u", contact.id, "김상무")
    assert await ingest.ingest_pending("u") == 1
    assert store.added[0]["body"].startswith("[2026-10-05 월 09:25] 김상무 → 김민수:")
    assert (await contacts.get("u", contact.id)).graph_name == "김상무"
    assert store.person_name_calls == 0


async def test_my_own_node_is_never_a_candidate() -> None:
    # 내 이름이 상대 별칭과 경칭만 다르면('김민수님' 상대, 나 '김민수') 내 노드로 맞추지 않는다
    contacts, _, ingest, _, contact, _ = await _setup(alias="김민수님", nodes=())
    assert await ingest.ingest_pending("u") == 1
    assert (await contacts.get("u", contact.id)).graph_name == "김민수님"


async def test_candidates_come_only_from_this_contacts_rooms() -> None:
    # 다른 상대의 방에만 나온 '김상무'는 후보가 아니다
    contacts, store, ingest, _, contact, _ = await _setup()
    store.mentions = {"c9:m01": ["김상무"], **{e: ["김민수"] for e in EPISODES}}
    assert await ingest.ingest_pending("u") == 1
    assert (await contacts.get("u", contact.id)).graph_name == "김상무님"


async def test_nothing_ingested_means_no_check_and_failure_is_swallowed() -> None:
    contacts, store, ingest, _, contact, _ = await _setup()

    async def broken(group_id, episode_names):  # noqa: ANN001, ANN202
        raise RuntimeError("neo4j down")

    store.person_names = broken  # type: ignore[method-assign]
    assert await ingest.ingest_pending("u") == 1  # 대조가 실패해도 수집은 끝난 것이다
    assert (await contacts.get("u", contact.id)).graph_name == "김상무님"

    calls: list[str] = []

    async def counting(group_id, episode_names):  # noqa: ANN001, ANN202
        calls.append(group_id)
        return []

    store.person_names = counting  # type: ignore[method-assign]
    assert await ingest.ingest_pending("u") == 0  # 넣은 것이 없으면 대조하지 않는다
    assert calls == []


def test_extraction_rules_name_the_address_term_mistakes() -> None:
    """2026-11-19 실제 규모 수집에서 나온 호칭 오류 유형을 지시문과 필드 설명이 금지한다."""
    description = Relationship.model_fields["address_term"].description or ""
    for text in (EXTRACTION_INSTRUCTIONS, description):
        for needle in ("source 자신의 이름·직함", "조각", "제3자", "그대로 옮긴", "영어 대화", "비운다"):
            assert needle in text


async def test_script_fixes_an_already_ingested_graph(capsys: pytest.CaptureFixture[str]) -> None:
    """`scripts/graph_names.py`: 수집이 이미 끝난 그래프도 맞춘다. --dry-run은 보기만 한다."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import graph_names

    contacts, store, ingest, _, contact, _ = await _setup()
    hidden, store.mentions = store.mentions, {}  # 수집 때는 후보를 못 봤다 (대조가 없던 때의 그래프)
    assert await ingest.ingest_pending("u") == 1
    assert (await contacts.get("u", contact.id)).graph_name == "김상무님"
    store.mentions = hidden

    assert await graph_names.reconcile(ingest, "u", apply=False) == 0
    assert "'김상무님' 노드 없음 → '김상무'" in capsys.readouterr().out
    assert (await contacts.get("u", contact.id)).graph_name == "김상무님"  # 보기만 했다

    assert await graph_names.reconcile(ingest, "u", apply=True) == 0
    assert (await contacts.get("u", contact.id)).graph_name == "김상무"
    assert await graph_names.reconcile(ingest, "u", apply=True) == 0  # 다시 돌려도 그대로
    assert "맞음 (노드 '김상무')" in capsys.readouterr().out

    store.entities.pop("김상무")  # 후보가 없으면 남은 것으로 알린다
    store.mentions = {}
    assert await graph_names.reconcile(ingest, "u", apply=True) == 1
    assert "그대로 둔다" in capsys.readouterr().out
    assert len(store.added) == 1  # 수집(모델 호출)은 하지 않는다
