"""묶음 첨삭 API (`POST /v1/rewrites/batch`): 같은 초안을 여러 상대에게, 상대마다 그 상대의 말투로.

묶음은 단건 경로를 상대마다 그대로 탄다 — 여기서는 묶음만의 약속(순서, 항목별 실패, 본문 검증, 파생 멱등 키,
이력·사용량이 상대 수만큼)을 본다. 단건 첨삭 자체는 `test_api.py`·`test_rewrites_api.py`가 본다.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.ai.providers.fake import ScriptedProvider
from app.api.schemas import RewriteBatchCreate, RewriteCreate
from app.core.config import Settings
from app.domain.rewrite import IDEMPOTENCY_KEY_MAX, ModelOutput
from app.main import create_app

DRAFT = "오늘 정리해서 내일 오전까지 보내드릴게요."
BOSS = {"formality": 5, "address_term": "팀장님"}
FRIEND = {"formality": 1, "address_term": "준호야"}


def _contact(client: TestClient, headers: dict[str, str], alias: str, persona: dict | None = None) -> str:
    cid = client.post(
        "/v1/contacts", json={"alias": alias, "relationship_type": "superior"}, headers=headers
    ).json()["id"]
    if persona is not None:
        saved = client.put(
            f"/v1/contacts/{cid}/persona", json={"settings": persona, "expected_version": 0}, headers=headers
        )
        assert saved.status_code == 200, saved.text
    return cid


def _batch(client: TestClient, headers: dict[str, str], contact_ids: list[str], **extra: object):  # noqa: ANN202
    return client.post(
        "/v1/rewrites/batch",
        json={"contact_ids": contact_ids, "draft_text": DRAFT, **extra},
        headers=headers,
    )


def _two(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
    return _contact(client, headers, "박지현", BOSS), _contact(client, headers, "최준호", FRIEND)


def test_batch_body_has_the_same_fields_as_the_single_body() -> None:
    """묶음 본문 = 단건 본문에서 contact_id → contact_ids. 단건에만 필드를 더하면 여기서 걸린다."""
    single = {k: v for k, v in RewriteCreate.model_fields.items() if k != "contact_id"}
    batch = {k: v for k, v in RewriteBatchCreate.model_fields.items() if k != "contact_ids"}
    assert set(single) == set(batch)
    for name, field in single.items():
        assert (batch[name].annotation, batch[name].default) == (field.annotation, field.default), name


def test_same_draft_goes_to_each_contact_in_their_own_persona(
    client: TestClient, user_a: dict[str, str]
) -> None:
    boss, friend = _two(client, user_a)
    provider = ScriptedProvider([ModelOutput(rewritten_text=DRAFT)] * 2)
    client.app.state.container.workflow.provider = provider

    r = _batch(client, user_a, [friend, boss])  # 만든 순서와 반대로 보낸다 — 응답은 요청 순서를 따른다
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["contact_id"] for i in items] == [friend, boss]
    assert all(i["error"] is None for i in items)
    assert [i["rewrite"]["contact_id"] for i in items] == [friend, boss]
    prompts = list(provider.prompts)  # 아래 단건 요청이 프롬프트를 더 쌓기 전에 떠 둔다
    # 각 항목은 단건 응답과 같은 모양이다
    single = client.post(
        "/v1/rewrites", json={"contact_id": boss, "draft_text": DRAFT, "condition": "A"}, headers=user_a
    ).json()
    assert all(set(i["rewrite"]) == set(single) for i in items)

    # 상대마다 모델을 한 번씩 부르고, 프롬프트에는 그 상대의 설정만 들어간다 (같은 초안은 둘 다에)
    assert len(prompts) == 2
    by_alias = {alias: next(p for p in prompts if alias in p) for alias in ("박지현", "최준호")}
    assert by_alias["박지현"] is not by_alias["최준호"]
    boss_settings, friend_settings = (by_alias[a].split("[상황]")[0] for a in ("박지현", "최준호"))
    assert "격식 5/5" in boss_settings and "팀장님" in boss_settings
    assert "격식 1/5" in friend_settings and "준호야" in friend_settings
    assert "준호야" not in boss_settings and "팀장님" not in friend_settings
    assert all(DRAFT in p for p in prompts)


def test_shared_fields_apply_to_every_contact(client: TestClient, user_a: dict[str, str]) -> None:
    boss, friend = _two(client, user_a)
    r = _batch(client, user_a, [boss, friend], adjust="shorter", channel="mobile", condition="auto")
    assert r.status_code == 200, r.text
    for item in r.json()["items"]:
        rewrite = item["rewrite"]
        assert (rewrite["channel"], rewrite["condition"], rewrite["result"]["adjust"]) == (
            "mobile",
            "B",
            "shorter",
        )


def test_one_missing_contact_does_not_stop_the_others(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    boss, friend = _two(client, user_a)
    someone_elses = _contact(client, user_b, "남의 상대", BOSS)
    ghost = str(uuid4())

    r = _batch(client, user_a, [boss, ghost, someone_elses, friend])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["contact_id"] for i in items] == [boss, ghost, someone_elses, friend]
    assert [i["rewrite"] is not None for i in items] == [True, False, False, True]
    assert items[0]["error"] is None and items[3]["error"] is None
    for failed in items[1:3]:  # 없는 상대와 남의 상대는 구분하지 않는다 (단건과 같다)
        assert failed["error"]["code"] == "not_found" and failed["error"]["message"]
    # 실패한 상대는 한도를 쓰지 않았고 이력도 남기지 않았다
    assert client.get("/v1/usage", headers=user_a).json()["rewrites"] == 2
    assert len(client.get("/v1/rewrites", headers=user_a).json()) == 2
    assert client.get("/v1/rewrites", headers=user_b).json() == []


def test_item_errors_carry_the_single_route_codes(client: TestClient, user_a: dict[str, str]) -> None:
    boss = _contact(client, user_a, "박지현", BOSS)
    bare = _contact(client, user_a, "설정 없는 상대")  # B 조건에는 저장된 설정이 필요하다 → 단건이면 422
    items = _batch(client, user_a, [bare, boss]).json()["items"]
    assert items[0]["rewrite"] is None and items[0]["error"]["code"] == "invalid_input"
    assert "페르소나" in items[0]["error"]["message"]
    assert items[1]["error"] is None and items[1]["rewrite"]["contact_id"] == boss


def test_unexpected_failure_of_one_contact_is_reported_per_item(
    client: TestClient, user_a: dict[str, str]
) -> None:
    boss, friend = _two(client, user_a)
    service = client.app.state.container.rewrite_service
    real = service.rewrite

    async def flaky(inp, key):  # noqa: ANN001, ANN202
        if str(inp.contact_id) == boss:
            raise RuntimeError("대화 원문이 섞였을 수 있는 예외 문구")
        return await real(inp, key)

    service.rewrite = flaky
    r = _batch(client, user_a, [boss, friend])
    assert r.status_code == 200, r.text
    failed, ok = r.json()["items"]
    assert failed["rewrite"] is None and failed["error"]["code"] == "internal_error"
    assert "예외 문구" not in failed["error"]["message"]  # 예외 문구는 응답에 싣지 않는다
    assert ok["error"] is None and ok["rewrite"]["contact_id"] == friend


def test_each_contact_counts_as_one_request_against_the_limit(user_a: dict[str, str]) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, llm_provider="fake", embedding_provider="fake", usage_daily_rewrite_limit=2
    )
    client = TestClient(create_app(settings))
    ids = [_contact(client, user_a, f"상대{i}", BOSS) for i in range(3)]
    r = _batch(client, user_a, ids)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    done = [i for i in items if i["rewrite"] is not None]
    blocked = [i for i in items if i["error"] is not None]
    assert len(done) == 2 and len(blocked) == 1
    assert blocked[0]["error"]["code"] == "usage_exceeded"
    usage = client.get("/v1/usage", headers=user_a).json()
    assert (usage["rewrites"], usage["remaining_rewrites"]) == (2, 0)


def test_invalid_bodies_are_rejected_as_a_whole(client: TestClient, user_a: dict[str, str]) -> None:
    boss, friend = _two(client, user_a)
    six = [str(uuid4()) for _ in range(6)]
    bad_bodies: list[dict] = [
        {"contact_ids": [], "draft_text": DRAFT},  # 빈 목록
        {"contact_ids": six, "draft_text": DRAFT},  # 5명 초과
        {"contact_ids": [boss, friend, boss], "draft_text": DRAFT},  # 중복
        {"draft_text": DRAFT},  # 목록 없음
        {"contact_id": boss, "draft_text": DRAFT},  # 단건 본문
        {"contact_ids": [boss, "not-a-uuid"], "draft_text": DRAFT},
        {"contact_ids": [boss], "draft_text": DRAFT, "adjust": "funnier"},
    ]
    for body in bad_bodies:
        r = client.post("/v1/rewrites/batch", json=body, headers=user_a)
        assert r.status_code == 422, (body, r.text)
        assert r.json()["code"] == "invalid_input" and r.json()["message"]
    assert "두 번" in client.post("/v1/rewrites/batch", json=bad_bodies[2], headers=user_a).json()["message"]
    # 아무것도 처리하지 않았다
    assert client.get("/v1/usage", headers=user_a).json()["rewrites"] == 0
    assert client.get("/v1/rewrites", headers=user_a).json() == []
    # 5명까지는 받는다
    five = [boss, friend, *(_contact(client, user_a, f"상대{i}", BOSS) for i in range(3))]
    ok = _batch(client, user_a, five)
    assert ok.status_code == 200 and all(i["error"] is None for i in ok.json()["items"])
    # 로그인이 없으면 단건과 같이 401
    anonymous = client.post("/v1/rewrites/batch", json={"contact_ids": [boss], "draft_text": DRAFT})
    assert anonymous.status_code == 401


def test_input_errors_shared_by_every_contact_reject_the_whole_request(
    client: TestClient, user_a: dict[str, str], settings: Settings
) -> None:
    """상대와 무관한 입력 오류는 단건처럼 422다 — 같은 오류가 상대 수만큼 200으로 나가지 않는다."""
    boss = _contact(client, user_a, "박지현", BOSS)
    # 설정 없는 상대: 항목별로 돌았다면 이 상대의 오류('저장된 페르소나가 없습니다')가 섞였다
    bare = _contact(client, user_a, "설정 없는 상대")
    ids = [bare, boss]
    long_situation = "가" * (settings.max_situation_chars + 1)
    bad: list[tuple[dict, dict[str, str]]] = [
        ({"contact_ids": ids, "draft_text": "   "}, {}),
        ({"contact_ids": ids, "draft_text": "가" * (settings.max_draft_chars + 1)}, {}),
        ({"contact_ids": ids, "draft_text": DRAFT, "situation": long_situation}, {}),
        ({"contact_ids": ids, "draft_text": DRAFT}, {"Idempotency-Key": "batch\x7fkey"}),  # 보이지 않는 문자
    ]
    for body, extra in bad:
        r = client.post("/v1/rewrites/batch", json=body, headers={**user_a, **extra})
        assert r.status_code == 422, (body, r.text)
        assert r.json()["code"] == "invalid_input" and "items" not in r.json()
        assert "페르소나" not in r.json()["message"]
        # 단건도 같은 입력을 같은 문구로 거절한다
        single = {k: v for k, v in body.items() if k != "contact_ids"} | {"contact_id": boss}
        one = client.post("/v1/rewrites", json=single, headers={**user_a, **extra})
        assert (one.status_code, one.json()["message"]) == (422, r.json()["message"])
    # 아무것도 처리하지 않았다
    assert client.get("/v1/usage", headers=user_a).json()["rewrites"] == 0
    assert client.get("/v1/rewrites", headers=user_a).json() == []


def test_idempotency_key_is_derived_per_contact(client: TestClient, user_a: dict[str, str]) -> None:
    boss, friend = _two(client, user_a)
    h = {**user_a, "Idempotency-Key": "batch-1"}
    first = _batch(client, h, [boss, friend]).json()["items"]
    assert all(i["error"] is None for i in first)
    ids = [i["rewrite"]["id"] for i in first]
    assert len(set(ids)) == 2

    # 같은 키·같은 본문을 다시 보내면 저장된 결과를 돌려준다 — 모델도 한도도 다시 쓰지 않는다
    provider = ScriptedProvider([])  # 불리면 실패한다
    client.app.state.container.workflow.provider = provider
    again = _batch(client, h, [boss, friend])
    assert again.status_code == 200, again.text
    assert [i["rewrite"]["id"] for i in again.json()["items"]] == ids
    assert provider.prompts == []
    assert client.get("/v1/usage", headers=user_a).json()["rewrites"] == 2
    assert len(client.get("/v1/rewrites", headers=user_a).json()) == 2

    # 파생 키는 `<키>:<contact_id>` — 단건 경로에 그 키를 보내면 같은 결과다
    single = client.post(
        "/v1/rewrites",
        json={"contact_id": boss, "draft_text": DRAFT},
        headers={**user_a, "Idempotency-Key": f"batch-1:{boss}"},
    )
    assert single.status_code == 201 and single.json()["id"] == ids[0]

    # 같은 키에 다른 본문이면 항목마다 충돌로 알린다 (단건의 409)
    changed = _batch(client, h, [boss, friend], draft_text="다른 초안입니다.").json()["items"]
    assert [i["error"]["code"] for i in changed] == ["conflict", "conflict"]

    # 파생 키가 길이 한도를 넘게 되는 키는 요청 전체를 422로 돌려보낸다
    longest = "k" * (IDEMPOTENCY_KEY_MAX - 37)
    assert _batch(client, {**user_a, "Idempotency-Key": longest}, [boss]).status_code == 200
    too_long = _batch(client, {**user_a, "Idempotency-Key": longest + "k"}, [boss])
    assert too_long.status_code == 422 and "Idempotency-Key" in too_long.json()["message"]


def test_retry_with_the_same_key_only_redoes_the_failed_contact(
    client: TestClient, user_a: dict[str, str]
) -> None:
    boss = _contact(client, user_a, "박지현", BOSS)
    late = _contact(client, user_a, "나중에 설정할 상대")
    h = {**user_a, "Idempotency-Key": "batch-retry"}
    first = _batch(client, h, [boss, late]).json()["items"]
    assert first[0]["error"] is None and first[1]["error"]["code"] == "invalid_input"

    saved = client.put(
        f"/v1/contacts/{late}/persona", json={"settings": FRIEND, "expected_version": 0}, headers=user_a
    )
    assert saved.status_code == 200, saved.text
    second = _batch(client, h, [boss, late]).json()["items"]
    assert second[0]["rewrite"]["id"] == first[0]["rewrite"]["id"]  # 끝난 상대는 그대로
    assert second[1]["error"] is None and second[1]["rewrite"]["contact_id"] == late
    assert client.get("/v1/usage", headers=user_a).json()["rewrites"] == 2


def test_both_results_are_saved_to_history_like_single_rewrites(
    client: TestClient, user_a: dict[str, str]
) -> None:
    boss, friend = _two(client, user_a)
    items = _batch(client, user_a, [boss, friend]).json()["items"]
    ids = {i["rewrite"]["id"] for i in items}

    history = client.get("/v1/rewrites", headers=user_a).json()
    assert {r["id"] for r in history} == ids and len(history) == 2
    for item in items:
        rewrite = item["rewrite"]
        assert client.get(f"/v1/rewrites/{rewrite['id']}", headers=user_a).json() == rewrite
        per_contact = client.get(f"/v1/rewrites?contact_id={item['contact_id']}", headers=user_a).json()
        assert [r["id"] for r in per_contact] == [rewrite["id"]]
    # 저장된 결과는 단건처럼 피드백·삭제를 받는다
    one = items[0]["rewrite"]["id"]
    liked = client.post(f"/v1/rewrites/{one}/feedback", json={"accepted": True}, headers=user_a)
    assert liked.status_code == 204
    assert client.delete(f"/v1/rewrites/{one}", headers=user_a).status_code == 204
    assert len(client.get("/v1/rewrites", headers=user_a).json()) == 1
