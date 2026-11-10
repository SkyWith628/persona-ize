"""사용자별 보관 한도와 길이 한도 (2026-11-11 보안 검토 D8).

- `MAX_CONTACTS_PER_USER`, `MAX_CONVERSATIONS_PER_USER`, `MAX_MESSAGES_PER_USER`를 넘는 요청은 아무것도
  저장하지 않고 422 `quota_exceeded`. API 경로에서만 건다 (스크립트는 서비스를 직접 부른다).
- 피드백의 고친 문장에 길이 한도가 있다. 메시지 목록은 `limit`으로 최근 N건만 받을 수 있다.
인메모리와 PostgreSQL에서 같은 시나리오를 돌린다.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.ai.embeddings import FakeEmbedder
from app.core.config import Settings
from app.main import create_app
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[arg-type]


@pytest.fixture(params=["memory", "postgres"])
def api(request: pytest.FixtureRequest):  # noqa: ANN201
    """한도를 작게 잡은 서버: 상대 2명, 대화방 2개, 메시지 5건."""
    overrides: dict[str, object] = {
        "max_contacts_per_user": 2,
        "max_conversations_per_user": 2,
        "max_messages_per_user": 5,
    }
    if request.param == "postgres":
        overrides["database_url"] = request.getfixturevalue("pg_url")
    with TestClient(create_app(_settings(**overrides))) as client:
        yield client


def _headers() -> dict[str, str]:
    return {"X-User-Id": f"u-{uuid.uuid4().hex[:10]}"}


def _contact(client: TestClient, h: dict[str, str], alias: str = "박지현"):  # noqa: ANN202
    return client.post("/v1/contacts", json={"alias": alias, "relationship_type": "friend"}, headers=h)


def _conversation(client: TestClient, h: dict[str, str], contact_id: str, key: str):  # noqa: ANN202
    return client.post("/v1/conversations", json={"contact_id": contact_id, "source_key": key}, headers=h)


def _lines(start: int, n: int) -> str:
    return "".join(f"2026-10-05 09:{i:02d} | me | {i}번째 메시지입니다\n" for i in range(start, start + n))


def _quota(r) -> str:  # noqa: ANN001
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "quota_exceeded"
    return r.json()["message"]


def test_contact_and_conversation_quotas(api: TestClient) -> None:
    client, h, other = api, _headers(), _headers()
    first = _contact(client, h, "하나").json()["id"]
    assert _contact(client, h, "둘").status_code == 201
    assert "2명까지" in _quota(_contact(client, h, "셋"))
    assert len(client.get("/v1/contacts", headers=h).json()) == 2  # 저장되지 않았다
    assert _contact(client, other, "남의 상대").status_code == 201  # 한도는 사용자마다 따로다

    assert _conversation(client, h, first, "c1").status_code == 201
    assert _conversation(client, h, first, "c2").status_code == 201
    assert "2개까지" in _quota(_conversation(client, h, first, "c3"))
    assert len(client.get("/v1/conversations", headers=h).json()) == 2

    # 지우면 다시 만들 수 있다 (상대를 지우면 그 대화방도 사라진다)
    assert client.delete(f"/v1/contacts/{first}", headers=h).status_code == 200
    again = _contact(client, h, "셋")
    assert again.status_code == 201
    assert _conversation(client, h, again.json()["id"], "c3").status_code == 201


def test_message_quota_counts_only_new_messages_and_stores_nothing_when_exceeded(api: TestClient) -> None:
    client, h = api, _headers()
    cid = _contact(client, h).json()["id"]
    c1 = _conversation(client, h, cid, "c1").json()["id"]
    c2 = _conversation(client, h, cid, "c2").json()["id"]

    def raw(conv: str, text: str, **extra: object):  # noqa: ANN202
        return client.post(f"/v1/conversations/{conv}/imports/raw", json={"text": text, **extra}, headers=h)

    def total() -> int:
        return client.get("/v1/data/summary", headers=h).json()["totals"]["messages"]

    first = raw(c1, _lines(0, 3))
    assert first.status_code == 202 and first.json()["imported"] == 3

    message = _quota(raw(c2, _lines(10, 3)))  # 3 + 3 > 5
    assert "5건까지" in message and "지금 3건" in message and "새로 3건" in message
    assert total() == 3
    assert client.get(f"/v1/conversations/{c2}/stats", headers=h).json()["latest_job"] is None

    # 같은 파일을 다시 올리는 것은 새 메시지가 없으므로 한도와 무관하다
    again = raw(c1, _lines(0, 3))
    assert again.status_code == 202 and again.json()["id"] == first.json()["id"]
    # 겹치는 파일: 새로 들어갈 2건만 센다 (3 + 2 = 5)
    overlap = raw(c1, _lines(1, 4))
    assert overlap.status_code == 202 and (overlap.json()["imported"], overlap.json()["duplicates"]) == (2, 2)
    assert total() == 5

    # 꽉 찼다: JSON 경로도 같은 한도를 본다
    body = {
        "messages": [{"id": "j1", "speaker": "me", "sent_at": "2026-10-06T09:00:00+09:00", "text": "하나 더"}]
    }
    assert "새로 1건" in _quota(client.post(f"/v1/conversations/{c2}/imports", json=body, headers=h))
    assert total() == 5

    # 대화방을 지우면 그만큼 다시 가져올 수 있다
    assert client.delete(f"/v1/conversations/{c1}", headers=h).status_code == 200
    assert raw(c2, _lines(10, 6), keep_recent=5).json()["imported"] == 5
    assert total() == 5


@pytest.mark.anyio
async def test_scripts_calling_the_service_directly_are_not_limited() -> None:
    """합성 적재·평가 스크립트는 한도를 주지 않는다 — 설정과 무관하게 전부 들어간다."""
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    svc = ImportService(contacts, conversations, FakeEmbedder(dim=16))
    boss = await contacts.create("u", "박지현", "superior")
    for k in range(3):
        conv = await svc.create_conversation("u", boss.id, None, "json", f"c{k}")
        payload = ImportPayload(
            messages=[
                ImportMessage(id=f"m{i}", speaker="me", sent_at=f"2026-10-05T09:{i:02d}:00+09:00", text="말")
                for i in range(8)
            ]
        )
        assert (await svc.import_messages("u", conv.id, payload)).imported == 8
    assert await conversations.count_owned("u") == (3, 24)
    assert await contacts.count("u") == 1 and await contacts.count("someone-else") == 0


def test_feedback_edited_text_has_a_length_limit(api: TestClient) -> None:
    client, h = api, _headers()
    cid = _contact(client, h).json()["id"]
    rid = client.post(
        "/v1/rewrites", json={"contact_id": cid, "draft_text": "안녕하세요", "condition": "A"}, headers=h
    ).json()["id"]
    url = f"/v1/rewrites/{rid}/feedback"
    too_long = client.post(url, json={"accepted": False, "edited_text": "가" * 4001}, headers=h)
    assert too_long.status_code == 422 and "4,000자" in too_long.json()["message"]
    assert client.get(f"/v1/rewrites/{rid}", headers=h).json()["feedback"] is None  # 저장되지 않았다
    ok = client.post(url, json={"accepted": False, "edited_text": "가" * 4000}, headers=h)
    assert ok.status_code == 204
    assert len(client.get(f"/v1/rewrites/{rid}", headers=h).json()["feedback"]["edited_text"]) == 4000


def test_messages_can_be_listed_with_a_limit(api: TestClient) -> None:
    client, h = api, _headers()
    cid = _contact(client, h).json()["id"]
    conv = _conversation(client, h, cid, "c1").json()["id"]
    assert (
        client.post(
            f"/v1/conversations/{conv}/imports/raw", json={"text": _lines(0, 5)}, headers=h
        ).status_code
        == 202
    )
    url = f"/v1/conversations/{conv}/messages"
    everything = [m["text"] for m in client.get(url, headers=h).json()]
    assert everything == [f"{i}번째 메시지입니다" for i in range(5)]  # 생략하면 예전처럼 전부, 시각 순
    recent = client.get(url, params={"limit": 2}, headers=h).json()
    assert [m["text"] for m in recent] == everything[-2:]  # 가장 최근 2건, 역시 시각 순
    assert len(client.get(url, params={"limit": 5000}, headers=h).json()) == 5
    for bad in (0, -1, 5001):
        assert client.get(url, params={"limit": bad}, headers=h).status_code == 422
