"""원래 화자 이름 저장(0011)과 화자별 역할 고치기 (인메모리 + API).

- 가져올 때 내보내기의 화자 이름을 메시지에 남긴다. 역할 낱말(me/contact)로 올린 것은 이름이 없다.
- 이름이 비어 있는 예전 메시지는 같은 내보내기를 다시 올리면 채워진다 (역할은 그대로).
- `set_speaker_roles`: 화자 이름별로 나/상대/제외. 적지 않은 화자는 그대로, 제외는 메시지를 지운다.
  다시 보내도 결과가 같다(멱등). 그래프는 바뀐 메시지만 지우고 다시 넣는다.
- 제외했던 화자는 그 화자를 넣어 다시 올리면 빠진 메시지만 더 들어온다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.ai.embeddings import FakeEmbedder
from app.core.errors import InvalidInputError, NotFoundError
from app.graph.ingest import GraphIngestService
from app.importers import ParsedImport
from app.importers.base import ParsedMessage
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService
from tests.test_graph import FakeGraphStore

pytestmark = pytest.mark.anyio

KST = timezone(timedelta(hours=9))
# 단체방: 김민수가 나, 박지현이 상대. 최유진은 끼어 있는 다른 사람이다.
LINES = [
    ("g1", "박지현", "보고서 언제 돼요?"),
    ("g2", "김민수", "수요일까지 드릴게요."),
    ("g3", "최유진", "저도 수요일에 볼게요."),
    ("g4", "박지현", "화요일은 어려워요?"),
    ("g5", "김민수", "네, 수요일로 부탁드려요."),
    ("g6", "최유진", "점심은 제가 살게요."),
]
EVERYONE = {"김민수": "me", "박지현": "contact", "최유진": "contact"}
WITHOUT_YUJIN = {"김민수": "me", "박지현": "contact", "최유진": "skip"}


def _parsed() -> ParsedImport:
    start = datetime(2026, 10, 5, 9, 25, tzinfo=KST)
    return ParsedImport(
        format="plain",
        messages=[
            ParsedMessage(speaker=who, sent_at=start + timedelta(minutes=i), text=text, id=key)
            for i, (key, who, text) in enumerate(LINES)
        ],
    )


async def _setup(graph_mode: str = "off", *, with_graph: bool = False):  # noqa: ANN202
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    store = FakeGraphStore()
    ingest = GraphIngestService(store, conversations, contacts) if with_graph else None
    svc = ImportService(
        contacts,
        conversations,
        FakeEmbedder(dim=32),
        graph_ingest=ingest,
        graph_mode=graph_mode,  # type: ignore[arg-type]
    )
    boss = await contacts.create("u", "박지현", "superior")
    conv = await svc.create_conversation("u", boss.id, "팀 방", "plain", "c1")
    return conversations, svc, store, conv


async def _who(conversations, conv) -> dict[str, tuple[str | None, str]]:  # noqa: ANN001
    return {
        m.source_key: (m.speaker_name, m.speaker_role)
        for m in await conversations.list_messages("u", conv.id)
    }


async def _speakers(conversations, conv) -> list[tuple[str | None, str, int]]:  # noqa: ANN001
    return [(sp.name, sp.speaker_role, sp.messages) for sp in await conversations.speakers("u", conv.id)]


async def test_import_keeps_the_original_speaker_names() -> None:
    conversations, svc, _, conv = await _setup()
    await svc.import_parsed("u", conv.id, _parsed(), EVERYONE)
    assert await _who(conversations, conv) == {
        "g1": ("박지현", "contact"),
        "g2": ("김민수", "me"),
        "g3": ("최유진", "contact"),
        "g4": ("박지현", "contact"),
        "g5": ("김민수", "me"),
        "g6": ("최유진", "contact"),
    }
    assert await _speakers(conversations, conv) == [
        ("김민수", "me", 2),
        ("박지현", "contact", 2),
        ("최유진", "contact", 2),
    ]


async def test_json_import_names_only_mapped_speakers() -> None:
    conversations, svc, _, conv = await _setup()
    payload = ImportPayload(
        speaker_map={"  김민수 ": "me"},
        messages=[
            ImportMessage(id="m1", speaker="contact", sent_at="2026-10-05T09:25:00+09:00", text="언제 돼요?"),
            ImportMessage(
                id="m2", speaker="  김민수 ", sent_at="2026-10-05T09:31:00+09:00", text="수요일요."
            ),
            ImportMessage(id="m3", speaker="me", sent_at="2026-10-05T09:40:00+09:00", text="네."),
        ],
    )
    await svc.import_messages("u", conv.id, payload)
    # 역할 낱말로 올린 메시지는 이름이 없다. 이름은 앞뒤 공백을 뗀다
    assert await _who(conversations, conv) == {
        "m1": (None, "contact"),
        "m2": ("김민수", "me"),
        "m3": (None, "me"),
    }
    assert await _speakers(conversations, conv) == [
        ("김민수", "me", 1),
        (None, "contact", 1),
        (None, "me", 1),
    ]


async def test_uploading_the_same_export_again_fills_missing_names() -> None:
    """이 열이 생기기 전에 가져온 방: 다시 올리면 이름만 채워진다 — 역할과 작업은 그대로."""
    conversations, svc, _, conv = await _setup()
    first = await svc.import_parsed("u", conv.id, _parsed(), EVERYONE)
    conversations._messages[conv.id] = [
        m.model_copy(update={"speaker_name": None}) for m in conversations._messages[conv.id]
    ]
    assert await _speakers(conversations, conv) == [(None, "contact", 4), (None, "me", 2)]

    again = await svc.import_parsed("u", conv.id, _parsed(), EVERYONE)
    assert (again.id, again.imported) == (first.id, first.imported)  # 같은 내용 — 새로 넣은 것은 없다
    assert {name for name, _ in (await _who(conversations, conv)).values()} == {"김민수", "박지현", "최유진"}

    # 이미 있는 이름은 덮어쓰지 않고, 다르게 지정해 올려도 저장된 역할은 바뀌지 않는다
    swapped = {"김민수": "contact", "박지현": "me", "최유진": "contact"}
    await svc.import_parsed("u", conv.id, _parsed(), swapped)
    assert (await _who(conversations, conv))["g2"] == ("김민수", "me")


async def test_one_speaker_is_removed_and_the_rest_stay() -> None:
    conversations, svc, _, conv = await _setup()
    await svc.import_parsed("u", conv.id, _parsed(), EVERYONE)
    before = await conversations.conversation_stats("u", conv.id)
    assert (before.messages, before.my_messages) == (6, 2)

    r = await svc.set_speaker_roles("u", conv.id, {"최유진": "skip"})
    assert (r.changed, r.messages, r.messages_removed, r.my_messages) == (True, 0, 2, 2)
    assert r.me_name == "김민수"  # '나'는 그대로다 — 첫 가져오기에서 받은 이름도 그대로
    assert r.job is not None and (r.job.status, r.job.imported) == ("completed", 0)
    assert set(await _who(conversations, conv)) == {"g1", "g2", "g4", "g5"}
    stats = await conversations.conversation_stats("u", conv.id)
    assert (stats.messages, stats.my_messages, stats.episodes) == (4, 2, r.job.episodes)
    query = await FakeEmbedder(dim=32).embed_query("수요일")
    hits = await conversations.search_episodes("u", conv.contact_id, query, 50)
    assert all("최유진" not in h.episode.context_text and "점심" not in h.episode.context_text for h in hits)

    # 다시 보내도 같다: 이미 없는 화자의 제외는 아무 일도 하지 않는다
    again = await svc.set_speaker_roles("u", conv.id, {"최유진": "skip"})
    assert (again.changed, again.messages_removed, again.job) == (False, 0, None)
    assert (await conversations.latest_job("u", conv.id)).id == r.job.id
    assert len(svc._conv_locks) == 0


async def test_roles_are_fixed_per_speaker_and_me_name_follows() -> None:
    conversations, svc, _, conv = await _setup()
    wrong = {"김민수": "contact", "박지현": "me", "최유진": "contact"}
    await svc.import_parsed("u", conv.id, _parsed(), wrong)
    assert (await conversations.get("u", conv.id)).me_name == "박지현"

    r = await svc.set_speaker_roles("u", conv.id, {"김민수": "me", "박지현": "contact"})
    assert (r.changed, r.messages, r.messages_removed, r.my_messages) == (True, 4, 0, 2)
    assert r.me_name == "김민수"  # '나'가 한 사람이면 그 이름을 쓴다
    assert (await conversations.get("u", conv.id)).me_name == "김민수"
    assert await _speakers(conversations, conv) == [
        ("김민수", "me", 2),
        ("박지현", "contact", 2),
        ("최유진", "contact", 2),
    ]
    # 이미 맞는 상태를 다시 적으면 아무것도 하지 않는다 (일부만 적어도 같다)
    assert (await svc.set_speaker_roles("u", conv.id, {"김민수": "me"})).changed is False
    assert (await svc.set_speaker_roles("u", conv.id, EVERYONE)).changed is False


async def test_me_name_is_kept_when_another_speaker_joins_me_and_given_name_wins() -> None:
    conversations, svc, _, conv = await _setup()
    await svc.import_parsed("u", conv.id, _parsed(), EVERYONE)
    # 내 다른 계정이 섞여 있었다: '나'가 둘이 되지만 원래 '나'가 그대로라 이름은 남는다
    r = await svc.set_speaker_roles("u", conv.id, {"최유진": "me"}, me_name="다른이름")
    assert (r.messages, r.my_messages, r.me_name) == (2, 4, "다른이름")
    # '나'가 달라지지 않는 변경에서는 준 이름을 쓰지 않는다
    r = await svc.set_speaker_roles("u", conv.id, {"박지현": "skip"}, me_name="또다른이름")
    assert (r.messages_removed, r.me_name) == (2, "다른이름")
    # '나'였던 화자가 빠지고 남은 '나'가 한 사람이면 그 이름
    r = await svc.set_speaker_roles("u", conv.id, {"김민수": "contact"})
    assert (r.my_messages, r.me_name) == (2, "최유진")


async def test_unknown_names_and_emptying_the_room_are_refused() -> None:
    conversations, svc, _, conv = await _setup()
    await svc.import_parsed("u", conv.id, _parsed(), WITHOUT_YUJIN)
    before = await _who(conversations, conv)
    with pytest.raises(InvalidInputError, match="최유진"):  # 제외해서 저장되지 않은 화자
        await svc.set_speaker_roles("u", conv.id, {"최유진": "contact"})
    with pytest.raises(InvalidInputError, match="없는사람"):
        await svc.set_speaker_roles("u", conv.id, {"김민수": "contact", "없는사람": "me"})
    with pytest.raises(InvalidInputError, match="남지 않습니다"):
        await svc.set_speaker_roles("u", conv.id, {"김민수": "skip", "박지현": "skip"})
    with pytest.raises(NotFoundError):
        await svc.set_speaker_roles("someone-else", conv.id, {"김민수": "contact"})
    assert await _who(conversations, conv) == before
    assert len(svc._conv_locks) == 0


async def test_a_skipped_speaker_comes_back_by_uploading_again() -> None:
    conversations, svc, _, conv = await _setup()
    first = await svc.import_parsed("u", conv.id, _parsed(), WITHOUT_YUJIN)
    assert (first.imported, first.duplicates) == (4, 0)
    assert await _speakers(conversations, conv) == [("김민수", "me", 2), ("박지현", "contact", 2)]

    back = await svc.import_parsed("u", conv.id, _parsed(), EVERYONE)
    assert (back.imported, back.duplicates) == (2, 4)  # 빠졌던 메시지만 더 들어온다
    assert await _speakers(conversations, conv) == [
        ("김민수", "me", 2),
        ("박지현", "contact", 2),
        ("최유진", "contact", 2),
    ]
    # 다시 빼고, 또 되살릴 수 있다
    assert (await svc.set_speaker_roles("u", conv.id, {"최유진": "skip"})).messages_removed == 2
    # 끝난 작업과 같은 내용이어도 빠진 메시지가 있으면 다시 넣는다 (같은 작업 id)
    again = await svc.import_parsed("u", conv.id, _parsed(), EVERYONE)
    assert (again.id, again.status, again.imported, again.duplicates) == (back.id, "completed", 2, 4)
    assert (await conversations.conversation_stats("u", conv.id)).messages == 6
    # 빠진 것이 없으면 예전처럼 끝난 작업을 그대로 돌려준다
    same = await svc.import_parsed("u", conv.id, _parsed(), EVERYONE)
    assert (same.id, same.imported, same.duplicates) == (back.id, 2, 4)


async def test_graph_only_touches_the_changed_speaker() -> None:
    conversations, svc, store, conv = await _setup("sync", with_graph=True)
    await svc.import_parsed("u", conv.id, _parsed(), EVERYONE)
    assert [a["name"] for a in store.added] == [f"c1:g{i}" for i in range(1, 7)]
    untouched = {a["name"]: a for a in store.added if a["name"] not in ("c1:g3", "c1:g6")}

    r = await svc.set_speaker_roles("u", conv.id, {"최유진": "skip"})
    assert (r.graph_removed, r.graph_requeued, r.messages_removed) == (2, 2, 2)
    assert r.job is not None and (r.job.graph_status, r.job.graph_episodes) == ("completed", 0)
    # 남은 화자의 에피소드는 지우지도 다시 넣지도 않았다 (같은 객체 그대로)
    assert {a["name"]: a for a in store.added} == untouched
    assert all(a is untouched[a["name"]] for a in store.added)
    stats = await conversations.conversation_stats("u", conv.id)
    assert (stats.messages, stats.graph_ingested, stats.graph_pending) == (4, 4, 0)


async def test_graph_reingests_the_whole_room_when_my_name_changes() -> None:
    conversations, svc, store, conv = await _setup("sync", with_graph=True)
    wrong = {"김민수": "contact", "박지현": "me", "최유진": "contact"}
    await svc.import_parsed("u", conv.id, _parsed(), wrong)
    assert "박지현 → 박지현" in next(a for a in store.added if a["name"] == "c1:g1")["body"]

    r = await svc.set_speaker_roles("u", conv.id, {"김민수": "me", "박지현": "contact"})
    # 내 이름이 박지현 → 김민수로 바뀐다. 에피소드 본문마다 내 이름이 있어 최유진의 것도 다시 넣는다
    assert (r.graph_removed, r.graph_requeued, r.me_name) == (6, 6, "김민수")
    bodies = {a["name"]: a["body"] for a in store.added}
    assert len(bodies) == 6
    assert "박지현 → 김민수: 보고서 언제 돼요?" in bodies["c1:g1"]
    assert "김민수 → 박지현: 수요일" in bodies["c1:g2"]
    assert "박지현 → 김민수: 저도 수요일" in bodies["c1:g3"]  # 상대 쪽 화자는 상대 이름으로 들어간다
    assert (await conversations.conversation_stats("u", conv.id)).graph_pending == 0


# ---- API ----

RAW = "\n".join(f"2026-10-05 09:{25 + i} | {who} | {text}" for i, (_, who, text) in enumerate(LINES)) + "\n"


def _room(client: TestClient, h: dict[str, str], speaker_map: dict[str, str]) -> str:
    cid = client.post(
        "/v1/contacts",
        json={"alias": "박지현", "relationship_type": "superior", "use_default_persona": True},
        headers=h,
    ).json()["id"]
    conv_id = client.post("/v1/conversations", json={"contact_id": cid}, headers=h).json()["id"]
    job = client.post(
        f"/v1/conversations/{conv_id}/imports/raw", json={"text": RAW, "speaker_map": speaker_map}, headers=h
    )
    assert job.status_code == 202, job.text
    return conv_id


def test_api_lists_speakers_and_fixes_one(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    conv_id = _room(client, user_a, EVERYONE)
    base = f"/v1/conversations/{conv_id}"
    assert client.get(f"{base}/speakers", headers=user_b).status_code == 404
    assert client.get(f"{base}/speakers", headers=user_a).json() == [
        {"name": "김민수", "speaker_role": "me", "messages": 2},
        {"name": "박지현", "speaker_role": "contact", "messages": 2},
        {"name": "최유진", "speaker_role": "contact", "messages": 2},
    ]
    messages = client.get(f"{base}/messages", headers=user_a).json()
    assert [m["speaker_name"] for m in messages] == [who for _, who, _ in LINES]

    url = f"{base}/speaker-roles"
    anchor = {"message_id": messages[0]["id"], "speaker_role": "me"}
    for bad in (
        {},
        {"speakers": {}},
        {"speakers": {"최유진": "nobody"}},
        {"speakers": {"최유진": "skip"}, **anchor},  # 두 방식을 섞지 않는다
        {"speakers": {"": "skip"}},
        {"speakers": {"가" * 65: "skip"}},
        {"speakers": {"최\x00유진": "skip"}},
        {"message_id": messages[0]["id"]},
    ):
        assert client.put(url, json=bad, headers=user_a).status_code == 422, bad
    unknown = client.put(url, json={"speakers": {"없는사람": "me"}}, headers=user_a)
    assert (unknown.status_code, "없는사람" in unknown.json()["message"]) == (422, True)
    assert client.put(url, json={"speakers": {"최유진": "skip"}}, headers=user_b).status_code == 404

    r = client.put(url, json={"speakers": {"최유진": "skip"}}, headers=user_a)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["changed"], out["messages"], out["messages_removed"], out["my_messages"]) == (True, 0, 2, 2)
    assert (out["me_name"], out["job"]["status"]) == ("김민수", "completed")
    assert [sp["name"] for sp in client.get(f"{base}/speakers", headers=user_a).json()] == [
        "김민수",
        "박지현",
    ]
    stats = client.get(f"{base}/stats", headers=user_a).json()
    assert (stats["messages"], stats["my_messages"]) == (4, 2)
    again = client.put(url, json={"speakers": {"최유진": "skip"}}, headers=user_a).json()
    assert (again["changed"], again["messages_removed"], again["job"]) == (False, 0, None)

    # 방 전체 맞바꾸기(기준 메시지)는 그대로 된다
    swapped = client.put(url, json={**anchor, "me_name": "박지현"}, headers=user_a).json()
    assert (swapped["changed"], swapped["messages"], swapped["messages_removed"]) == (True, 4, 0)
    assert client.get(f"{base}/speakers", headers=user_a).json() == [
        {"name": "김민수", "speaker_role": "contact", "messages": 2},
        {"name": "박지현", "speaker_role": "me", "messages": 2},
    ]


def test_api_export_carries_speaker_names(client: TestClient, user_a: dict[str, str]) -> None:
    _room(client, user_a, WITHOUT_YUJIN)
    exported = client.get("/v1/export", headers=user_a)
    assert exported.status_code == 200, exported.text
    rows = exported.json()["contacts"][0]["conversations"][0]["messages"]
    assert [(m["speaker_name"], m["speaker_role"]) for m in rows] == [
        ("박지현", "contact"),
        ("김민수", "me"),
        ("박지현", "contact"),
        ("김민수", "me"),
    ]
