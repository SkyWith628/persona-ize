"""첨삭 API 확장.

조건 auto, 조정(adjust), 채널, 피드백 읽기·선택형 사유, 근거 원문, 이력 삭제·페이지, 전체 차단기를 본다.
기본 흐름(생성·조회·멱등성·사용자 한도)은 `test_api.py`가 본다. 조건 auto의 D 갈래는 `test_graph_caps.py`.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.ai.providers.fake import ScriptedProvider
from app.core.config import Settings
from app.domain.rewrite import ModelOutput, RewriteInput
from app.main import create_app
from app.services.rewrite_service import _input_hash

DRAFT = "오늘 정리해서 내일 오전까지 보내드릴게요."
PLAIN_HISTORY = (
    "2026-10-05 09:25 | contact | 대시보드 개편은 언제까지 가능해요?\n"
    "2026-10-05 09:31 | me | 팀장님, 이번 주 금요일은 어렵고 다음 주 수요일까지는 가능합니다.\n"
    "2026-10-05 09:40 | contact | 목요일까지는 안 될까요?\n"
    "2026-10-05 09:52 | me | 죄송하지만 수요일로 조정 부탁드립니다.\n"
)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[arg-type]


def _contact(
    client: TestClient, headers: dict[str, str], persona: dict | None = None, alias: str = "박지현"
) -> str:
    cid = client.post(
        "/v1/contacts", json={"alias": alias, "relationship_type": "superior"}, headers=headers
    ).json()["id"]
    if persona is not None:
        saved = client.put(
            f"/v1/contacts/{cid}/persona", json={"settings": persona, "expected_version": 0}, headers=headers
        )
        assert saved.status_code == 200, saved.text
    return cid


def _import_history(client: TestClient, headers: dict[str, str], contact_id: str, key: str = "c1") -> str:
    conv_id = client.post(
        "/v1/conversations", json={"contact_id": contact_id, "source_key": key}, headers=headers
    ).json()["id"]
    job = client.post(
        f"/v1/conversations/{conv_id}/imports/raw", json={"text": PLAIN_HISTORY}, headers=headers
    )
    assert job.status_code == 202, job.text
    return conv_id


def _rewrite(client: TestClient, headers: dict[str, str], contact_id: str, **extra: object):  # noqa: ANN202
    return client.post(
        "/v1/rewrites", json={"contact_id": contact_id, "draft_text": DRAFT, **extra}, headers=headers
    )


def _scripted(client: TestClient, n: int = 4) -> ScriptedProvider:
    """프롬프트를 들여다볼 수 있게 프로바이더를 바꾼다 (출력은 원문 그대로 → 검증 통과)."""
    provider = ScriptedProvider([ModelOutput(rewritten_text=DRAFT)] * n)
    client.app.state.container.workflow.provider = provider
    return provider


def test_auto_condition_follows_available_data(client: TestClient, user_a: dict[str, str]) -> None:
    bare = _contact(client, user_a)  # 페르소나도 대화도 없음 → A
    assert _rewrite(client, user_a, bare, condition="auto").json()["condition"] == "A"

    with_persona = _contact(client, user_a, {"formality": 5, "address_term": "팀장님"}, alias="이서연")
    r = _rewrite(client, user_a, with_persona, condition="auto")
    assert r.status_code == 201, r.text
    assert (r.json()["condition"], r.json()["result"]["retrieval_version"]) == ("B", "none")

    _import_history(client, user_a, with_persona)  # 문체 묶음이 생기면 → C
    r = _rewrite(client, user_a, with_persona, condition="auto")
    assert r.json()["condition"] == "C"
    assert r.json()["result"]["retrieval_version"].startswith("knn-style-v1/")
    # 다른 상대의 자료는 판단에 섞이지 않는다
    assert _rewrite(client, user_a, bare, condition="auto").json()["condition"] == "A"
    # 저장된 값·목록에도 실제 적용된 조건이 남는다
    assert [x["condition"] for x in client.get("/v1/rewrites", headers=user_a).json()] == ["A", "C", "B", "A"]


def test_auto_uses_history_even_without_saved_persona(client: TestClient, user_a: dict[str, str]) -> None:
    """페르소나 없이 대화만 가져온 상대: auto는 C를 고르고(422 아님), 설정 없이 과거 표현만 참고한다."""
    cid = _contact(client, user_a)
    _import_history(client, user_a, cid)
    provider = _scripted(client)
    r = _rewrite(client, user_a, cid, condition="auto")
    assert r.status_code == 201, r.text
    assert r.json()["condition"] == "C"
    prompt = provider.prompts[0]
    assert "상대: 박지현 (상사)" in prompt and "저장된 말투 설정 없음" in prompt
    assert "격식" not in prompt.split("[상황]")[0]  # 지어낸 설정 수치는 없다
    assert "- id=" in prompt.split("[문체 예시")[1]  # 과거 표현은 들어간다
    # 조건을 직접 고르면 예전처럼 페르소나가 필요하다
    explicit = _rewrite(client, user_a, cid, condition="C")
    assert explicit.status_code == 422 and "페르소나" in explicit.json()["message"]
    assert _rewrite(client, user_a, cid, condition="E").status_code == 422


def test_auto_request_is_idempotent_on_the_request_not_the_choice(
    client: TestClient, user_a: dict[str, str]
) -> None:
    cid = _contact(client, user_a, {"formality": 5})
    h = {**user_a, "Idempotency-Key": "auto-1"}
    first = _rewrite(client, h, cid, condition="auto").json()
    assert first["condition"] == "B"
    _import_history(client, user_a, cid)  # 이제 auto는 C를 고르겠지만…
    again = _rewrite(client, h, cid, condition="auto")
    assert again.status_code == 201 and again.json()["id"] == first["id"]  # …같은 키·같은 본문은 기존 결과
    assert again.json()["condition"] == "B"
    assert _rewrite(client, h, cid, condition="B").status_code == 409  # 본문(조건)이 다르면 충돌


def test_input_hash_of_default_requests_is_unchanged() -> None:
    """새 선택 필드(adjust·channel)가 기본값이면 예전과 같은 해시다.

    배포 전에 보낸 요청의 멱등 재시도가 409로 깨지지 않는다.
    """
    inp = RewriteInput(
        user_id="u", contact_id="7d1f6b0e-58c1-4a35-9c35-0f3a1f1c2a10", draft_text=DRAFT, draft_version=1
    )
    legacy = {
        "user_id": "u",
        "contact_id": "7d1f6b0e-58c1-4a35-9c35-0f3a1f1c2a10",
        "draft_text": DRAFT,
        "draft_version": 1,
        "situation": "",
        "condition": "B",
        "persona_version": None,
        "as_of": None,
    }
    expected = hashlib.sha256(json.dumps(legacy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert _input_hash(inp) == expected
    assert _input_hash(inp.model_copy(update={"adjust": "shorter"})) != expected
    assert _input_hash(inp.model_copy(update={"channel": "mobile"})) != expected
    assert _input_hash(inp.model_copy(update={"condition": "auto"})) != expected


def test_adjust_overrides_persona_for_this_request_only(client: TestClient, user_a: dict[str, str]) -> None:
    cid = _contact(client, user_a, {"formality": 3, "address_term": "팀장님"})
    provider = _scripted(client, n=6)

    def settings_block(i: int) -> str:
        return provider.prompts[i].split("[상황]")[0]

    plain = _rewrite(client, user_a, cid)
    assert plain.json()["result"]["adjust"] is None
    assert "격식 3/5" in settings_block(0) and "이번 요청" not in settings_block(0)

    formal = _rewrite(client, user_a, cid, adjust="more_formal")
    assert formal.status_code == 201, formal.text
    assert formal.json()["result"]["adjust"] == "more_formal"
    assert "격식 4/5" in settings_block(1) and "이번 요청: 조금 더 격식 있게 쓴다" in settings_block(1)

    assert _rewrite(client, user_a, cid, adjust="more_casual").status_code == 201
    assert "격식 2/5" in settings_block(2) and "이번 요청: 조금 더 편한 말투로 쓴다" in settings_block(2)
    _rewrite(client, user_a, cid, adjust="shorter")
    assert "길이: 원문보다 짧게" in settings_block(3) and "이번 요청: 지금보다 짧게" in settings_block(3)
    _rewrite(client, user_a, cid, adjust="longer")
    assert "길이: 원문보다 조금 길게" in settings_block(4)

    # A 조건은 페르소나를 쓰지 않지만 조정 요청은 한 줄로 전한다
    _rewrite(client, user_a, cid, condition="A", adjust="shorter")
    assert "상대 정보 없음" in settings_block(5) and "이번 요청: 지금보다 짧게" in settings_block(5)

    # 저장된 페르소나는 그대로다 (버전도 늘지 않는다)
    saved = client.get(f"/v1/contacts/{cid}/persona", headers=user_a).json()
    assert (saved["version"], saved["settings"]["formality"], saved["settings"]["length"]) == (1, 3, "same")
    assert _rewrite(client, user_a, cid, adjust="funnier").status_code == 422


def test_adjust_is_clamped_at_the_ends_of_the_scale(client: TestClient, user_a: dict[str, str]) -> None:
    top = _contact(client, user_a, {"formality": 5})
    bottom = _contact(client, user_a, {"formality": 1}, alias="준호")
    provider = _scripted(client)
    _rewrite(client, user_a, top, adjust="more_formal")
    _rewrite(client, user_a, bottom, adjust="more_casual")
    assert "격식 5/5" in provider.prompts[0] and "격식 1/5" in provider.prompts[1]


def test_channel_is_stored_and_defaults_to_web(client: TestClient, user_a: dict[str, str]) -> None:
    cid = _contact(client, user_a, {"formality": 5})
    assert (
        _rewrite(client, user_a, cid).json()["channel"] == "web"
    )  # 기존 클라이언트는 아무것도 안 보내도 된다
    mobile = _rewrite(client, user_a, cid, channel="mobile")
    assert mobile.status_code == 201 and mobile.json()["channel"] == "mobile"
    assert client.get(f"/v1/rewrites/{mobile.json()['id']}", headers=user_a).json()["channel"] == "mobile"
    assert [r["channel"] for r in client.get("/v1/rewrites", headers=user_a).json()] == ["mobile", "web"]
    assert _rewrite(client, user_a, cid, channel="fax").status_code == 422


def test_feedback_is_returned_with_reason_code(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    cid = _contact(client, user_a, {"formality": 5})
    rec = _rewrite(client, user_a, cid).json()
    assert rec["feedback"] is None
    url = f"/v1/rewrites/{rec['id']}/feedback"
    bad = client.post(url, json={"accepted": False, "reason_code": "meh"}, headers=user_a)
    assert bad.status_code == 422
    fb = client.post(
        url,
        json={
            "accepted": False,
            "reason": "너무 딱딱해요",
            "reason_code": "too_stiff",
            "edited_text": "고친 문장",
        },
        headers=user_a,
    )
    assert fb.status_code == 204
    assert client.post(url, json={"accepted": True}, headers=user_b).status_code == 404

    got = client.get(f"/v1/rewrites/{rec['id']}", headers=user_a).json()["feedback"]
    assert set(got) == {"accepted", "edited_text", "reason", "reason_code", "consent_to_reuse", "created_at"}
    assert (got["accepted"], got["reason_code"], got["reason"], got["edited_text"]) == (
        False,
        "too_stiff",
        "너무 딱딱해요",
        "고친 문장",
    )
    assert got["consent_to_reuse"] is False and got["created_at"] is not None
    assert client.get("/v1/rewrites", headers=user_a).json()[0]["feedback"]["reason_code"] == "too_stiff"

    # 다시 보내면 덮어쓴다 (요청당 하나). 예전 클라이언트처럼 reason_code 없이 보내도 된다.
    assert client.post(url, json={"accepted": True}, headers=user_a).status_code == 204
    again = client.get(f"/v1/rewrites/{rec['id']}", headers=user_a).json()["feedback"]
    assert (again["accepted"], again["reason_code"], again["reason"]) == (True, None, None)


def test_evidence_reads_remaining_messages_and_vanishes_with_the_conversation(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    cid = _contact(client, user_a, {"formality": 5})
    conv_id = _import_history(client, user_a, cid)
    rec = _rewrite(client, user_a, cid, condition="auto", draft_text="수요일까지는 가능합니다.").json()
    result = rec["result"]
    assert (
        rec["condition"] == "C" and len(result["evidence_ids"]) == 1
    )  # fake 모델은 첫 문체 예시를 근거로 든다
    eid = result["evidence_ids"][0]
    refs = result["evidence_refs"][eid]
    assert refs and all(ref.startswith("c1:") for ref in refs)
    # 결과에는 참조만 있고 원문 사본은 없다
    dumped = json.dumps(result, ensure_ascii=False)
    assert "수요일로 조정 부탁드립니다" not in dumped and "대시보드 개편" not in dumped

    ev = client.get(f"/v1/rewrites/{rec['id']}/evidence", headers=user_a)
    assert ev.status_code == 200, ev.text
    body = ev.json()
    assert body["missing"] == 0 and [i["id"] for i in body["items"]] == [eid]
    item = body["items"][0]
    assert item["source_message_ids"] == refs
    assert set(item["messages"][0]) == {"speaker_role", "sent_at", "text"}
    assert [m["sent_at"] for m in item["messages"]] == sorted(m["sent_at"] for m in item["messages"])
    assert "me" in {m["speaker_role"] for m in item["messages"]}
    assert client.get(f"/v1/rewrites/{rec['id']}/evidence", headers=user_b).status_code == 404

    # 근거가 없는 결과(B 조건)는 빈 목록
    b = _rewrite(client, user_a, cid).json()
    assert client.get(f"/v1/rewrites/{b['id']}/evidence", headers=user_a).json() == {
        "items": [],
        "missing": 0,
        "refs_recorded": True,
    }

    # 대화방을 지우면 근거 원문도 사라진다 — 이력에는 수만 남는다
    assert client.delete(f"/v1/conversations/{conv_id}", headers=user_a).status_code == 200
    gone = client.get(f"/v1/rewrites/{rec['id']}/evidence", headers=user_a).json()
    assert gone == {"items": [], "missing": 1, "refs_recorded": True}


def test_evidence_of_results_saved_before_refs_existed_is_marked_not_missing(
    client: TestClient, user_a: dict[str, str]
) -> None:
    """출처 참조를 남기기 전에 만든 결과: '원문이 지워졌다'(missing)가 아니라 '참조가 없다'로 알린다."""
    cid = _contact(client, user_a)
    conv_id = _import_history(client, user_a, cid)
    rec = _rewrite(client, user_a, cid, condition="auto").json()
    assert rec["condition"] == "C" and rec["result"]["evidence_ids"] and rec["result"]["evidence_refs"]

    # 옛 결과를 흉내 낸다: 근거 id는 있는데 참조가 없다 (그때는 payload에 evidence_refs가 없었다)
    rewrites = client.app.state.container.rewrites
    stored = next(r for r in rewrites._records.values() if str(r.id) == rec["id"])
    stored.result.evidence_refs.clear()

    old = client.get(f"/v1/rewrites/{rec['id']}/evidence", headers=user_a).json()
    assert old == {"items": [], "missing": 0, "refs_recorded": False}  # 예전: missing이 근거 수만큼
    # 대화방을 지워도 달라지지 않는다 — 처음부터 찾을 수 없던 것이다
    assert client.delete(f"/v1/conversations/{conv_id}", headers=user_a).status_code == 200
    assert client.get(f"/v1/rewrites/{rec['id']}/evidence", headers=user_a).json() == old


def test_delete_one_rewrite_and_all_for_a_contact(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    cid = _contact(client, user_a, {"formality": 5})
    other = _contact(client, user_a, {"formality": 3}, alias="이서연")
    keep = _rewrite(client, user_a, other).json()
    one = _rewrite(client, {**user_a, "Idempotency-Key": "del-1"}, cid).json()
    client.post(f"/v1/rewrites/{one['id']}/feedback", json={"accepted": True}, headers=user_a)
    for _ in range(2):
        _rewrite(client, user_a, cid)

    assert client.delete(f"/v1/rewrites/{one['id']}", headers=user_b).status_code == 404
    assert client.delete(f"/v1/rewrites/{one['id']}", headers=user_a).status_code == 204
    assert client.get(f"/v1/rewrites/{one['id']}", headers=user_a).status_code == 404
    assert client.delete(f"/v1/rewrites/{one['id']}", headers=user_a).status_code == 404
    # 지운 요청의 멱등 키는 다시 쓸 수 있다 (새 요청으로 처리)
    fresh = _rewrite(client, {**user_a, "Idempotency-Key": "del-1"}, cid)
    assert fresh.status_code == 201 and fresh.json()["id"] != one["id"]

    assert client.delete("/v1/rewrites", headers=user_a).status_code == 422  # contact_id 필수
    assert client.delete(f"/v1/rewrites?contact_id={cid}", headers=user_b).status_code == 404
    bulk = client.delete(f"/v1/rewrites?contact_id={cid}", headers=user_a)
    assert bulk.status_code == 200 and bulk.json() == {"deleted": 3}
    assert client.delete(f"/v1/rewrites?contact_id={cid}", headers=user_a).json() == {"deleted": 0}
    assert [r["id"] for r in client.get("/v1/rewrites", headers=user_a).json()] == [keep["id"]]
    assert client.get(f"/v1/contacts/{cid}", headers=user_a).status_code == 200  # 상대는 그대로


def test_history_pages_backwards_with_before(client: TestClient, user_a: dict[str, str]) -> None:
    cid = _contact(client, user_a, {"formality": 5})
    ids = [_rewrite(client, user_a, cid, draft_text=f"{i}번 문장입니다.").json()["id"] for i in range(5)]
    newest_first = list(reversed(ids))

    page1 = client.get("/v1/rewrites?limit=2", headers=user_a).json()
    assert [r["id"] for r in page1] == newest_first[:2]
    cursor = quote(page1[-1]["created_at"])
    page2 = client.get(f"/v1/rewrites?limit=2&before={cursor}", headers=user_a).json()
    assert [r["id"] for r in page2] == newest_first[2:4]
    page3 = client.get(f"/v1/rewrites?limit=2&before={quote(page2[-1]['created_at'])}", headers=user_a).json()
    assert [r["id"] for r in page3] == newest_first[4:]
    # '+'를 인코딩하지 않아 공백이 된 시간대도 읽는다 / contact_id와 함께 / 못 읽는 값은 422
    raw_cursor = page1[-1]["created_at"].replace("Z", "+00:00")
    loose = client.get(f"/v1/rewrites?limit=2&contact_id={cid}&before={raw_cursor}", headers=user_a)
    assert loose.status_code == 200 and [r["id"] for r in loose.json()] == newest_first[2:4]
    assert client.get("/v1/rewrites?before=yesterday", headers=user_a).status_code == 422
    assert client.get("/v1/rewrites?before=2020-01-01T00:00:00", headers=user_a).json() == []


def test_global_daily_breaker_returns_budget_exceeded(user_a: dict[str, str], user_b: dict[str, str]) -> None:
    client = TestClient(create_app(_settings(usage_global_daily_rewrite_limit=2)))
    a = _contact(client, user_a, {"formality": 5})
    b = _contact(client, user_b, {"formality": 5})
    first = _rewrite(client, {**user_a, "Idempotency-Key": "g1"}, a)
    assert first.status_code == 201
    assert _rewrite(client, user_b, b).status_code == 201  # 전체 합계 2회 → 차단기 작동
    for headers, contact in ((user_a, a), (user_b, b)):
        blocked = _rewrite(client, headers, contact)
        assert blocked.status_code == 429, blocked.text
        assert blocked.json()["code"] == "budget_exceeded" and "서비스 전체" in blocked.json()["message"]
    # 막힌 요청은 사용자 사용량을 쓰지 않았다
    assert client.get("/v1/usage", headers=user_a).json()["rewrites"] == 1
    # 멱등 재요청은 모델을 부르지 않으므로 차단기와 무관하게 기존 결과를 돌려준다
    again = _rewrite(client, {**user_a, "Idempotency-Key": "g1"}, a)
    assert again.status_code == 201 and again.json()["id"] == first.json()["id"]


def test_global_token_breaker_and_user_limit_precedence(
    user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    client = TestClient(create_app(_settings(usage_global_daily_token_limit=1, usage_daily_rewrite_limit=1)))
    a = _contact(client, user_a, {"formality": 5})
    b = _contact(client, user_b, {"formality": 5})
    assert _rewrite(client, user_a, a).status_code == 201  # 토큰 합계가 1을 넘는다
    other = _rewrite(client, user_b, b)
    assert other.status_code == 429 and other.json()["code"] == "budget_exceeded"
    # 자기 한도(1회)도 다 쓴 사용자에게는 개인 한도로 알린다
    mine = _rewrite(client, user_a, a)
    assert mine.status_code == 429 and mine.json()["code"] == "usage_exceeded"
    # 차단기가 꺼져 있으면(기본 0) 아무 영향이 없다
    free = TestClient(create_app(_settings()))
    c = _contact(free, user_a, {"formality": 5})
    assert all(_rewrite(free, user_a, c).status_code == 201 for _ in range(3))
