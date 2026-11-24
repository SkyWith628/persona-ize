"""`scripts/load_kakao_demo.py`: 한 사용자의 데이터를 카카오톡 내보내기 파일들로 다시 채운다.

메모리 저장소 + 합성 대화(iOS .txt 꼴)로 본다. 그래프는 `FakeGraphStore` — 모델을 부르지 않는다.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from app.api.deps import build_state
from app.core.config import Settings
from app.graph.ingest import GraphIngestService
from tests.test_graph import FakeGraphStore

pytestmark = pytest.mark.anyio

ME = "김민수"

BOSS = """박지현 님과 카카오톡 대화
저장한 날짜 : 2025. 11. 20. 오후 3:12

2025년 11월 3일 월요일
2025. 11. 3. 오전 9:02, 박지현 : 민수씨, 개편안은 언제까지 가능해요?
2025. 11. 3. 오전 9:05, 김민수 : 팀장님, 수요일 오전까지 드리겠습니다.
2025. 11. 3. 오전 9:06, 박지현 : 네 그렇게 해요.
2025년 11월 4일 화요일
2025. 11. 4. 오후 2:10, 김민수 : 팀장님, 초안 공유드렸습니다. 확인 부탁드립니다.
"""

BROTHER = """김준호 님과 카카오톡 대화
저장한 날짜 : 2025. 11. 20. 오후 3:15

2025년 11월 5일 수요일
2025. 11. 5. 오후 8:00, 김민수 : 준호야 주말에 집에 오냐
2025. 11. 5. 오후 8:03, 김준호 : 토요일에 갈게
2025. 11. 5. 오후 8:04, 김민수 : ㅇㅋ 엄마한테 말해둘게
"""

GROUP = """동기방 님과 카카오톡 대화
저장한 날짜 : 2025. 11. 20. 오후 3:20

2025년 11월 6일 목요일
2025. 11. 6. 오후 12:00, 이서연 : 점심 뭐 먹을래
2025. 11. 6. 오후 12:01, 최동훈 : 나는 국밥
2025. 11. 6. 오후 12:02, 김민수 : 나도 국밥 좋아
"""


def _script():  # noqa: ANN202
    """`scripts/load_kakao_demo.py`를 모듈로 읽는다 (저장소 루트의 scripts는 패키지가 아니다)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "load_kakao_demo.py"
    if not path.is_file():
        pytest.skip("scripts/load_kakao_demo.py가 없는 설치입니다 (backend만 복사한 이미지).")
    spec = importlib.util.spec_from_file_location("pz_load_kakao_demo_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module  # dataclass가 모듈을 찾는다
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def state():  # noqa: ANN201
    settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]
    s = build_state(settings)
    try:
        yield s
    finally:
        await s.close()


async def _demo_user(state) -> str:  # noqa: ANN001
    result = await state.auth_service.register("demo", "persona-demo-2026")
    return str(result.user.id)


def _config(tmp_path: Path, *, group: dict | None = None) -> Path:
    (tmp_path / "boss.txt").write_text(BOSS, encoding="utf-8")
    (tmp_path / "bro.txt").write_text(BROTHER, encoding="utf-8")
    conversations = [
        {
            "file": "boss.txt",
            "contact": "박지현",
            "relationship_type": "superior",
            "address_term": "팀장님",
            "title": "박지현 팀장",
            "source_key": "k1",
        },
        {
            "file": "bro.txt",
            "contact": "김준호",
            "relationship_type": "family",
            "address_term": "준호야",
            "title": "동생",
            "source_key": "k2",
        },
    ]
    if group is not None:
        (tmp_path / "group.txt").write_text(GROUP, encoding="utf-8")
        conversations.append(
            {"file": "group.txt", "contact": "이서연", "relationship_type": "colleague", "source_key": "k3"}
            | group
        )
    path = tmp_path / "demo.json"
    path.write_text(json.dumps({"me": ME, "conversations": conversations}, ensure_ascii=False), "utf-8")
    return path


def _args(config: Path, **over) -> argparse.Namespace:  # noqa: ANN003
    base = {"config": config, "user": "demo", "dry_run": False, "yes": True, "graph": False}
    return argparse.Namespace(**(base | {"keep_existing": False} | over))


async def _snapshot(state, user_id: str) -> dict[str, tuple]:  # noqa: ANN001
    """별칭 → (관계, 호칭, 페르소나 버전, 방 키, 내 이름, 메시지 수, 내 메시지 수, 묶음 수)."""
    out = {}
    convs = await state.conversations.list(user_id)
    for contact in await state.contacts.list(user_id):
        persona = await state.contacts.latest_persona(user_id, contact.id)
        for conv in [c for c in convs if c.contact_id == contact.id]:
            s = await state.conversations.conversation_stats(user_id, conv.id)
            out[contact.alias] = (
                contact.relationship_type,
                persona.settings.address_term,
                persona.version,
                conv.source_key,
                conv.me_name,
                s.messages,
                s.my_messages,
                s.episodes,
            )
    return out


async def test_dry_run_reads_the_files_and_changes_nothing(state, tmp_path, capsys) -> None:  # noqa: ANN001
    mod = _script()
    user_id = await _demo_user(state)
    old = await state.contacts.create(user_id, "예전 상대", "friend")

    assert await mod.run(_args(_config(tmp_path), dry_run=True, yes=False), state) == 0

    assert [c.id for c in await state.contacts.list(user_id)] == [old.id]
    assert await state.conversations.list(user_id) == []
    printed = capsys.readouterr().out
    assert "박지현 2건" in printed and "김민수 2건" in printed and "메시지 4건" in printed
    assert "못 읽은 줄 0" in printed and "바꾼 것 없음" in printed


async def test_load_replaces_contacts_with_personas_and_messages(state, tmp_path, capsys) -> None:  # noqa: ANN001
    mod = _script()
    user_id = await _demo_user(state)
    await state.contacts.create(user_id, "예전 상대", "friend")

    assert await mod.run(_args(_config(tmp_path)), state) == 0

    snap = await _snapshot(state, user_id)
    assert set(snap) == {"박지현", "김준호"}  # 예전 상대는 지워졌다
    assert snap["박지현"][:7] == ("superior", "팀장님", 1, "k1", ME, 4, 2)
    assert snap["김준호"][:7] == ("family", "준호야", 1, "k2", ME, 3, 2)
    assert snap["박지현"][7] > 0 and snap["김준호"][7] > 0
    boss = next(c for c in await state.contacts.list(user_id) if c.alias == "박지현")
    persona = await state.contacts.latest_persona(user_id, boss.id)
    assert persona.settings.formality == 5  # 관계 유형 기본값에 호칭만 덮어쓴다
    # API용 설정은 되돌린다
    assert state.import_service.graph_mode == "off"
    assert state.import_service.index_wait == Settings(_env_file=None).import_index_wait_seconds  # type: ignore[call-arg]
    assert "실패:" not in capsys.readouterr().out


async def test_file_with_unexpected_speakers_is_reported_and_not_saved(state, tmp_path, capsys) -> None:  # noqa: ANN001
    mod = _script()
    user_id = await _demo_user(state)

    assert await mod.run(_args(_config(tmp_path, group={})), state) == 1

    assert set(await _snapshot(state, user_id)) == {"박지현", "김준호"}  # 맞는 파일은 들어간다
    printed = capsys.readouterr().out
    assert "화자가 맞지 않습니다" in printed
    assert "김민수, 이서연, 최동훈" in printed  # 파일 속 이름 목록

    # skip에 넣으면 그 화자를 빼고 들어간다
    assert await mod.run(_args(_config(tmp_path, group={"skip": ["최동훈"]})), state) == 0
    snap = await _snapshot(state, user_id)
    assert snap["이서연"][:7] == ("colleague", None, 1, "k3", ME, 2, 1)


async def test_nothing_is_deleted_when_no_file_is_loadable(state, tmp_path) -> None:  # noqa: ANN001
    mod = _script()
    user_id = await _demo_user(state)
    old = await state.contacts.create(user_id, "예전 상대", "friend")
    config = tmp_path / "demo.json"
    entry = {"file": "없음.txt", "contact": "박지현", "relationship_type": "superior", "source_key": "k1"}
    config.write_text(json.dumps({"me": ME, "conversations": [entry]}, ensure_ascii=False), "utf-8")

    assert await mod.run(_args(config), state) == 1
    assert [c.id for c in await state.contacts.list(user_id)] == [old.id]


async def test_running_twice_gives_the_same_result(state, tmp_path) -> None:  # noqa: ANN001
    mod = _script()
    user_id = await _demo_user(state)
    config = _config(tmp_path)

    assert await mod.run(_args(config), state) == 0
    first = await _snapshot(state, user_id)
    first_ids = {c.id for c in await state.contacts.list(user_id)}
    assert await mod.run(_args(config), state) == 0

    assert await _snapshot(state, user_id) == first
    assert not first_ids & {c.id for c in await state.contacts.list(user_id)}  # 지우고 다시 만들었다

    # --keep-existing은 지우지 않고, 같은 파일은 중복으로 걸러진다
    kept = {c.id for c in await state.contacts.list(user_id)}
    assert await mod.run(_args(config, keep_existing=True), state) == 0
    assert {c.id for c in await state.contacts.list(user_id)} == kept
    assert await _snapshot(state, user_id) == first


async def test_graph_ingest_and_missing_contact_node_warning(state, tmp_path, capsys) -> None:  # noqa: ANN001
    mod = _script()
    user_id = await _demo_user(state)
    store = FakeGraphStore()
    store.entities["박지현"] = "uuid-boss"  # 김준호 노드는 추출 모델이 다른 이름으로 만들었다고 치자
    ingest = GraphIngestService(store, state.conversations, state.contacts)
    state.graph_store, state.graph_ingest = store, ingest
    state.import_service._graph_ingest = ingest

    assert await mod.run(_args(_config(tmp_path), graph=True), state) == 0

    assert len(store.added) == 7 and {a["group_id"] for a in store.added} == {user_id}
    printed = capsys.readouterr().out
    assert "그래프에 '김준호' 노드가 없습니다" in printed
    assert "그래프에 '박지현' 노드가 없습니다" not in printed
    for conv in await state.conversations.list(user_id):
        s = await state.conversations.conversation_stats(user_id, conv.id)
        assert s.graph_ingested == s.messages


async def test_config_and_user_errors_change_nothing(state, tmp_path, capsys) -> None:  # noqa: ANN001
    mod = _script()
    config = _config(tmp_path)
    assert await mod.run(_args(config, user="없는사람"), state) == 2  # 이름을 id로 쓰지 않는다
    assert await mod.run(_args(config, graph=True), state) == 2  # 그래프가 꺼진 설정

    bad = tmp_path / "bad.json"
    entry = {"file": "boss.txt", "contact": "박지현", "relationship_type": "boss", "source_key": "k1"}
    bad.write_text(json.dumps({"me": ME, "conversations": [entry]}, ensure_ascii=False), "utf-8")
    assert await mod.run(_args(bad), state) == 2
    assert "relationship_type" in capsys.readouterr().out


async def test_without_yes_it_asks_once_and_only_y_proceeds(state, tmp_path, monkeypatch, capsys) -> None:  # noqa: ANN001
    mod = _script()
    user_id = await _demo_user(state)
    old = await state.contacts.create(user_id, "예전 상대", "friend")
    config = _config(tmp_path)
    asked: list[str] = []

    def answer_with(reply: str):  # noqa: ANN202
        def fake_input(prompt: str = "") -> str:
            asked.append(prompt)
            return reply

        return fake_input

    # 엔터(빈 답)·n은 취소 — 아무것도 지우지 않는다
    for reply in ("", "n"):
        monkeypatch.setattr("builtins.input", answer_with(reply))
        assert await mod.run(_args(config, yes=False), state) == 1
        assert [c.id for c in await state.contacts.list(user_id)] == [old.id]
    assert len(asked) == 2 and "되돌릴 수 없습니다" in asked[0]
    printed = capsys.readouterr().out
    assert "취소." in printed and "지울 대상" in printed and "상대 1명" in printed

    # y(대문자·공백 포함)는 진행
    monkeypatch.setattr("builtins.input", answer_with(" Y "))
    assert await mod.run(_args(config, yes=False), state) == 0
    assert set(await _snapshot(state, user_id)) == {"박지현", "김준호"}
    assert len(asked) == 3  # 한 번만 묻는다

    # --keep-existing은 지우지 않으므로 묻지 않는다
    assert await mod.run(_args(config, yes=False, keep_existing=True), state) == 0
    assert len(asked) == 3


@pytest.mark.parametrize("encoding", ["cp949", "utf-16", "utf-8-sig"])
async def test_reads_files_in_other_encodings(state, tmp_path, encoding) -> None:  # noqa: ANN001
    mod = _script()
    user_id = await _demo_user(state)
    config = _config(tmp_path)
    (tmp_path / "boss.txt").write_bytes(BOSS.encode(encoding))
    assert mod.read_text(tmp_path / "boss.txt").lstrip("\ufeff") == BOSS

    assert await mod.run(_args(config), state) == 0
    snap = await _snapshot(state, user_id)
    assert snap["박지현"][:7] == ("superior", "팀장님", 1, "k1", ME, 4, 2)
