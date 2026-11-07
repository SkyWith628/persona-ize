"""API 흐름: 상대 생성 → 페르소나 저장 → 첨삭 → 조회 → 피드백. 소유권 격리와 멱등성도 본다."""

from __future__ import annotations

from fastapi.testclient import TestClient

DRAFT = "오늘 정리해서 내일 오전까지 보내드릴게요."


def _setup_contact(client: TestClient, headers: dict[str, str]) -> str:
    c = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=headers
    )
    assert c.status_code == 201, c.text
    cid = c.json()["id"]
    p = client.put(
        f"/v1/contacts/{cid}/persona",
        json={
            "settings": {"formality": 5, "address_term": "팀장님", "forbidden_phrases": ["ㅋㅋ"]},
            "expected_version": 0,
        },
        headers=headers,
    )
    assert p.status_code == 200, p.text
    assert p.json()["version"] == 1
    return cid


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["llm_provider"] == "fake"


def test_requires_user_header(client: TestClient) -> None:
    assert client.get("/v1/contacts").status_code == 401


def test_vertical_flow(client: TestClient, user_a: dict[str, str]) -> None:
    cid = _setup_contact(client, user_a)

    r = client.post(
        "/v1/rewrites",
        json={"contact_id": cid, "draft_text": DRAFT, "situation": "업무 보고"},
        headers={**user_a, "Idempotency-Key": "k1"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["result"]["status"] == "recommended"  # fake는 원문 그대로 → 검증 통과
    assert body["result"]["result_text"] == DRAFT
    assert body["result"]["prompt_version"] == "rewrite-v1"

    got = client.get(f"/v1/rewrites/{body['id']}", headers=user_a)
    assert got.status_code == 200
    assert got.json()["id"] == body["id"]

    fb = client.post(f"/v1/rewrites/{body['id']}/feedback", json={"accepted": True}, headers=user_a)
    assert fb.status_code == 204


def test_idempotency(client: TestClient, user_a: dict[str, str]) -> None:
    cid = _setup_contact(client, user_a)
    payload = {"contact_id": cid, "draft_text": DRAFT}
    h = {**user_a, "Idempotency-Key": "same"}
    first = client.post("/v1/rewrites", json=payload, headers=h).json()
    second = client.post("/v1/rewrites", json=payload, headers=h).json()
    assert first["id"] == second["id"]
    conflict = client.post("/v1/rewrites", json={**payload, "draft_text": DRAFT + " 감사합니다."}, headers=h)
    assert conflict.status_code == 409


def test_persona_version_conflict(client: TestClient, user_a: dict[str, str]) -> None:
    cid = _setup_contact(client, user_a)
    r = client.put(
        f"/v1/contacts/{cid}/persona", json={"settings": {}, "expected_version": 0}, headers=user_a
    )
    assert r.status_code == 409


def test_user_isolation(client: TestClient, user_a: dict[str, str], user_b: dict[str, str]) -> None:
    cid = _setup_contact(client, user_a)
    assert client.get(f"/v1/contacts/{cid}", headers=user_b).status_code == 404
    assert client.get("/v1/contacts", headers=user_b).json() == []
    r = client.post("/v1/rewrites", json={"contact_id": cid, "draft_text": DRAFT}, headers=user_b)
    assert r.status_code == 404
    rec = client.post("/v1/rewrites", json={"contact_id": cid, "draft_text": DRAFT}, headers=user_a).json()
    assert client.get(f"/v1/rewrites/{rec['id']}", headers=user_b).status_code == 404


def test_input_validation(client: TestClient, user_a: dict[str, str]) -> None:
    cid = _setup_contact(client, user_a)
    assert (
        client.post("/v1/rewrites", json={"contact_id": cid, "draft_text": "   "}, headers=user_a).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/rewrites", json={"contact_id": cid, "draft_text": "a" * 2001}, headers=user_a
        ).status_code
        == 422
    )


def test_condition_b_requires_persona(client: TestClient, user_a: dict[str, str]) -> None:
    c = client.post(
        "/v1/contacts", json={"alias": "준호", "relationship_type": "family"}, headers=user_a
    ).json()
    assert (
        client.post(
            "/v1/rewrites", json={"contact_id": c["id"], "draft_text": DRAFT}, headers=user_a
        ).status_code
        == 422
    )
    ok = client.post(
        "/v1/rewrites", json={"contact_id": c["id"], "draft_text": DRAFT, "condition": "A"}, headers=user_a
    )
    assert ok.status_code == 201
