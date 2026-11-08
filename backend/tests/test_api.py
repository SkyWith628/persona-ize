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


def test_rewrite_history(client: TestClient, user_a: dict[str, str], user_b: dict[str, str]) -> None:
    cid = _setup_contact(client, user_a)
    cid2 = _setup_contact(client, user_a)
    for text, c in ((DRAFT, cid), ("네 알겠습니다.", cid), ("금요일에 봬요.", cid2)):
        assert (
            client.post(
                "/v1/rewrites", json={"contact_id": c, "draft_text": text}, headers=user_a
            ).status_code
            == 201
        )
    all_rows = client.get("/v1/rewrites", headers=user_a).json()
    assert [r["result"]["result_text"] for r in all_rows] == [
        "금요일에 봬요.",
        "네 알겠습니다.",
        DRAFT,
    ]  # 최근 순
    assert all(r["condition"] == "B" for r in all_rows)
    a_row = client.post(
        "/v1/rewrites", json={"contact_id": cid, "draft_text": DRAFT, "condition": "A"}, headers=user_a
    )
    assert a_row.json()["condition"] == "A"
    assert client.get("/v1/rewrites?limit=1", headers=user_a).json()[0]["condition"] == "A"
    only = client.get(f"/v1/rewrites?contact_id={cid}&limit=1", headers=user_a).json()
    assert len(only) == 1 and only[0]["result"]["result_text"] == DRAFT and only[0]["condition"] == "A"
    assert client.get("/v1/rewrites", headers=user_b).json() == []
    assert client.get("/v1/rewrites?limit=0", headers=user_a).status_code == 422


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


def test_usage_limits_and_status(settings, user_a: dict[str, str], user_b: dict[str, str]) -> None:  # noqa: ANN001
    """하루 요청 한도 2회 → 3번째 429. 멱등 재요청은 세지 않는다. 다른 사용자는 별도 집계."""
    from app.core.config import Settings
    from app.main import create_app

    small = Settings(
        _env_file=None, llm_provider="fake", embedding_provider="fake", usage_daily_rewrite_limit=2
    )  # type: ignore[call-arg]
    client = TestClient(create_app(small))
    cid = _setup_contact(client, user_a)
    payload = {"contact_id": cid, "draft_text": DRAFT}
    assert client.get("/v1/usage", headers=user_a).json()["remaining_rewrites"] == 2
    first = client.post("/v1/rewrites", json=payload, headers={**user_a, "Idempotency-Key": "u1"})
    assert first.status_code == 201
    # 같은 키 재요청: 모델을 안 부르니 세지 않음
    assert (
        client.post("/v1/rewrites", json=payload, headers={**user_a, "Idempotency-Key": "u1"}).status_code
        == 201
    )
    assert client.get("/v1/usage", headers=user_a).json()["rewrites"] == 1
    assert client.post("/v1/rewrites", json=payload, headers=user_a).status_code == 201
    third = client.post("/v1/rewrites", json=payload, headers=user_a)
    assert third.status_code == 429, third.text
    assert third.json()["code"] == "usage_exceeded"
    status = client.get("/v1/usage", headers=user_a).json()
    assert (status["rewrites"], status["remaining_rewrites"], status["rewrite_limit"]) == (2, 0, 2)
    assert status["input_tokens"] >= 0
    # 다른 사용자는 영향 없음
    cid_b = _setup_contact(client, user_b)
    assert (
        client.post(
            "/v1/rewrites", json={"contact_id": cid_b, "draft_text": DRAFT}, headers=user_b
        ).status_code
        == 201
    )


def test_usage_token_limit_blocks_next_request(user_a: dict[str, str]) -> None:
    """토큰 한도는 다음 요청부터 막는다 (한 번의 초과는 허용)."""
    from app.core.config import Settings
    from app.main import create_app

    tiny = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", usage_daily_token_limit=1)  # type: ignore[call-arg]
    client = TestClient(create_app(tiny))
    cid = _setup_contact(client, user_a)
    assert (
        client.post("/v1/rewrites", json={"contact_id": cid, "draft_text": DRAFT}, headers=user_a).status_code
        == 201
    )
    blocked = client.post("/v1/rewrites", json={"contact_id": cid, "draft_text": DRAFT}, headers=user_a)
    assert blocked.status_code == 429
    assert "토큰" in blocked.json()["message"]


def test_web_index_and_static(client: TestClient) -> None:
    """데모 화면이 서빙된다 (인증 없이 HTML·JS·CSS는 열리고, API는 로그인 뒤)."""
    r = client.get("/")
    assert r.status_code == 200 and "Persona_ize" in r.text and "/static/app.js" in r.text
    assert client.get("/static/app.js").status_code == 200
    assert "text/css" in client.get("/static/app.css").headers["content-type"]
