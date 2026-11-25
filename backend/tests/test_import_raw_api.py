"""원문 가져오기 API: 미리보기(parse) → 화자 지정 저장(raw) → 진행(stats). 개인정보 가림과 한계 조건.

파서 자체는 `test_importers.py`, 메시지 배열(JSON) 가져오기는 `test_import_api.py`가 본다.
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.conversation import SPEAKER_NAME_MAX, fit_speaker_name
from app.main import create_app
from app.services.import_service import ImportMessage, ImportPayload

KAKAO_ANDROID = """박지현 님과 카카오톡 대화
저장한 날짜 : 2026년 10월 20일 오후 3:12

2026년 10월 5일 오전 9:25, 박지현 : 민수씨, 개편은 언제까지 가능해요? 급하면 010-1234-5678로 전화 주세요.
2026년 10월 5일 오전 9:31, 회원님 : 솔직히 이번 주 금요일은 어렵고,
다음 주 수요일까지는 가능합니다.
2026년 10월 5일 오전 9:31, 회원님 : 죄송합니다.
2026년 10월 5일 오전 9:40, 박지현 : 사진
2026년 10월 5일 오전 9:41, 최유진 : (단체방에 낀 다른 사람) 저도 봤어요
2026년 10월 6일 오전 8:00
2026년 10월 6일 오전 8:01, 박지현 : 목요일까지는 안 될까요?
2026년 10월 6일 오전 8:05, 회원님 : 팀장님, 목요일이면 테스트를 못 돌립니다.
앞 메시지에 이어지는 줄
"""
RAW_MAP = {"회원님": "me", "박지현": "contact", "최유진": "skip"}
PC_SNIPPET = (
    "--------------- 2026년 10월 5일 월요일 ---------------\n"
    "[박지현] [오전 9:25] 언제까지 가능해요?\n[김민수] [오전 9:31] 수요일까지 가능합니다.\n"
)
PC_MAP = {"김민수": "me", "박지현": "contact"}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[arg-type]


def _conversation(client: TestClient, headers: dict[str, str], **extra: str) -> tuple[str, str]:
    cid = client.post(
        "/v1/contacts",
        json={"alias": "박지현", "relationship_type": "superior", "use_default_persona": True},
        headers=headers,
    ).json()["id"]
    conv = client.post("/v1/conversations", json={"contact_id": cid, **extra}, headers=headers)
    assert conv.status_code == 201, conv.text
    return cid, conv.json()["id"]


def test_parse_preview_reports_shape_without_saving(client: TestClient, user_a: dict[str, str]) -> None:
    r = client.post("/v1/imports/parse", json={"format": "auto", "text": KAKAO_ANDROID}, headers=user_a)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["format"], body["variant"], body["title_hint"]) == ("kakao", "android", "박지현")
    assert body["messages_total"] == 6
    assert body["date_from"] == "2026-10-05T09:25:00+09:00"
    assert body["date_to"] == "2026-10-06T08:05:00+09:00"
    assert body["speakers"] == [
        {"name": "회원님", "count": 3, "suggested_role": "me"},
        {"name": "박지현", "count": 2, "suggested_role": None},  # 화자가 셋이면 제목만으로 추천하지 않는다
        {"name": "최유진", "count": 1, "suggested_role": None},
    ]
    assert len(body["sample"]) == 6 and set(body["sample"][0]) == {"id", "speaker", "sent_at", "text"}
    # 개인정보는 미리보기에서도 가려서 보여 준다 (저장될 모습 그대로)
    assert body["masked_pii"] == 1 and "[전화번호]" in body["sample"][0]["text"]
    assert (body["skipped_lines"], body["system_lines"], body["problems"], body["chats"]) == (0, 2, [], None)
    assert body["max_messages"] == 5000  # 한 번에 가져올 수 있는 수 (MAX_IMPORT_MESSAGES)
    assert client.get("/v1/conversations", headers=user_a).json() == []  # 저장 없음
    assert client.post("/v1/imports/parse", json={"text": KAKAO_ANDROID}).status_code == 401


def test_parse_sample_is_capped_at_twenty(client: TestClient, user_a: dict[str, str]) -> None:
    text = "".join(f"2026-10-05 09:{i:02d} | 박지현 | {i}번째 메시지\n" for i in range(30))
    body = client.post("/v1/imports/parse", json={"format": "plain", "text": text}, headers=user_a).json()
    assert (body["format"], body["messages_total"], len(body["sample"])) == ("plain", 30, 20)
    assert body["sample"][0]["text"] == "0번째 메시지"


def test_parse_rejects_unreadable_and_oversized_text(user_a: dict[str, str]) -> None:
    client = TestClient(create_app(_settings(max_import_chars=100)))
    too_big = client.post("/v1/imports/parse", json={"text": KAKAO_ANDROID}, headers=user_a)
    assert too_big.status_code == 422 and "너무 깁니다" in too_big.json()["message"]
    junk = client.post("/v1/imports/parse", json={"text": "형식이 없는 글"}, headers=user_a)
    assert junk.status_code == 422 and junk.json()["code"] == "invalid_input"
    assert "형식을 알 수 없습니다" in junk.json()["message"]
    assert (
        client.post("/v1/imports/parse", json={"format": "hwp", "text": "x"}, headers=user_a).status_code
        == 422
    )
    # raw도 같은 상한을 쓴다
    _, conv_id = _conversation(client, user_a)
    raw = client.post(
        f"/v1/conversations/{conv_id}/imports/raw",
        json={"text": KAKAO_ANDROID, "speaker_map": RAW_MAP},
        headers=user_a,
    )
    assert raw.status_code == 422 and "너무 깁니다" in raw.json()["message"]


def test_raw_import_requires_speaker_map_then_skips_and_dedupes(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    cid, conv_id = _conversation(client, user_a)
    url = f"/v1/conversations/{conv_id}/imports/raw"
    preview = client.post("/v1/imports/parse", json={"text": KAKAO_ANDROID}, headers=user_a).json()

    # 화자 지정이 빠지면 아무것도 저장하지 않고 422 (이름 나열)
    missing = client.post(
        url, json={"format": "kakao", "text": KAKAO_ANDROID, "speaker_map": {"회원님": "me"}}, headers=user_a
    )
    assert missing.status_code == 422, missing.text
    assert "박지현" in missing.json()["message"] and "최유진" in missing.json()["message"]
    assert client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json() == []
    # 남의 대화방은 404, 모르는 역할은 422
    body = {"text": KAKAO_ANDROID, "speaker_map": RAW_MAP}
    assert client.post(url, json=body, headers=user_b).status_code == 404
    bad_role = client.post(
        url, json={"text": KAKAO_ANDROID, "speaker_map": {"회원님": "boss"}}, headers=user_a
    )
    assert bad_role.status_code == 422

    job = client.post(url, json={"format": "auto", **body}, headers=user_a)
    assert job.status_code == 202, job.text
    done = job.json()
    # 최유진(skip) 1건 제외 → 5건. 본인 발화 구간 2개 → 묶음 2개
    assert (done["status"], done["imported"], done["duplicates"], done["episodes"]) == ("completed", 5, 0, 2)
    msgs = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
    assert [m["speaker_role"] for m in msgs] == ["contact", "me", "me", "contact", "me"]
    assert msgs[1]["text"] == "솔직히 이번 주 금요일은 어렵고,\n다음 주 수요일까지는 가능합니다."
    assert msgs[4]["text"].endswith("앞 메시지에 이어지는 줄")
    assert "[전화번호]" in msgs[0]["text"] and "010-1234-5678" not in msgs[0]["text"]  # 가려서 저장
    # 같은 분의 두 메시지는 순서가 지켜지고, 저장된 id는 미리보기의 id와 같다
    assert [m["sent_at"] for m in msgs[1:3]] == [
        "2026-10-05T09:31:00+09:00",
        "2026-10-05T09:31:00.001000+09:00",
    ]
    kept_ids = [s["id"] for s in preview["sample"] if s["speaker"] != "최유진"]
    assert [m["source_key"] for m in msgs] == kept_ids

    # 같은 파일을 다시 올리면 같은 작업. 범위가 겹치는 더 긴 내보내기는 새 메시지만 들어간다
    again = client.post(url, json=body, headers=user_a).json()
    assert again["id"] == done["id"]
    longer = KAKAO_ANDROID + "2026년 10월 7일 오후 2:00, 박지현 : 그럼 수요일로 하죠.\n"
    more = client.post(url, json={"text": longer, "speaker_map": RAW_MAP}, headers=user_a).json()
    assert (more["imported"], more["duplicates"]) == (1, 5)

    # C 조건 첨삭이 가져온 본인 발화를 근거로 쓴다
    r = client.post(
        "/v1/rewrites",
        json={"contact_id": cid, "draft_text": "목요일이면 테스트를 못 돌립니다.", "condition": "C"},
        headers=user_a,
    )
    assert r.status_code == 201 and r.json()["result"]["retrieval_version"].startswith("knn-style-v1/fake")


def test_raw_import_keep_recent_all_skipped_and_limit(user_a: dict[str, str]) -> None:
    client = TestClient(create_app(_settings(max_import_messages=3)))
    _, conv_id = _conversation(client, user_a)
    url = f"/v1/conversations/{conv_id}/imports/raw"
    body = {"text": KAKAO_ANDROID, "speaker_map": RAW_MAP}

    # 미리보기가 상한과 넘친 사실을 알려 준다 (화면이 '최근 N건만'을 권할 수 있게)
    preview = client.post("/v1/imports/parse", json={"text": KAKAO_ANDROID}, headers=user_a).json()
    assert (preview["messages_total"], preview["max_messages"]) == (6, 3)
    assert any("keep_recent" in p and "3건까지" in p for p in preview["problems"])
    over = client.post(url, json=body, headers=user_a)  # 5건 > 3건 상한
    assert over.status_code == 422 and "keep_recent" in over.json()["message"]
    nothing = client.post(
        url, json={"text": KAKAO_ANDROID, "speaker_map": dict.fromkeys(RAW_MAP, "skip")}, headers=user_a
    )
    assert nothing.status_code == 422 and "가져올 메시지가 없습니다" in nothing.json()["message"]
    assert client.post(url, json={**body, "keep_recent": 0}, headers=user_a).status_code == 422
    assert client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json() == []

    recent = client.post(url, json={**body, "keep_recent": 2}, headers=user_a)
    assert recent.status_code == 202, recent.text
    assert recent.json()["imported"] == 2
    msgs = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
    assert [m["text"][:5] for m in msgs] == ["목요일까지", "팀장님, "]  # 가장 최근 2건


def test_raw_import_accepts_plain_lines_with_literal_roles(
    client: TestClient, user_a: dict[str, str]
) -> None:
    """붙여넣기 형식: 화자에 me/contact를 직접 쓰면 지정이 필요 없다 (예전 화면 JS가 하던 일)."""
    _, conv_id = _conversation(client, user_a)
    text = "2026-10-05 09:25 | contact | 언제까지 가능해요?\n2026-10-05 09:31 | me | 수요일까지 가능합니다.\n"
    job = client.post(
        f"/v1/conversations/{conv_id}/imports/raw", json={"format": "plain", "text": text}, headers=user_a
    )
    assert job.status_code == 202, job.text
    assert (job.json()["imported"], job.json()["episodes"]) == (2, 1)
    msgs = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
    assert [m["speaker_role"] for m in msgs] == ["contact", "me"]
    # 줄 번호가 아니라 내용으로 id를 만들므로, 다른 내용을 또 붙여 넣어도 서로 덮어쓰지 않는다
    more = "2026-10-06 10:00 | me | 어제는 감사했습니다.\n"
    second = client.post(
        f"/v1/conversations/{conv_id}/imports/raw", json={"text": more}, headers=user_a
    ).json()
    assert (second["imported"], second["duplicates"]) == (1, 0)


def test_conversation_stats_and_me_name_adoption(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    _, conv_id = _conversation(client, user_a)
    empty = client.get(f"/v1/conversations/{conv_id}/stats", headers=user_a).json()
    assert (empty["messages"], empty["episodes"], empty["latest_job"]) == (0, 0, None)
    assert (empty["first_message_at"], empty["last_message_at"]) == (None, None)

    job = client.post(
        f"/v1/conversations/{conv_id}/imports/raw",
        json={"format": "kakao", "text": PC_SNIPPET, "speaker_map": PC_MAP},
        headers=user_a,
    ).json()
    st = client.get(f"/v1/conversations/{conv_id}/stats", headers=user_a)
    assert st.status_code == 200, st.text
    stats = st.json()
    assert (stats["conversation_id"], stats["messages"], stats["my_messages"], stats["episodes"]) == (
        conv_id,
        2,
        1,
        1,
    )
    # 그래프를 쓰지 않는 서버: 대기 0, 상한 아님, 작업은 skipped
    assert (
        stats["graph_enabled"],
        stats["graph_ingested"],
        stats["graph_pending"],
        stats["graph_limited"],
    ) == (
        False,
        0,
        0,
        False,
    )
    assert stats["first_message_at"] == "2026-10-05T09:25:00+09:00"
    assert stats["last_message_at"] == "2026-10-05T09:31:00+09:00"
    assert (stats["latest_job"]["id"], stats["latest_job"]["graph_status"]) == (job["id"], "skipped")
    assert client.get(f"/v1/conversations/{conv_id}/stats", headers=user_b).status_code == 404
    # me로 지정한 화자의 실명이 대화방의 내 이름이 된다 (그래프 에피소드의 화자 이름) — 첫 가져오기에서만
    assert client.get(f"/v1/conversations/{conv_id}", headers=user_a).json()["me_name"] == "김민수"

    # 이미 지정된 이름은 바꾸지 않는다. 회원님은 이름으로 쓰지 않는다.
    _, other = _conversation(client, user_a, me_name="민수")
    client.post(
        f"/v1/conversations/{other}/imports/raw",
        json={"text": PC_SNIPPET, "speaker_map": PC_MAP},
        headers=user_a,
    )
    assert client.get(f"/v1/conversations/{other}", headers=user_a).json()["me_name"] == "민수"
    _, android = _conversation(client, user_a)
    client.post(
        f"/v1/conversations/{android}/imports/raw",
        json={"text": KAKAO_ANDROID, "speaker_map": RAW_MAP},
        headers=user_a,
    )
    assert client.get(f"/v1/conversations/{android}", headers=user_a).json()["me_name"] is None

    # 대화방을 만들 때 보낸 이름도 마찬가지다: 화면은 '나'로 지정한 화자 이름을 me_name으로 보내는데,
    # 안드로이드 내보내기면 그것이 '회원님'이다 — 저장하지 않는다 (그래프 에피소드의 화자가 '회원님'이 된다).
    for generic in ("회원님", " 회원님 ", "나", "me", "   "):
        _, made = _conversation(client, user_a, me_name=generic)
        assert client.get(f"/v1/conversations/{made}", headers=user_a).json()["me_name"] is None, generic
    _, named = _conversation(client, user_a, me_name=" 김민수 ")
    assert client.get(f"/v1/conversations/{named}", headers=user_a).json()["me_name"] == "김민수"


def test_json_import_masks_pii_only_through_the_api(client: TestClient, user_a: dict[str, str]) -> None:
    """API 경로는 가리고, ImportService를 직접 부르는 경로(평가·합성 적재 스크립트)는 그대로 둔다."""
    _, conv_id = _conversation(client, user_a)
    message = {
        "id": "m01",
        "speaker": "me",
        "sent_at": "2026-10-05T09:00:00+09:00",
        "text": "제 번호는 010-1234-5678, 메일은 minsu@example.com 입니다.",
    }
    job = client.post(f"/v1/conversations/{conv_id}/imports", json={"messages": [message]}, headers=user_a)
    assert job.status_code == 202, job.text
    stored = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
    assert stored[0]["text"] == "제 번호는 [전화번호], 메일은 [이메일] 입니다."
    # 같은 내용을 다시 올리면 (가린 뒤 해시가 같으므로) 같은 작업
    again = client.post(f"/v1/conversations/{conv_id}/imports", json={"messages": [message]}, headers=user_a)
    assert again.json()["id"] == job.json()["id"]

    state = client.app.state.container
    _, direct_conv = _conversation(client, user_a)
    payload = ImportPayload(messages=[ImportMessage(**message)])
    asyncio.run(state.import_service.import_messages("user-a", UUID(direct_conv), payload))
    direct = client.get(f"/v1/conversations/{direct_conv}/messages", headers=user_a).json()
    assert direct[0]["text"] == message["text"]  # 직접 호출은 원문 그대로


def test_mask_pii_can_be_turned_off(user_a: dict[str, str]) -> None:
    client = TestClient(create_app(_settings(import_mask_pii=False)))
    preview = client.post("/v1/imports/parse", json={"text": KAKAO_ANDROID}, headers=user_a).json()
    assert preview["masked_pii"] == 0 and "010-1234-5678" in preview["sample"][0]["text"]
    _, conv_id = _conversation(client, user_a)
    client.post(
        f"/v1/conversations/{conv_id}/imports/raw",
        json={"text": KAKAO_ANDROID, "speaker_map": RAW_MAP},
        headers=user_a,
    )
    stored = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
    assert "010-1234-5678" in stored[0]["text"]


def test_telegram_full_export_needs_a_chat_choice(client: TestClient, user_a: dict[str, str]) -> None:
    def chat(chat_id: int, name: str, text: str) -> dict:
        return {
            "name": name,
            "type": "personal_chat",
            "id": chat_id,
            "messages": [
                {"id": 1, "type": "message", "date": "2026-10-05T09:25:00", "from": name, "text": text},
                {"id": 2, "type": "message", "date": "2026-10-05T09:26:00", "from": "김민수", "text": "네!"},
            ],
        }

    export = json.dumps(
        {"chats": {"list": [chat(11, "박지현", "보고서 주세요"), chat(22, "최유진", "밥 먹자")]}}
    )
    listing = client.post(
        "/v1/imports/parse", json={"format": "telegram", "text": export}, headers=user_a
    ).json()
    assert listing["messages_total"] == 0 and [c["id"] for c in listing["chats"]] == ["11", "22"]
    assert listing["chats"][1] == {"id": "22", "name": "최유진", "type": "personal_chat", "messages": 2}
    assert "chat_id" in listing["problems"][0]

    picked = client.post("/v1/imports/parse", json={"text": export, "chat_id": 22}, headers=user_a).json()
    assert (picked["format"], picked["title_hint"], picked["messages_total"], picked["chats"]) == (
        "telegram",
        "최유진",
        2,
        None,
    )
    assert {s["name"]: s["suggested_role"] for s in picked["speakers"]} == {
        "최유진": "contact",
        "김민수": "me",
    }

    _, conv_id = _conversation(client, user_a)
    url = f"/v1/conversations/{conv_id}/imports/raw"
    roles = {"최유진": "contact", "김민수": "me"}
    unchosen = client.post(url, json={"text": export, "speaker_map": roles}, headers=user_a)
    assert unchosen.status_code == 422 and "chat_id" in unchosen.json()["message"]
    ok = client.post(url, json={"text": export, "speaker_map": roles, "chat_id": "22"}, headers=user_a)
    assert ok.status_code == 202 and ok.json()["imported"] == 2
    msgs = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
    assert [m["source_key"] for m in msgs] == ["tg1", "tg2"]


def test_failed_reindex_can_be_retried_with_the_same_upload(
    client: TestClient, user_a: dict[str, str]
) -> None:
    _, conv_id = _conversation(client, user_a)
    state = client.app.state.container
    embed = state.embedder.embed_passages
    url = f"/v1/conversations/{conv_id}/imports/raw"
    body = {"text": PC_SNIPPET, "speaker_map": PC_MAP}

    async def boom(texts):  # noqa: ANN001, ANN202
        raise RuntimeError("embedder down")

    state.embedder.embed_passages = boom
    failed = client.post(url, json=body, headers=user_a).json()
    assert (failed["status"], failed["error_code"]) == ("failed", "reindex_failed")
    state.embedder.embed_passages = embed
    retried = client.post(url, json=body, headers=user_a).json()
    assert (retried["id"], retried["status"], retried["episodes"]) == (failed["id"], "completed", 1)
    assert (retried["imported"], retried["duplicates"]) == (0, 2)  # 메시지는 이미 들어가 있었다


# ---------- 다시 올릴 때 따르지 않은 화자 지정 (`ignored_speaker_roles`) ----------

MORE_PC = PC_SNIPPET + "[박지현] [오전 9:40] 화요일은 어려워요?\n[이서연] [오전 9:41] 저도 궁금해요.\n"


def test_reupload_with_the_same_roles_reports_nothing_ignored(
    client: TestClient, user_a: dict[str, str]
) -> None:
    _, conv_id = _conversation(client, user_a)
    url = f"/v1/conversations/{conv_id}/imports/raw"
    first = client.post(url, json={"text": PC_SNIPPET, "speaker_map": PC_MAP}, headers=user_a).json()
    assert (first["imported"], first["ignored_speaker_roles"]) == (2, [])
    again = client.post(url, json={"text": PC_SNIPPET, "speaker_map": PC_MAP}, headers=user_a).json()
    assert (again["id"], again["imported"], again["ignored_speaker_roles"]) == (first["id"], 2, [])


def test_reupload_with_other_roles_lists_what_was_ignored_and_changes_nothing(
    client: TestClient, user_a: dict[str, str]
) -> None:
    _, conv_id = _conversation(client, user_a)
    url = f"/v1/conversations/{conv_id}/imports/raw"
    first = client.post(url, json={"text": PC_SNIPPET, "speaker_map": PC_MAP}, headers=user_a).json()
    before = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()

    # 같은 파일, 나와 상대를 거꾸로: 역할이 해시에 들어가므로 새 작업이지만 메시지는 전부 건너뛴다
    swapped = client.post(
        url, json={"text": PC_SNIPPET, "speaker_map": {"김민수": "contact", "박지현": "me"}}, headers=user_a
    )
    assert swapped.status_code == 202, swapped.text
    body = swapped.json()
    assert body["id"] != first["id"] and (body["imported"], body["duplicates"]) == (0, 2)
    assert body["ignored_speaker_roles"] == [
        {"name": "김민수", "requested_role": "contact", "stored_role": "me", "messages": 1},
        {"name": "박지현", "requested_role": "me", "stored_role": "contact", "messages": 1},
    ]
    assert client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json() == before
    # 저장하지 않는 값이다 — 작업을 다시 읽으면 빈 목록
    assert client.get(f"/v1/imports/{body['id']}", headers=user_a).json()["ignored_speaker_roles"] == []

    # 제외(skip)로 골라도 이미 가져온 메시지는 남는다 — 그것도 알린다
    skipped = client.post(
        url, json={"text": PC_SNIPPET, "speaker_map": {"김민수": "me", "박지현": "skip"}}, headers=user_a
    ).json()
    assert skipped["ignored_speaker_roles"] == [
        {"name": "박지현", "requested_role": "skip", "stored_role": "contact", "messages": 1}
    ]
    assert len(client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()) == 2


def test_new_speakers_and_new_messages_are_not_reported_as_ignored(
    client: TestClient, user_a: dict[str, str]
) -> None:
    _, conv_id = _conversation(client, user_a)
    url = f"/v1/conversations/{conv_id}/imports/raw"
    client.post(url, json={"text": PC_SNIPPET, "speaker_map": PC_MAP}, headers=user_a)

    # 이어진 파일: 박지현을 '나'로 달리 지정했고 새 화자(이서연)가 섞였다. 박지현의 새 메시지는 지정대로
    # 들어가므로 세지 않고, 이미 있던 한 건만 알린다. 새 화자는 따르지 않은 것이 없다.
    more = client.post(
        url,
        json={"text": MORE_PC, "speaker_map": {"김민수": "me", "박지현": "me", "이서연": "contact"}},
        headers=user_a,
    ).json()
    assert (more["imported"], more["duplicates"]) == (2, 2)
    assert more["ignored_speaker_roles"] == [
        {"name": "박지현", "requested_role": "me", "stored_role": "contact", "messages": 1}
    ]
    speakers = client.get(f"/v1/conversations/{conv_id}/speakers", headers=user_a).json()
    assert {(sp["name"], sp["speaker_role"], sp["messages"]) for sp in speakers} == {
        ("김민수", "me", 1),
        ("박지현", "contact", 1),
        ("박지현", "me", 1),
        ("이서연", "contact", 1),
    }
    # 한 이름에 두 역할이 저장된 뒤에는 저장된 역할마다 한 줄
    mixed = client.post(
        url,
        json={"text": MORE_PC, "speaker_map": {"김민수": "me", "박지현": "skip", "이서연": "contact"}},
        headers=user_a,
    ).json()
    assert mixed["ignored_speaker_roles"] == [
        {"name": "박지현", "requested_role": "skip", "stored_role": "contact", "messages": 1},
        {"name": "박지현", "requested_role": "skip", "stored_role": "me", "messages": 1},
    ]


def test_messages_stored_without_a_speaker_name_are_left_out_of_the_report(
    client: TestClient, user_a: dict[str, str]
) -> None:
    """0011 이전처럼 이름 없이(역할 낱말로) 들어온 메시지는 누구 것인지 몰라 세지 않는다."""
    _, conv_id = _conversation(client, user_a)
    sample = client.post("/v1/imports/parse", json={"text": PC_SNIPPET}, headers=user_a).json()["sample"]
    nameless = [
        {"id": m["id"], "speaker": PC_MAP[m["speaker"]], "sent_at": m["sent_at"], "text": m["text"]}
        for m in sample
    ]
    saved = client.post(
        f"/v1/conversations/{conv_id}/imports", json={"messages": nameless}, headers=user_a
    ).json()
    assert saved["imported"] == 2 and saved["ignored_speaker_roles"] == []

    def names() -> list[str | None]:
        msgs = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
        return [m["speaker_name"] for m in msgs]

    assert names() == [None, None]
    url = f"/v1/conversations/{conv_id}/imports/raw"
    flipped = {"김민수": "contact", "박지현": "me"}
    first = client.post(url, json={"text": PC_SNIPPET, "speaker_map": flipped}, headers=user_a).json()
    assert (first["imported"], first["duplicates"], first["ignored_speaker_roles"]) == (0, 2, [])
    # 그 업로드가 이름을 채웠으므로(역할은 그대로) 다음부터는 알린다
    assert names() == ["박지현", "김민수"]
    second = client.post(url, json={"text": PC_SNIPPET, "speaker_map": flipped}, headers=user_a).json()
    assert [(ig["name"], ig["stored_role"]) for ig in second["ignored_speaker_roles"]] == [
        ("김민수", "me"),
        ("박지현", "contact"),
    ]


# ---------- 64자를 넘는 화자 이름 (`fit_speaker_name`, 2026-11-25) ----------

LONG_STEM = "Very_Long_Display_Name_For_A_Speaker_In_The_Cloud_Platform_Engineering_Team"
LONG_A, LONG_B = LONG_STEM + "_Alpha", LONG_STEM + "_Beta"  # 앞 64자가 같은 두 사람
LONG_TEXT = (
    f"2026-10-05 09:25 | {LONG_A} | 언제까지 가능해요?\n"
    f"2026-10-05 09:26 | {LONG_B} | 저도 궁금해요.\n"
    "2026-10-05 09:27 | 김민수 | 수요일까지 가능합니다.\n"
)


def test_fit_speaker_name_keeps_short_names_and_tells_long_ones_apart() -> None:
    assert fit_speaker_name("  박지현 ") == "박지현"
    assert fit_speaker_name("가" * SPEAKER_NAME_MAX) == "가" * SPEAKER_NAME_MAX  # 딱 맞으면 그대로
    a, b = fit_speaker_name(LONG_A), fit_speaker_name(LONG_B)
    assert len(a) == len(b) == SPEAKER_NAME_MAX and a != b
    assert a.startswith(LONG_STEM[:57] + "…")  # 줄인 이름임이 보인다
    assert fit_speaker_name(a) == a and fit_speaker_name(LONG_A + " ") == a  # 다시 맞춰도 같다
    # 자른 자리가 공백이면 떼고 붙인다 (64자를 넘기지 않는다)
    spaced = fit_speaker_name("가" * 56 + " " + "나" * 20)
    assert spaced.startswith("가" * 56 + "…") and len(spaced) == SPEAKER_NAME_MAX - 1


def _long_names_stay_consistent(client: TestClient, headers: dict[str, str]) -> None:
    """미리보기·화자 지정·저장·역할 고치기·따르지 않은 지정이 긴 이름을 같은 꼴로 쓰고, 두 사람을 가른다."""
    a, b = fit_speaker_name(LONG_A), fit_speaker_name(LONG_B)
    _, conv_id = _conversation(client, headers)
    preview = client.post("/v1/imports/parse", json={"text": LONG_TEXT}, headers=headers).json()
    assert sorted(sp["name"] for sp in preview["speakers"]) == sorted([a, b, "김민수"])

    url = f"/v1/conversations/{conv_id}/imports/raw"
    first_map = {a: "contact", b: "contact", "김민수": "me"}
    saved = client.post(url, json={"text": LONG_TEXT, "speaker_map": first_map}, headers=headers)
    assert saved.status_code == 202, saved.text
    assert saved.json()["imported"] == 3

    def speakers() -> list[tuple[str, str, int]]:
        rows = client.get(f"/v1/conversations/{conv_id}/speakers", headers=headers).json()
        return sorted((sp["name"], sp["speaker_role"], sp["messages"]) for sp in rows)

    # 예전에는 둘 다 앞 64자로 저장돼 한 사람(메시지 2건)이 됐다
    assert speakers() == sorted([(a, "contact", 1), (b, "contact", 1), ("김민수", "me", 1)])

    # 원래 이름(64자 초과)으로 적은 지정도 같은 화자로 읽는다. 따르지 않은 지정의 이름은 저장된 이름과 같다
    other_map = {LONG_A: "me", LONG_B: "skip", "김민수": "me"}
    again = client.post(url, json={"text": LONG_TEXT, "speaker_map": other_map}, headers=headers)
    assert again.status_code == 202, again.text
    ignored = again.json()["ignored_speaker_roles"]
    assert sorted((ig["name"], ig["requested_role"]) for ig in ignored) == sorted([(a, "me"), (b, "skip")])

    # 한 사람만 제외하면 그 사람 메시지만 지워진다 (예전에는 두 사람 것이 함께 지워졌다)
    fixed = client.put(
        f"/v1/conversations/{conv_id}/speaker-roles", json={"speakers": {b: "skip"}}, headers=headers
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["messages_removed"] == 1
    assert speakers() == sorted([(a, "contact", 1), ("김민수", "me", 1)])


def test_long_speaker_names_stay_consistent(client: TestClient, user_a: dict[str, str]) -> None:
    _long_names_stay_consistent(client, user_a)


def test_long_speaker_names_stay_consistent_on_postgres(pg_url: str) -> None:
    with TestClient(create_app(_settings(database_url=pg_url))) as client:
        assert client.get("/healthz").json()["storage"] == "postgres"
        _long_names_stay_consistent(client, {"X-User-Id": f"long-names-{uuid4().hex[:8]}"})
