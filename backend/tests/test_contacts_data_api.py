"""상대·페르소나 확장과 내 데이터 API.

상대 수정(PATCH), 기본 페르소나, 버전 목록, 기본값 표, 문체 추정(suggestion), 요약(summary), 내보내기(export),
보존 기간 정리를 본다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from app.ai.style_suggest import suggest_persona
from app.core.config import Settings
from app.domain.persona import DEFAULT_PERSONAS, PersonaSettings, RelationshipType, apply_adjust
from app.main import create_app
from app.services.housekeeping import run_housekeeping

DRAFT = "오늘 정리해서 내일 오전까지 보내드릴게요."
FORMAL_HISTORY = (
    "2026-10-05 09:25 | contact | 대시보드 개편은 언제까지 가능해요?\n"
    "2026-10-05 09:31 | me | 팀장님, 이번 주 금요일은 어렵고 다음 주 수요일까지는 가능합니다.\n"
    "2026-10-05 09:40 | contact | 목요일까지는 안 될까요?\n"
    "2026-10-05 09:52 | me | 팀장님, 목요일이면 테스트를 못 돌립니다.\n"
    "2026-10-05 10:10 | me | 죄송하지만 수요일로 조정 부탁드립니다.\n"
    "2026-10-06 09:00 | me | 팀장님, 어제 말씀하신 자료 보내드렸습니다.\n"
    "2026-10-06 09:30 | me | 확인 부탁드려요.\n"
)
CASUAL_HISTORY = (
    "2026-10-07 20:00 | contact | 주말에 뭐해?\n"
    "2026-10-07 20:01 | me | 유진아 나 토요일에 시간 돼ㅋㅋ\n"
    "2026-10-07 20:02 | me | 밥 먹자!! 😆\n"
    "2026-10-07 20:05 | me | 유진아 근데 어디서 볼까~\n"
    "2026-10-07 20:06 | me | 강남 어때\n"
)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[arg-type]


def _new_contact(client: TestClient, headers: dict[str, str], alias: str, rel: str, **extra: object) -> dict:
    r = client.post("/v1/contacts", json={"alias": alias, "relationship_type": rel, **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _import(client: TestClient, headers: dict[str, str], contact_id: str, text: str, key: str) -> str:
    conv_id = client.post(
        "/v1/conversations", json={"contact_id": contact_id, "source_key": key}, headers=headers
    ).json()["id"]
    job = client.post(f"/v1/conversations/{conv_id}/imports/raw", json={"text": text}, headers=headers)
    assert job.status_code == 202, job.text
    return conv_id


def test_default_personas_cover_every_relationship_type(client: TestClient, user_a: dict[str, str]) -> None:
    kinds = set(get_args(RelationshipType))
    assert len(kinds) == 7 and set(DEFAULT_PERSONAS) == kinds
    r = client.get("/v1/persona-defaults", headers=user_a)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == kinds
    assert body["superior"]["formality"] == 5 and body["superior"]["forbidden_phrases"] == ["ㅋㅋ", "ㅠ"]
    assert (body["friend"]["formality"], body["friend"]["emoji"]) == (1, "rare")
    assert all(PersonaSettings.model_validate(v) for v in body.values())  # 그대로 PUT에 쓸 수 있는 모양
    assert client.get("/v1/persona-defaults").status_code == 401
    # 평가·시연 스크립트가 쓰는 이름은 같은 표를 가리킨다
    from app.services import synthetic_loader

    assert synthetic_loader.DEFAULT_PERSONAS is DEFAULT_PERSONAS


def test_create_contact_with_default_persona(client: TestClient, user_a: dict[str, str]) -> None:
    plain = _new_contact(client, user_a, "이서연", "colleague")
    assert plain["persona_version"] is None  # 기본 동작은 그대로
    made = _new_contact(client, user_a, "박지현", "superior", use_default_persona=True)
    assert made["persona_version"] == 1
    persona = client.get(f"/v1/contacts/{made['id']}/persona", headers=user_a).json()
    assert persona["version"] == 1
    assert persona["settings"] == DEFAULT_PERSONAS["superior"].model_dump(mode="json")
    # 바로 B 조건 첨삭이 된다
    r = client.post("/v1/rewrites", json={"contact_id": made["id"], "draft_text": DRAFT}, headers=user_a)
    assert r.status_code == 201, r.text
    # 목록은 상대마다 페르소나 버전을 함께 준다
    listed = {c["alias"]: c["persona_version"] for c in client.get("/v1/contacts", headers=user_a).json()}
    assert listed == {"이서연": None, "박지현": 1}
    blank = client.post("/v1/contacts", json={"alias": "   ", "relationship_type": "friend"}, headers=user_a)
    assert blank.status_code == 422


def test_patch_contact_alias_and_relationship(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    c = _new_contact(client, user_a, "이서연", "colleague", use_default_persona=True)
    url = f"/v1/contacts/{c['id']}"

    renamed = client.patch(url, json={"alias": "  이서연 파트장  "}, headers=user_a)
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["alias"], renamed.json()["relationship_type"]) == ("이서연 파트장", "colleague")
    promoted = client.patch(url, json={"relationship_type": "superior"}, headers=user_a).json()
    assert (promoted["alias"], promoted["relationship_type"], promoted["persona_version"]) == (
        "이서연 파트장",
        "superior",
        1,
    )
    both = client.patch(url, json={"alias": "서연", "relationship_type": "friend"}, headers=user_a).json()
    assert (both["alias"], both["relationship_type"]) == ("서연", "friend")
    assert client.get(url, headers=user_a).json()["alias"] == "서연"
    # 빈 본문·null은 그대로 둔다. 관계를 바꿔도 저장된 페르소나는 덮어쓰지 않는다.
    assert client.patch(url, json={}, headers=user_a).json()["alias"] == "서연"
    assert client.patch(url, json={"alias": None}, headers=user_a).json()["alias"] == "서연"
    persona = client.get(f"{url}/persona", headers=user_a).json()
    assert (persona["version"], persona["settings"]["formality"]) == (
        1,
        DEFAULT_PERSONAS["colleague"].formality,
    )

    assert client.patch(url, json={"alias": "   "}, headers=user_a).status_code == 422
    assert client.patch(url, json={"alias": "가" * 51}, headers=user_a).status_code == 422
    assert client.patch(url, json={"relationship_type": "enemy"}, headers=user_a).status_code == 422
    assert client.patch(url, json={"alias": "남의 것"}, headers=user_b).status_code == 404
    assert client.get(url, headers=user_a).json()["alias"] == "서연"


def test_persona_versions_newest_first(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    c = _new_contact(client, user_a, "박지현", "superior")
    url = f"/v1/contacts/{c['id']}/persona"
    assert client.get(f"{url}/versions", headers=user_a).json() == []
    for version, formality in enumerate((5, 4, 3)):
        saved = client.put(
            url, json={"settings": {"formality": formality}, "expected_version": version}, headers=user_a
        )
        assert saved.status_code == 200, saved.text
    versions = client.get(f"{url}/versions", headers=user_a).json()
    assert [(v["version"], v["settings"]["formality"]) for v in versions] == [(3, 3), (2, 4), (1, 5)]
    assert set(versions[0]) == {"contact_id", "version", "settings", "created_at"}
    assert client.get(f"{url}/versions", headers=user_b).status_code == 404


def test_persona_suggestion_from_my_past_messages(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    boss = _new_contact(client, user_a, "박지현", "superior", use_default_persona=True)
    url = f"/v1/contacts/{boss['id']}/persona/suggestion"
    empty = client.get(url, headers=user_a)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"based_on_messages": 0, "settings": None, "signals": []}

    _import(client, user_a, boss["id"], FORMAL_HISTORY, "c1")
    body = client.get(url, headers=user_a).json()
    assert body["based_on_messages"] == 5  # 내 메시지만 센다
    signals = {s["field"]: s for s in body["signals"]}
    assert signals["formality"]["value"] == 4 and "5개 중 4개가 '-습니다'체" in signals["formality"]["reason"]
    assert (
        signals["address_term"]["value"] == "팀장님" and "3개가 '팀장님'" in signals["address_term"]["reason"]
    )
    # 이모지·구어 표지가 다섯 건에 없었다는 것만으로는 근거가 되지 않는다 — 기준값을 건드리지 않는다
    assert "emoji" not in signals and "friendliness" not in signals
    assert body["settings"]["emoji"] == DEFAULT_PERSONAS["superior"].emoji
    assert body["settings"]["friendliness"] == DEFAULT_PERSONAS["superior"].friendliness
    assert body["settings"]["formality"] == 4 and body["settings"]["address_term"] == "팀장님"
    # 추정하지 않는 항목은 저장된 설정 그대로 (금지 표현 등), 저장은 하지 않는다
    assert body["settings"]["forbidden_phrases"] == ["ㅋㅋ", "ㅠ"]
    assert client.get(f"/v1/contacts/{boss['id']}/persona", headers=user_a).json()["version"] == 1
    assert client.get(url, headers=user_b).status_code == 404

    friend = _new_contact(client, user_a, "최유진", "friend")
    conv_id = _import(client, user_a, friend["id"], CASUAL_HISTORY, "c2")
    few = client.get(f"/v1/contacts/{friend['id']}/persona/suggestion", headers=user_a).json()
    # 내 메시지 4건: 표본이 작아 추정하지 않는다 (본 메시지 수만 알려 준다)
    assert few == {"based_on_messages": 4, "settings": None, "signals": []}
    more = "2026-10-07 20:07 | me | 유진아 7시 괜찮아?ㅎㅎ"
    added = client.post(f"/v1/conversations/{conv_id}/imports/raw", json={"text": more}, headers=user_a)
    assert added.status_code == 202, added.text
    casual = client.get(f"/v1/contacts/{friend['id']}/persona/suggestion", headers=user_a).json()
    by_field = {s["field"]: s["value"] for s in casual["signals"]}
    assert casual["based_on_messages"] == 5
    assert by_field["formality"] == 1 and by_field["friendliness"] >= 4
    assert by_field["emoji"] == "often" and by_field["address_term"] == "유진아"
    # 페르소나가 없는 상대는 관계 유형 기본값 위에 얹는다
    assert casual["settings"]["directness"] == DEFAULT_PERSONAS["friend"].directness


def test_suggest_persona_rules() -> None:
    base = PersonaSettings()
    assert suggest_persona([], base, "박지현") == (None, [])
    assert suggest_persona(["   "], base, "박지현") == (None, [])
    assert suggest_persona(["네", "넵", "알겠습니다", "감사합니다"], base, "박지현") == (None, [])  # 4건
    polite, signals = suggest_persona(
        ["네 알겠어요", "내일 봬요~", "감사해요!", "넵", "확인했어요"], base, "이서연"
    )
    assert polite is not None and polite.formality == 3
    # 근거가 있는 것만 낸다: 호칭(반복되는 부르는 말 없음)과 이모지(다섯 건에 없었을 뿐)는 내지 않는다
    assert {s.field for s in signals} == {"formality", "friendliness"}
    assert polite.emoji == base.emoji and polite.address_term == base.address_term
    mixed, _ = suggest_persona(["알겠습니다", "ㅇㅋ 내일 봐", "그래", "고마워요", "응"], base, "이서연")
    assert mixed is not None and mixed.formality == 2
    # '좋아,' '뭐야,' 같은 말은 호칭이 아니다. 별칭에 든 이름의 호격만 받는다.
    none, sig = suggest_persona(
        ["좋아, 그럼 내일 보자", "좋아, 알겠어", "뭐야, 진짜?", "좋아, 가자", "뭐야, 벌써?"], base, "최유진"
    )
    assert none is not None and none.address_term is None and "address_term" not in {s.field for s in sig}
    kin, _ = suggest_persona(["형 내일 뭐해", "형, 밥 먹었어?", "ㅋㅋㅋ", "알았어", "간다"], base, "김준호")
    assert kin is not None and kin.address_term == "형"
    # 아주 긴 줄도 끝부분만 보고 판단한다 (4000자 느낌표 줄에서 정규식이 느려지지 않는다)
    long_formal, _ = suggest_persona(["가" * 3990 + " 확인했습니다", "!" * 4000 + "가"] * 50, base, "박지현")
    assert long_formal is not None and long_formal.formality == 4

    # "한 번도 없었다"는 메시지가 충분히 많을 때만 근거다: 몇 건에 없었다고 저장된 값을 덮어쓰지 않는다
    saved = PersonaSettings(friendliness=4, emoji="often", address_term="선배")
    few, few_signals = suggest_persona(["확인했습니다"] * 6, saved, "박지현")
    assert few is not None and {s.field for s in few_signals} == {"formality"}
    assert (few.friendliness, few.emoji, few.address_term, few.formality) == (4, "often", "선배", 5)
    many, many_signals = suggest_persona(["확인했습니다"] * 20, saved, "박지현")
    assert many is not None and {s.field for s in many_signals} == {"formality", "friendliness", "emoji"}
    assert (many.friendliness, many.emoji, many.address_term) == (2, "none", "선배")
    # 종결 어미를 판단할 수 있는 메시지가 3건 미만이면 격식도 말하지 않는다
    vague, vague_signals = suggest_persona(["ㅇㅇ", "ㄱㄱ", "ㅋㅋㅋ", "!!", "OK"], saved, "박지현")
    assert vague is not None and vague.formality == saved.formality
    assert "formality" not in {s.field for s in vague_signals}


def test_apply_adjust_returns_a_copy() -> None:
    base = PersonaSettings(formality=5)
    assert apply_adjust(base, None) is base and apply_adjust(None, "shorter") is None
    assert (
        apply_adjust(base, "more_formal").formality == 5 and apply_adjust(base, "more_casual").formality == 4
    )
    assert (
        apply_adjust(base, "shorter").length == "shorter" and apply_adjust(base, "longer").length == "longer"
    )
    assert (base.formality, base.length) == (5, "same")  # 원본은 그대로
    with pytest.raises(ValueError):
        apply_adjust(base, "louder")


def test_data_summary_counts_per_contact(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    assert client.get("/v1/data/summary", headers=user_a).json() == {
        "contacts": [],
        "totals": {"contacts": 0, "conversations": 0, "messages": 0, "rewrites": 0},
        "graph_enabled": False,
        "retention_days": 0,
    }
    boss = _new_contact(client, user_a, "박지현", "superior", use_default_persona=True)
    friend = _new_contact(client, user_a, "최유진", "friend")
    _new_contact(client, user_b, "남의 상대", "friend", use_default_persona=True)
    _import(client, user_a, boss["id"], FORMAL_HISTORY, "c1")
    _import(client, user_a, boss["id"], CASUAL_HISTORY, "c1b")
    for _ in range(2):
        client.post("/v1/rewrites", json={"contact_id": boss["id"], "draft_text": DRAFT}, headers=user_a)

    r = client.get("/v1/data/summary", headers=user_a)
    assert r.status_code == 200, r.text
    body = r.json()
    rows = {c["alias"]: c for c in body["contacts"]}
    assert [c["alias"] for c in body["contacts"]] == ["박지현", "최유진"]  # 만든 순
    assert set(rows["박지현"]) == {
        "contact_id",
        "alias",
        "relationship_type",
        "persona_version",
        "conversations",
        "messages",
        "my_messages",
        "graph_ingested",
        "rewrites",
        "last_rewrite_at",
    }
    b = rows["박지현"]
    assert (b["contact_id"], b["relationship_type"], b["persona_version"]) == (boss["id"], "superior", 1)
    assert (b["conversations"], b["messages"], b["my_messages"], b["graph_ingested"]) == (2, 12, 9, 0)
    assert b["rewrites"] == 2 and b["last_rewrite_at"] is not None
    f = rows["최유진"]
    assert (f["contact_id"], f["persona_version"], f["conversations"], f["messages"]) == (
        friend["id"],
        None,
        0,
        0,
    )
    assert (f["rewrites"], f["last_rewrite_at"]) == (0, None)
    assert body["totals"] == {"contacts": 2, "conversations": 2, "messages": 12, "rewrites": 2}
    assert client.get("/v1/data/summary", headers=user_b).json()["totals"]["contacts"] == 1
    assert client.get("/v1/data/summary").status_code == 401

    kept = TestClient(create_app(_settings(rewrite_retention_days=30)))
    assert kept.get("/v1/data/summary", headers=user_a).json()["retention_days"] == 30


def test_export_downloads_everything_i_own(client: TestClient, user_b: dict[str, str]) -> None:
    reg = client.post("/v1/auth/register", json={"username": "exporter", "password": "long-enough-pw"})
    assert reg.status_code == 201, reg.text
    h = {"X-CSRF-Token": reg.json()["csrf_token"]}
    boss = _new_contact(client, h, "박지현", "superior", use_default_persona=True)
    conv_id = _import(client, h, boss["id"], FORMAL_HISTORY, "c1")
    rec = client.post(
        "/v1/rewrites", json={"contact_id": boss["id"], "draft_text": DRAFT, "channel": "desktop"}, headers=h
    ).json()
    client.post(
        f"/v1/rewrites/{rec['id']}/feedback", json={"accepted": False, "reason_code": "too_long"}, headers=h
    )
    other = TestClient(client.app)  # 쿠키가 없는 다른 사용자(헤더 스텁)
    _new_contact(other, user_b, "남의 상대", "friend")

    r = client.get("/v1/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    disposition = r.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="persona-ize-export-') and disposition.endswith(
        '.json"'
    )
    assert len(disposition.split("export-")[1]) == len('YYYYMMDD.json"')
    body = r.json()
    assert set(body) == {"exported_at", "user", "contacts"}
    assert (body["user"]["username"], body["user"]["user_id"]) == ("exporter", reg.json()["user_id"])
    assert body["user"]["created_at"] is not None
    assert "password" not in r.text and "csrf" not in r.text and "남의 상대" not in r.text

    (contact,) = body["contacts"]
    assert (contact["alias"], contact["relationship_type"]) == ("박지현", "superior")
    assert [p["version"] for p in contact["persona_versions"]] == [1]
    (conv,) = contact["conversations"]
    assert (conv["id"], conv["source_key"], len(conv["messages"])) == (conv_id, "c1", 7)
    assert set(conv["messages"][0]) == {"id", "speaker_role", "sent_at", "text"}
    (rewrite,) = contact["rewrites"]
    assert (rewrite["id"], rewrite["condition"], rewrite["channel"]) == (rec["id"], "B", "desktop")
    assert rewrite["result"]["result_text"] == DRAFT
    assert rewrite["feedback"]["reason_code"] == "too_long" and "request_id" not in rewrite["feedback"]

    # 헤더 스텁 사용자는 계정 행이 없다 — 이름 없이 자기 데이터만
    stub = other.get("/v1/export", headers=user_b).json()
    assert (stub["user"]["username"], [c["alias"] for c in stub["contacts"]]) == (None, ["남의 상대"])
    assert other.get("/v1/export").status_code == 401


def test_retention_purges_old_rewrites_through_run_housekeeping(user_a: dict[str, str]) -> None:
    client = TestClient(create_app(_settings(rewrite_retention_days=30)))
    cid = _new_contact(client, user_a, "박지현", "superior", use_default_persona=True)["id"]
    ids = [
        client.post(
            "/v1/rewrites",
            json={"contact_id": cid, "draft_text": DRAFT},
            headers={**user_a, "Idempotency-Key": f"k{i}"},
        ).json()["id"]
        for i in range(3)
    ]
    client.post(f"/v1/rewrites/{ids[0]}/feedback", json={"accepted": True}, headers=user_a)
    state = client.app.state.container
    records = state.rewrites._records  # 인메모리 저장소: 두 건을 40일 전으로 돌린다
    old = datetime.now(UTC) - timedelta(days=40)
    for rid in list(records)[:2]:
        records[rid] = records[rid].model_copy(update={"created_at": old})

    result = asyncio.run(run_housekeeping(state))
    assert set(result) == {"sessions", "import_jobs", "usage_rows", "rewrites"}
    assert result["rewrites"] == 2
    left = client.get("/v1/rewrites", headers=user_a).json()
    assert [r["id"] for r in left] == [ids[2]]
    assert client.get(f"/v1/rewrites/{ids[0]}", headers=user_a).status_code == 404
    # 지워진 요청의 멱등 키는 다시 새 요청이 된다
    again = client.post(
        "/v1/rewrites",
        json={"contact_id": cid, "draft_text": DRAFT},
        headers={**user_a, "Idempotency-Key": "k0"},
    )
    assert again.status_code == 201 and again.json()["id"] != ids[0]

    # 보존 기간이 0(기본)이면 첨삭 이력은 지우지 않는다
    forever = TestClient(create_app(_settings()))
    fid = _new_contact(forever, user_a, "박지현", "superior", use_default_persona=True)["id"]
    forever.post("/v1/rewrites", json={"contact_id": fid, "draft_text": DRAFT}, headers=user_a)
    fstate = forever.app.state.container
    for rid in list(fstate.rewrites._records):
        fstate.rewrites._records[rid] = fstate.rewrites._records[rid].model_copy(update={"created_at": old})
    assert asyncio.run(run_housekeeping(fstate))["rewrites"] == 0
    assert len(forever.get("/v1/rewrites", headers=user_a).json()) == 1
