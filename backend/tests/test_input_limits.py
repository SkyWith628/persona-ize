"""모델을 부른 뒤에야 저장에서 실패하던 입력, 같은 멱등 키의 동시 요청 (2026-11-11 검토 D9).

- 128자를 넘는 `Idempotency-Key`, INTEGER 범위를 넘는 `draft_version`·`persona_version`, 저장할 수 없는
  문자(NUL·깨진 유니코드)는 모델을 부르기 전에 422다. 예전에는 모델 비용을 쓰고 500이었다.
- 같은 키의 요청이 겹치면 모델을 한 번만 부르고 모두 같은 결과를 받는다.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.ai.providers.fake import FakeProvider
from app.core.config import Settings
from app.core.errors import DomainError, InvalidInputError
from app.domain.rewrite import IDEMPOTENCY_KEY_MAX, INT4_MAX, RewriteInput
from app.main import create_app
from app.services.import_service import ImportMessage, ImportPayload, normalize_messages

LONE_SURROGATE = chr(0xD800)  # 짝 없는 대리 문자 — UTF-8로 쓸 수 없다
NUL = chr(0)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[arg-type]


@pytest.fixture(params=["memory", "postgres"])
def api(request: pytest.FixtureRequest):  # noqa: ANN201
    """(저장소 종류, 클라이언트, 프로바이더). 서버 오류는 예외 대신 500 응답으로 본다."""
    overrides = {"database_url": request.getfixturevalue("pg_url")} if request.param == "postgres" else {}
    app = create_app(_settings(**overrides))
    provider = FakeProvider()
    app.state.container.workflow.provider = provider
    with TestClient(app, raise_server_exceptions=False) as client:
        yield request.param, client, provider


def _headers() -> dict[str, str]:
    return {"X-User-Id": f"u-{uuid.uuid4().hex[:10]}"}


def _contact(client: TestClient, h: dict[str, str]) -> str:
    r = client.post("/v1/contacts", json={"alias": "박지현", "relationship_type": "friend"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_values_that_cannot_be_stored_are_rejected_before_the_model_call(api) -> None:  # noqa: ANN001
    _, client, provider = api
    h = _headers()
    base = {"contact_id": _contact(client, h), "draft_text": "안녕하세요", "condition": "A"}

    def post(extra: dict | None = None, **headers: str):  # noqa: ANN202
        return client.post("/v1/rewrites", json={**base, **(extra or {})}, headers={**h, **headers})

    rejected = {
        "키 129자": post(**{"Idempotency-Key": "k" * (IDEMPOTENCY_KEY_MAX + 1)}),
        "키에 제어 문자": post(**{"Idempotency-Key": "abc\tdef"}),
        "draft_version 2^40": post({"draft_version": 2**40}),
        "draft_version 2^31": post({"draft_version": INT4_MAX + 1}),
        "persona_version 2^40": post({"persona_version": 2**40}),
        "persona_version -2^40": post({"persona_version": -(2**40)}),
        "초안에 NUL": post({"draft_text": f"안녕{NUL}하세요"}),
        "상황 설명에 NUL": post({"situation": f"보고{NUL}"}),
    }
    assert {name: r.status_code for name, r in rejected.items()} == dict.fromkeys(rejected, 422)
    assert all(r.json()["code"] == "invalid_input" for r in rejected.values())
    assert provider.calls == 0  # 예전에는 모델을 부른 뒤 저장에서 실패했다
    usage = client.get("/v1/usage", headers=h).json()
    assert (usage["rewrites"], usage["input_tokens"]) == (0, 0)  # 요청 수도 쓰지 않는다

    # 경계값은 그대로 된다
    ok = post({"draft_version": INT4_MAX}, **{"Idempotency-Key": "k" * IDEMPOTENCY_KEY_MAX})
    assert ok.status_code == 201, ok.text
    again = post({"draft_version": INT4_MAX}, **{"Idempotency-Key": "k" * IDEMPOTENCY_KEY_MAX})
    assert again.status_code == 201 and again.json()["id"] == ok.json()["id"]
    assert provider.calls == 1
    # 없는 페르소나 버전은 전처럼 404다 (범위 안의 값)
    assert post({"persona_version": 99, "condition": "B"}).status_code == 404


def test_history_paging_with_extreme_timestamps_is_422(api) -> None:  # noqa: ANN001
    _, client, _ = api
    h = _headers()
    for before in ("0001-01-01T00:00:00%2B14:00", "9999-12-31T23:59:59-14:00", "not-a-date"):
        r = client.get(f"/v1/rewrites?before={before}", headers=h)
        assert r.status_code == 422, (before, r.text)
    assert client.get("/v1/rewrites?before=2026-11-11T03:00:00%2B09:00", headers=h).status_code == 200


def test_unstorable_characters_in_saved_fields_are_422_not_500(api) -> None:  # noqa: ANN001
    _, client, _ = api
    h = _headers()
    cid = _contact(client, h)
    conv = client.post("/v1/conversations", json={"contact_id": cid, "source_key": "c1"}, headers=h).json()
    rid = client.post(
        "/v1/rewrites", json={"contact_id": cid, "draft_text": "안녕", "condition": "A"}, headers=h
    ).json()["id"]

    def raw(method: str, url: str, body: dict):  # noqa: ANN202
        # 깨진 대리 문자는 클라이언트가 JSON 이스케이프(ASCII)로 보낸다
        content = json.dumps(body, ensure_ascii=True).encode("ascii")
        return client.request(method, url, content=content, headers={**h, "Content-Type": "application/json"})

    message = {"id": "m1", "speaker": "me", "sent_at": "2026-10-05T09:00:00+09:00", "text": "본문"}
    for bad in (NUL, LONE_SURROGATE):
        cases = {
            "상대 이름": raw("POST", "/v1/contacts", {"alias": f"이름{bad}", "relationship_type": "friend"}),
            "상대 이름 바꾸기": raw("PATCH", f"/v1/contacts/{cid}", {"alias": f"새{bad}이름"}),
            "대화방 제목": raw(
                "POST", "/v1/conversations", {"contact_id": cid, "title": f"제목{bad}", "source_key": "c2"}
            ),
            "대화방 키": raw("POST", "/v1/conversations", {"contact_id": cid, "source_key": f"c{bad}3"}),
            "내 이름": raw("POST", "/v1/conversations", {"contact_id": cid, "me_name": f"민수{bad}"}),
            "메시지 id": raw(
                "POST",
                f"/v1/conversations/{conv['id']}/imports",
                {"messages": [{**message, "id": f"m{bad}1"}]},
            ),
            "초안": raw("POST", "/v1/rewrites", {"contact_id": cid, "draft_text": f"초안{bad} 내용"}),
            "페르소나 메모": raw("PUT", f"/v1/contacts/{cid}/persona", {"settings": {"notes": f"메모{bad}"}}),
            "페르소나 금지 표현": raw(
                "PUT", f"/v1/contacts/{cid}/persona", {"settings": {"forbidden_phrases": [f"ㅋ{bad}ㅋ"]}}
            ),
            "피드백 편집본": raw(
                "POST", f"/v1/rewrites/{rid}/feedback", {"accepted": False, "edited_text": f"고친{bad} 문장"}
            ),
            "피드백 사유": raw(
                "POST", f"/v1/rewrites/{rid}/feedback", {"accepted": False, "reason": f"사유{bad}"}
            ),
        }
        assert {name: r.status_code for name, r in cases.items()} == dict.fromkeys(cases, 422), repr(bad)

    # 아무것도 저장되지 않았다
    assert [c["alias"] for c in client.get("/v1/contacts", headers=h).json()] == ["박지현"]
    assert client.get(f"/v1/conversations/{conv['id']}/stats", headers=h).json()["messages"] == 0
    assert client.get(f"/v1/contacts/{cid}/persona", headers=h).status_code == 404

    # 메시지 본문의 NUL·깨진 글자는 가져오기를 막지 않는다: NUL은 빼고 깨진 글자는 ?로 바꿔 저장한다
    body = {"messages": [{**message, "text": f"앞{NUL}뒤{LONE_SURROGATE}끝"}]}
    assert raw("POST", f"/v1/conversations/{conv['id']}/imports", body).status_code == 202
    stored = client.get(f"/v1/conversations/{conv['id']}/messages", headers=h).json()
    assert [m["text"] for m in stored] == ["앞뒤?끝"]

    # 원문 가져오기(parse/raw)도 같다: 원문의 깨진 글자는 ?로 읽고, 화자 지정의 이름에 있으면 422
    text = f"2026-10-05 09:25 | 박지현 | 언제{LONE_SURROGATE}까지{NUL} 가능해요?\n"
    preview = raw("POST", "/v1/imports/parse", {"text": text})
    assert preview.status_code == 200, preview.text
    assert [s["text"] for s in preview.json()["sample"]] == ["언제?까지 가능해요?"]
    url = f"/v1/conversations/{conv['id']}/imports/raw"
    for bad in (NUL, LONE_SURROGATE):
        r = raw("POST", url, {"text": text, "speaker_map": {f"박{bad}지현": "contact"}})
        assert r.status_code == 422, r.text
    assert raw("POST", url, {"text": text, "speaker_map": {"박지현": "contact"}}).status_code == 202


def test_normalize_messages_reports_bad_ids_and_cleans_text() -> None:
    good = ImportMessage(id="m1", speaker="me", sent_at="2026-10-05T09:00:00+09:00", text=f"가{NUL}나")
    assert [m.text for m in normalize_messages(ImportPayload(messages=[good]))] == ["가나"]
    bad = good.model_copy(update={"id": f"m{NUL}2"})
    with pytest.raises(InvalidInputError, match=r"messages\[1\]: 메시지 id"):
        normalize_messages(ImportPayload(messages=[good, bad]))


# ---- 같은 멱등 키의 동시 요청 ----------------------------------------------------------------------


class SlowProvider(FakeProvider):
    async def generate(self, *, system: str, user: str):  # noqa: ANN201
        await asyncio.sleep(0.05)
        return await super().generate(system=system, user=user)


async def _try(coro):  # noqa: ANN001, ANN202
    try:
        return await coro
    except DomainError as exc:
        return exc.code


async def _concurrent_same_key(settings: Settings) -> None:
    state = create_app(settings).state.container
    provider = SlowProvider()
    state.workflow.provider = provider
    try:
        user = f"u-{uuid.uuid4().hex[:10]}"
        contact = await state.contacts.create(user, "박지현", "friend")
        inp = RewriteInput(
            user_id=user, contact_id=contact.id, draft_text="안녕하세요", draft_version=1, condition="A"
        )
        service = state.rewrite_service

        # 같은 키·같은 본문 5건이 겹친다: 모델은 한 번만, 모두 같은 결과 (예전: 5번 부르고 4건은 409)
        records = await asyncio.gather(*[_try(service.rewrite(inp, "key-1")) for _ in range(5)])
        assert len({getattr(r, "id", r) for r in records}) == 1 and not isinstance(records[0], str)
        assert provider.calls == 1
        assert (await service.usage(user)).rewrites == 1

        # 같은 키·다른 본문이 겹친다: 먼저 온 것만 처리하고 나머지는 409 (모델을 부르지 않는다)
        other = inp.model_copy(update={"draft_text": "다른 본문입니다"})
        mixed = await asyncio.gather(
            _try(service.rewrite(inp, "key-2")), _try(service.rewrite(other, "key-2"))
        )
        assert not isinstance(mixed[0], str) and mixed[1] == "conflict"
        assert provider.calls == 2

        # 다른 키, 키 없는 요청은 서로 기다리지 않는다
        await asyncio.gather(
            service.rewrite(inp, "key-3"), service.rewrite(inp, "key-4"), service.rewrite(inp, None)
        )
        assert provider.calls == 5
        assert len(service._locks) == 0  # 잠금은 쓰는 동안만 남는다
    finally:
        await state.close()


@pytest.mark.anyio
async def test_concurrent_requests_with_the_same_key_call_the_model_once() -> None:
    await _concurrent_same_key(_settings())


def test_concurrent_requests_with_the_same_key_call_the_model_once_on_postgres(pg_url: str) -> None:
    asyncio.run(_concurrent_same_key(_settings(database_url=pg_url)))


@pytest.mark.anyio
async def test_a_failed_first_request_does_not_block_the_next_one_with_the_same_key() -> None:
    state = create_app(_settings(usage_daily_rewrite_limit=0)).state.container
    contact = await state.contacts.create("u", "박지현", "friend")
    inp = RewriteInput(
        user_id="u", contact_id=contact.id, draft_text="안녕하세요", draft_version=1, condition="A"
    )
    results = await asyncio.gather(*[_try(state.rewrite_service.rewrite(inp, "k")) for _ in range(3)])
    assert results == ["usage_exceeded"] * 3  # 앞 요청이 실패해도 뒤 요청이 멈추지 않는다
    state.settings.usage_daily_rewrite_limit = 5
    assert not isinstance(await _try(state.rewrite_service.rewrite(inp, "k")), str)
