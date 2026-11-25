"""말투 프로필 플래그가 `build_state`에서 각 부품까지 닿는지 (docs/design/graph-redesign-2026-11-16.md 5절).

기본값에서는 예전 구성 그대로여야 하고, 켜면 해당 부품만 바뀌어야 한다. 부품 각각의 동작은
test_register_*.py · test_style_gating.py · test_graph_invalidation.py가 본다.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.ai.register_source import own_messages
from app.api.deps import build_state
from app.core.config import Settings
from app.repositories.memory import InMemoryConversationRepo


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)


def test_defaults_keep_every_register_feature_off_except_suggestions() -> None:
    settings = _settings()
    assert settings.register_suggestions is True
    state = build_state(settings)
    assert state.workflow.register_warnings is False
    assert state.workflow.register_post_check is False
    assert state.rewrite_service._register_state_block is False
    assert state.rewrite_service._register_warnings is False
    vector = state.workflow.retrievers["C"]
    assert vector._gate is False  # type: ignore[attr-defined]
    assert vector.version == "knn-style-v1/" + state.embedder.version  # 버전 문자열도 예전 그대로


def test_flags_reach_workflow_service_and_retriever() -> None:
    state = build_state(
        _settings(
            register_warnings=True,
            register_post_check=True,
            register_state_block=True,
            style_example_gating=True,
        )
    )
    assert state.workflow.register_warnings is True
    assert state.workflow.register_post_check is True
    assert state.rewrite_service._register_state_block is True
    assert state.rewrite_service._register_warnings is True
    vector = state.workflow.retrievers["C"]
    assert vector._gate is True  # type: ignore[attr-defined]
    assert vector.version.endswith("+gate")
    assert state.workflow.retrievers["D"] is vector  # 그래프가 없으면 D = C (같은 걸러내기)


def test_each_flag_is_independent() -> None:
    state = build_state(_settings(register_post_check=True))
    assert state.workflow.register_post_check is True
    assert state.workflow.register_warnings is False
    assert state.rewrite_service._register_warnings is False
    assert state.workflow.retrievers["C"]._gate is False  # type: ignore[attr-defined]


def test_invalidation_mode_reaches_graph_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neo4j 없이 확인한다 — 저장소 생성만 가짜로 바꾸고 `_build_graph`의 나머지는 그대로 돈다."""
    from app.api import deps

    # graphiti_core는 import 시점에 load_dotenv()로 .env 전체를 os.environ에 올린다. 그대로 두면 뒤에 도는
    # 테스트의 Settings(_env_file=None)이 DATABASE_URL·NEO4J_URI·ANTHROPIC_API_KEY를 환경변수에서 읽어
    # 개발 DB와 실제 키로 붙는다. `deps._build_graph`와 같은 방식으로 새로 생긴 변수를 지운다.
    before = set(os.environ)
    try:
        pytest.importorskip("graphiti_core")
        import app.graph.client as client
        import app.graph.store as store
    finally:
        for key in set(os.environ) - before:
            del os.environ[key]

    class _Store:
        def __init__(self, graphiti: object) -> None:
            self.graphiti = graphiti

    monkeypatch.setattr(client, "build_graphiti", lambda settings, embedder: object())
    monkeypatch.setattr(store, "GraphitiStore", _Store)
    for mode, version in (("trust", "graph-context-v3"), ("artifacts", "graph-context-v3+inv-artifacts")):
        settings = _settings(neo4j_uri="bolt://unused:7687", graph_invalidation_mode=mode)
        state = build_state(settings)
        _, _, retriever = deps._build_graph(settings, state.embedder, state.contacts, state.conversations)
        assert retriever.version == version


def test_own_messages_keeps_only_mine_with_episode_style_ids() -> None:
    async def scenario() -> None:
        from app.domain.conversation import NewMessage

        repo = InMemoryConversationRepo()
        contact_id = uuid4()
        conv = await repo.create("u1", contact_id, title="t", source="test", source_key="c9")
        at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        rows = [("m01", "me", "안녕"), ("m02", "contact", "응"), ("m03", "me", "내일 봐")]
        await repo.add_messages(
            "u1",
            conv.id,
            [NewMessage(source_key=k, speaker_role=r, sent_at=at, text=t) for k, r, t in rows],  # type: ignore[arg-type]
        )
        got = await own_messages(repo, "u1", contact_id)
        assert [(mid, text) for mid, _, text in got] == [("c9:m01", "안녕"), ("c9:m03", "내일 봐")]
        assert await own_messages(repo, "u1", uuid4()) == []

    asyncio.run(scenario())


def test_build_state_gives_the_gate_the_contact_alias() -> None:
    """걸러내기가 제안 카드·상태 블록과 같은 프로필을 계산하도록 `build_state`가 별칭 조회를 넘긴다."""

    async def scenario() -> None:
        state = build_state(_settings(style_example_gating=True))
        contact = await state.contacts.create("u1", "이서연", "colleague")
        vector = state.workflow.retrievers["C"]
        assert await vector._alias_of("u1", contact.id) == "이서연"  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_recent_before_filters_sorts_and_caps() -> None:
    from datetime import timedelta

    from app.ai.register_source import MAX_PROFILE_MESSAGES, recent_before
    from app.ai.style_suggest import MAX_MESSAGES

    assert MAX_PROFILE_MESSAGES == MAX_MESSAGES == 500  # 제안·상태 블록·걸러내기가 같은 창을 본다
    base = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    rows = [(f"c:m{i:03d}", base + timedelta(minutes=i), "넵") for i in range(600)]
    got = recent_before(list(reversed(rows)), None)
    assert len(got) == 500 and got[0][0] == "c:m100" and got[-1][0] == "c:m599"
    cut = recent_before(rows, base + timedelta(minutes=3))
    assert [mid for mid, _, _ in cut] == ["c:m000", "c:m001", "c:m002"]  # as_of 그 시각은 빠진다
    # 시간대 없는 시각이 섞여도 죽지 않는다
    assert (
        len(recent_before([("a:1", datetime(2026, 10, 1, 9, 0), "x"), rows[0]], base + timedelta(hours=1)))
        == 2
    )


def test_rewrite_service_profile_runs_off_the_event_loop_on_a_bounded_window() -> None:
    """상태 블록·경고용 프로필: 최근 500건만, 이벤트 루프 밖에서 계산한다 (요청마다 불린다)."""
    import threading
    from datetime import timedelta

    from app.ai import register
    from app.domain.conversation import NewMessage
    from app.domain.rewrite import RewriteInput

    async def scenario() -> None:
        state = build_state(_settings(register_state_block=True))
        contact = await state.contacts.create("u1", "이서연", "colleague")
        conv = await state.conversations.create("u1", contact.id, title="t", source="test", source_key="c9")
        base = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        await state.conversations.add_messages(
            "u1",
            conv.id,
            [
                NewMessage(
                    source_key=f"m{i:03d}",
                    speaker_role="me",
                    sent_at=base + timedelta(minutes=i),
                    text="확인했습니다.",
                )  # type: ignore[arg-type]
                for i in range(520)
            ],
        )
        loop_thread = threading.get_ident()
        seen: dict[str, object] = {}
        real = register.build_profile

        def spy(messages, alias, **kwargs):  # noqa: ANN001, ANN003, ANN202
            seen["thread"] = threading.get_ident()
            seen["count"] = len(messages)
            return real(messages, alias, **kwargs)

        register.build_profile = spy  # type: ignore[assignment]
        try:
            inp = RewriteInput(
                user_id="u1", contact_id=contact.id, draft_text="내일 드릴게요", draft_version=1
            )
            profile = await state.rewrite_service._register_profile(inp, contact)
        finally:
            register.build_profile = real  # type: ignore[assignment]
        assert profile is not None and profile.observations == 500
        assert seen["count"] == 500 and seen["thread"] != loop_thread

    asyncio.run(scenario())
