"""대화방 생성 → 가져오기(화자 지정) → 중복 처리 → C 조건 첨삭에서 문체 예시가 붙는지 (인메모리 저장소)."""

from __future__ import annotations

from fastapi.testclient import TestClient

MESSAGES = [
    {
        "id": "m01",
        "speaker": "박지현",
        "sent_at": "2026-10-05T09:25:00+09:00",
        "text": "대시보드 개편은 언제까지 가능해요?",
    },
    {
        "id": "m02",
        "speaker": "김민수",
        "sent_at": "2026-10-05T09:31:00+09:00",
        "text": "솔직히 이번 주 금요일은 어렵고, 다음 주 수요일까지는 가능합니다.",
    },
    {
        "id": "m03",
        "speaker": "박지현",
        "sent_at": "2026-10-05T09:40:00+09:00",
        "text": "목요일까지는 안 될까요?",
    },
    {
        "id": "m04",
        "speaker": "김민수",
        "sent_at": "2026-10-05T09:52:00+09:00",
        "text": "팀장님, 목요일이면 테스트를 못 돌립니다. 죄송하지만 수요일로 조정 부탁드립니다.",
    },
]
SPEAKER_MAP = {"김민수": "me", "박지현": "contact"}


def _contact(client: TestClient, headers: dict[str, str]) -> str:
    c = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=headers
    )
    cid = c.json()["id"]
    client.put(
        f"/v1/contacts/{cid}/persona",
        json={"settings": {"formality": 5, "address_term": "팀장님"}, "expected_version": 0},
        headers=headers,
    )
    return cid


def test_import_flow_and_condition_c(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    cid = _contact(client, user_a)
    conv = client.post(
        "/v1/conversations", json={"contact_id": cid, "source_key": "c1", "title": "팀장 방"}, headers=user_a
    )
    assert conv.status_code == 201, conv.text
    conv_id = conv.json()["id"]
    # 같은 source_key는 409
    assert (
        client.post(
            "/v1/conversations", json={"contact_id": cid, "source_key": "c1"}, headers=user_a
        ).status_code
        == 409
    )

    # 화자 지정 없이 이름만 주면 422에 이름이 나온다
    bad = client.post(f"/v1/conversations/{conv_id}/imports", json={"messages": MESSAGES}, headers=user_a)
    assert bad.status_code == 422
    assert "박지현" in bad.json()["message"]

    job = client.post(
        f"/v1/conversations/{conv_id}/imports",
        json={"messages": MESSAGES, "speaker_map": SPEAKER_MAP},
        headers=user_a,
    )
    assert job.status_code == 202, job.text
    body = job.json()
    assert (body["status"], body["imported"], body["duplicates"], body["episodes"]) == ("completed", 4, 0, 2)
    assert client.get(f"/v1/imports/{body['id']}", headers=user_a).json()["id"] == body["id"]
    assert client.get(f"/v1/imports/{body['id']}", headers=user_b).status_code == 404

    # 같은 내용 재업로드 → 같은 작업. 메시지 하나 추가 → 겹친 4개는 건너뛰고 1개만
    again = client.post(
        f"/v1/conversations/{conv_id}/imports",
        json={"messages": MESSAGES, "speaker_map": SPEAKER_MAP},
        headers=user_a,
    ).json()
    assert again["id"] == body["id"]
    more = client.post(
        f"/v1/conversations/{conv_id}/imports",
        json={
            "messages": MESSAGES
            + [
                {
                    "id": "m05",
                    "speaker": "me",
                    "sent_at": "2026-10-05T10:10:00+09:00",
                    "text": "감사합니다, 팀장님.",
                }
            ],
            "speaker_map": SPEAKER_MAP,
        },
        headers=user_a,
    ).json()
    assert (more["imported"], more["duplicates"], more["episodes"]) == (1, 4, 2)  # m04·m05 연속 → 한 묶음

    msgs = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
    assert [m["source_key"] for m in msgs] == ["m01", "m02", "m03", "m04", "m05"]
    assert [m["speaker_role"] for m in msgs][:2] == ["contact", "me"]
    assert client.get(f"/v1/conversations/{conv_id}/messages", headers=user_b).status_code == 404
    assert client.get("/v1/conversations", headers=user_b).json() == []

    # C 조건: 검색기 버전이 kNN이고, 첨삭이 정상 동작한다 (fake 모델은 원문 반환)
    r = client.post(
        "/v1/rewrites",
        json={
            "contact_id": cid,
            "draft_text": "이번 주 금요일은 어렵고 다음 주 수요일까지는 가능합니다.",
            "condition": "C",
        },
        headers=user_a,
    )
    assert r.status_code == 201, r.text
    assert r.json()["result"]["retrieval_version"].startswith("knn-style-v1/fake")
    assert r.json()["result"]["status"] == "recommended"
    # B 조건은 검색하지 않는다
    rb = client.post("/v1/rewrites", json={"contact_id": cid, "draft_text": "네 알겠습니다."}, headers=user_a)
    assert rb.json()["result"]["retrieval_version"] == "none"
