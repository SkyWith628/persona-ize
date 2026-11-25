"""시연 흐름 회귀 검사 (`scripts/demo_check.py`)를 서버 없이 돈다 — 메모리 저장소, 가짜 모델·임베더.

장면 함수는 스크립트에 있고 여기서는 전송 계층만 TestClient로 바꾼다. 실패하면 깨진 장면과 단계가 그대로
나온다. 보는 것은 경로뿐이다(가짜 모델은 초안을 그대로 돌려준다) — 한계는 스크립트 머리말에 있다.

    make demo-check-docker          # = make test-docker ARGS="tests/test_demo_flow.py -s"
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.providers.fake import FakeProvider
from app.ai.retrieval.composite import CompositeRetriever
from app.core.config import Settings
from app.graph.retriever import GraphContextRetriever
from app.graph.store import EpisodeRef, GraphEdgeHit
from app.importers import parse_import
from app.main import create_app
from tests.test_graph import FakeGraphStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_check.py"
# 헬스 1 + 장면 0의 4 + 준비 8 + 장면 1~3의 6 + 장면 4의 3 + 장면 5의 5 + 끝 2. 단계를 더하면 고친다
STEPS = 29


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("pz_demo_check_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass가 모듈을 찾는다
    spec.loader.exec_module(module)
    return module


demo = _load()


class _Response:
    def __init__(self, raw: Any) -> None:
        self.status = raw.status_code
        self.headers = raw.headers
        self._raw = raw

    def json(self) -> Any:
        try:
            return self._raw.json()
        except ValueError:
            return None


class _Client:
    """TestClient를 스크립트의 HTTP 클라이언트 꼴로. 쿠키는 TestClient가 들고, CSRF 토큰은 여기서 붙인다."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.csrf: str | None = None
        self.calls: list[tuple[str, str]] = []

    def request(
        self, method: str, path: str, body: Any = None, *, csrf: bool = True, headers: dict | None = None
    ) -> _Response:
        send = dict(headers or {})
        if csrf and self.csrf and method not in ("GET", "HEAD"):
            send["X-CSRF-Token"] = self.csrf
        self.calls.append((method, path))
        return _Response(self.client.request(method, path, json=body, headers=send))


def _settings(**overrides: Any) -> Settings:
    # 헤더 스텁을 끈다 — 시연처럼 세션 쿠키와 CSRF 토큰으로만 통과해야 한다
    values: dict[str, Any] = {"llm_provider": "fake", "embedding_provider": "fake"}
    values.update(auth_allow_header_stub=False, log_level="WARNING", **overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _run(app_client: TestClient, **kwargs: Any) -> tuple[Any, _Client, list[str]]:
    lines: list[str] = []

    def out(line: str) -> None:
        lines.append(line)
        print(line)

    client = _Client(app_client)
    check = demo.DemoCheck(client, poll_seconds=0.01, index_wait=20.0, out=out, **kwargs)
    return check, client, lines


def test_demo_flow_runs_every_scene_with_the_graph_off() -> None:
    with TestClient(create_app(_settings())) as app_client:
        check, client, lines = _run(app_client)
        report = check.run()

    assert report.failed == [] and report.skipped == [], "\n".join(report.failed + report.skipped)
    assert report.passed == STEPS
    assert lines[-2].startswith("시연 흐름 통과") and lines[-1] == demo.LIMITS  # 한계를 늘 말한다
    # 그래프가 꺼진 서버: D는 C와 같은 검색, 자동 조건은 C
    rewrites = check.rewrites
    assert sorted(rewrites) == [
        "boss:B",
        "boss:C",
        "colleague:C",
        "colleague:D",
        "english:D",
        "english:auto",
    ]
    assert rewrites["english:auto"]["condition"] == "C"
    c, d = rewrites["colleague:C"]["result"], rewrites["colleague:D"]["result"]
    assert c["retrieval_version"] == d["retrieval_version"] and d["evidence_ids"]
    assert all(r["result"]["model_version"] == "fake-echo" for r in rewrites.values())
    # 임시 계정은 남지 않는다
    assert check.deleted and ("DELETE", "/v1/auth/me") in client.calls


def test_demo_flow_with_invite_code_and_consent_required() -> None:
    """운영과 같은 가입 설정(초대 코드·동의)에서도 장면 0이 지나간다. 코드가 없으면 장면 0에서 멈춘다."""
    settings = _settings(register_invite_code="demo-invite", require_consent=True)
    with TestClient(create_app(settings)) as app_client:
        check, _, _ = _run(app_client, invite_code="demo-invite")
        assert check.run().ok

        blocked, client, lines = _run(app_client)
        report = blocked.run()
    assert report.failed == ["장면 0 · 가입 (초대 코드·동의) — 초대 코드가 필요한 서버입니다 (--invite-code)"]
    assert "깨진 장면: 0(가입·로그인)" in lines
    assert not blocked.registered and ("DELETE", "/v1/auth/me") not in client.calls


class _RecordingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def generate(self, *, system: str, user: str) -> Any:
        self.prompts.append(user)
        return await super().generate(system=system, user=user)


FACT = "윤가람이 오과장에게 2차 생산 완료를 11월 14일 오전으로 안내했다"


def test_demo_flow_with_a_graph_adds_the_context_block_to_condition_d() -> None:
    """그래프가 켜진 구성(FakeGraphStore): D의 프롬프트에 사실 맥락이 붙는다."""
    at = datetime(2026, 11, 7, 6, 30, tzinfo=UTC)
    store = FakeGraphStore(
        entities={"오과장": "node-oh"},
        refs={"ep1": EpisodeRef("ep1", "k:m10", at)},
        edges=[
            GraphEdgeHit(
                uuid="edge-1",
                name="Involved",
                fact=FACT,
                valid_at=at,
                invalid_at=None,
                episode_uuids=["ep1"],
                source_node_uuid="node-me",
                target_node_uuid="node-oh",
            )
        ],
    )
    provider = _RecordingProvider()
    with TestClient(create_app(_settings())) as app_client:
        state = app_client.app.state.container  # type: ignore[attr-defined]
        # deps.build_state가 NEO4J_URI로 하는 배선을 가짜 저장소로 (수집은 붙이지 않는다 — 모델이 필요하다)
        state.graph_store = store
        state.rewrite_service.graph_enabled = True
        style = state.workflow.retrievers["C"]
        state.workflow.retrievers["D"] = CompositeRetriever(
            [style, GraphContextRetriever(store, state.contacts)]
        )
        state.workflow.provider = provider
        check, _, _ = _run(app_client)
        report = check.run()

    assert report.failed == [] and report.skipped == [], "\n".join(report.failed + report.skipped)
    c, d = check.rewrites["colleague:C"]["result"], check.rewrites["colleague:D"]["result"]
    assert d["retrieval_version"] == f"{c['retrieval_version']}+graph-context-v3"
    with_fact = [p for p in provider.prompts if FACT in p]
    assert len(with_fact) == 1 and "[사실 맥락" in with_fact[0]  # 동료에게 보낸 D 한 번에만
    assert store.searches and all(s[2] == "node-oh" for s in store.searches)


def test_a_broken_step_names_its_scene_and_does_not_print_the_response_body() -> None:
    """경로가 깨지면 장면·단계·응답 코드를 말하고, 그 뒤 장면은 이어서 본다. 본문(초안·대화)은 찍지 않는다."""
    with TestClient(create_app(_settings(max_draft_chars=30))) as app_client:  # 시연 초안이 한도를 넘는다
        check, client, lines = _run(app_client)
        report = check.run()

    assert not report.ok
    assert report.failed_scenes() == ["1", "2", "3"]
    assert report.failed[0] == "장면 1 · B 첨삭 — HTTP 422 code=invalid_input"
    # 첨삭이 없어도 장면 4(제안)와 내 데이터는 본다. 이력·피드백은 할 것이 없어 건너뛴다
    assert any(line.startswith("[ OK ] 장면 4 · 제안 카드") for line in lines)
    assert [s.split(" — ")[0] for s in report.skipped] == ["장면 5 · 피드백 저장", "장면 5 · 이력"]
    printed = "\n".join(lines)
    for draft, _situation in demo.DRAFTS.values():
        assert draft not in printed
    assert check.deleted and ("DELETE", "/v1/auth/me") in client.calls  # 실패해도 계정은 지운다


def test_real_model_servers_are_refused_unless_asked() -> None:
    class Health:
        status = 200
        headers: dict[str, str] = {}

        def __init__(self, body: dict[str, Any]) -> None:
            self._body = body

        def json(self) -> dict[str, Any]:
            return self._body

    class Server:
        csrf = None

        def __init__(self, body: dict[str, Any]) -> None:
            self.body, self.calls = body, 0

        def request(self, method: str, path: str, body: Any = None, **kw: Any) -> Health:
            self.calls += 1
            return Health(self.body)

    for body in ({"status": "ok", "llm_provider": "anthropic"}, {"status": "ok"}):  # 개발 / 운영 모드
        server = Server(body)
        report = demo.DemoCheck(server, out=lambda _line: None).run()
        assert len(report.failed) == 1 and "--assume-fake" in report.failed[0]
        assert server.calls == 1  # 헬스체크에서 멈춘다 — 가입도 가져오기도 하지 않는다


@pytest.mark.parametrize("plan", demo.PLANS, ids=lambda p: p.key)
def test_synthetic_conversations_are_read_as_kakao_csv(plan: Any) -> None:
    """합성 대화가 카카오톡 PC .csv 꼴로 읽히고, 초대·삭제·사진 행은 메시지가 되지 않는다."""
    parsed = parse_import(demo.kakao_csv(plan), "auto")
    assert (parsed.format, parsed.variant) == ("kakao", "csv")
    assert dict(parsed.speakers()) == demo.expected_counts(plan)
    assert parsed.problems == [] and parsed.skipped_lines == 0
    assert parsed.system_lines == sum(1 for _at, who, text in plan.turns if who == "system" or text == "사진")


def test_plans_match_the_demo_script_cast() -> None:
    assert [(p.key, p.relationship_type) for p in demo.PLANS] == [
        ("boss", "superior"),
        ("colleague", "colleague"),
        ("english", "acquaintance"),
        ("changed", "acquaintance"),
    ]
    # 동료 대화는 단체방 꼴이다: 나·상대 말고 한 사람을 뺀다
    assert sorted(demo.speaker_map(demo.PLANS[1]).values()) == ["contact", "me", "skip"]
