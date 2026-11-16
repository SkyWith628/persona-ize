"""운영 대시보드: 집계 계산과 권한."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.admin import RewriteStat
from app.main import create_app
from app.services.admin_service import mask_user, percentile, summarize

NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)


def _stat(user: str, status: str, latency: int, **kw) -> RewriteStat:  # noqa: ANN003
    base = {"condition": "D", "created_at": NOW, "input_tokens": 100, "output_tokens": 20}
    return RewriteStat(user_id=user, status=status, latency_ms=latency, **{**base, **kw})


def test_summarize_counts_quality_latency_feedback_and_reasons() -> None:
    stats = [
        _stat("a", "recommended", 1000, feedback_accepted=True),
        _stat("a", "recommended", 3000, condition="B"),
        _stat(
            "b",
            "fallback_original",
            9000,
            violation_checks=("preserve_missing", "preserve_missing", "fact_added"),
        ),
        _stat("b", "fallback_original", 2000, created_at=NOW + timedelta(minutes=1), feedback_accepted=False),
        _stat("c", "recommended", 2500, feedback_accepted=False, feedback_has_reason=True),
    ]
    out = summarize(stats)

    assert (out["requests"], out["active_users"], out["recommended"], out["kept"]) == (5, 3, 3, 2)
    assert out["success_rate"] == 0.6
    assert (out["latency_p50_ms"], out["latency_p95_ms"]) == (2500, 9000)
    assert (out["feedback_total"], out["feedback_accepted"], out["accept_rate"]) == (3, 1, 0.3333)
    assert (out["input_tokens"], out["output_tokens"]) == (500, 100)
    # 한 건에서 같은 사유가 두 번 나와도 한 번만 세고, 사유가 없는 원문 유지는 unknown.
    assert {r.check: r.count for r in out["fallback_reasons"]} == {
        "preserve_missing": 1,
        "fact_added": 1,
        "unknown": 1,
    }
    assert [(c.condition, c.requests, c.kept) for c in out["by_condition"]] == [("B", 1, 0), ("D", 4, 2)]
    # 거절은 최근 순이고 사용자 id는 가려진다.
    assert [(r.user, r.has_reason) for r in out["recent_rejections"]] == [
        (mask_user("b"), False),
        (mask_user("c"), True),
    ]
    assert "b" != out["recent_rejections"][0].user and out["recent_rejections"][0].user.startswith("u_")


def test_summarize_empty_has_no_rates() -> None:
    out = summarize([])
    assert out["requests"] == 0 and out["success_rate"] is None and out["accept_rate"] is None
    assert out["latency_p50_ms"] is None and out["fallback_reasons"] == []
    assert percentile([], 0.95) is None and percentile([7], 0.95) == 7


def _client(admins: str) -> TestClient:
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        embedding_provider="fake",
        admin_usernames=admins,
        auth_signup_requires_approval=False,
    )  # type: ignore[call-arg]
    return TestClient(create_app(settings))


def _register(client: TestClient, name: str) -> dict[str, str]:
    r = client.post("/v1/auth/register", json={"username": name, "password": "test-pass-1234"})
    assert r.status_code == 201
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def _admin(client: TestClient, name: str) -> dict[str, str]:
    """`ADMIN_USERNAMES`의 이름은 공개 가입으로 못 만든다 → 운영자 도구처럼 서비스로 만들고 로그인한다."""
    service = client.app.state.container.auth_service  # type: ignore[attr-defined]
    asyncio.run(service.create_account(name, "test-pass-1234"))
    r = client.post("/v1/auth/login", json={"username": name, "password": "test-pass-1234"})
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def test_overview_requires_login_and_admin_name() -> None:
    client = _client("boss")
    assert client.get("/v1/admin/overview").status_code == 401
    # 헤더 스텁으로는 열리지 않는다.
    assert client.get("/v1/admin/overview", headers={"X-User-Id": "boss"}).status_code == 401
    _register(client, "member")
    assert client.get("/v1/admin/overview").status_code == 403
    assert _client("").get("/v1/admin/overview").status_code == 401


def test_overview_reports_rewrites_feedback_and_backlog_without_text() -> None:
    client = _client("boss, other")
    # 관리자 이름은 공개 가입으로 선점할 수 없다 (이미 있는 이름과 같은 409).
    assert (
        client.post("/v1/auth/register", json={"username": "boss", "password": "test-pass-1234"}).status_code
        == 409
    )
    csrf = _admin(client, "boss")
    contact = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=csrf
    ).json()
    draft = "내일 3시까지 초안 보내드릴게요."
    first = client.post(
        "/v1/rewrites",
        json={"contact_id": contact["id"], "draft_text": draft, "condition": "A"},
        headers=csrf,
    )
    assert first.status_code == 201
    client.post(
        "/v1/rewrites",
        json={"contact_id": contact["id"], "draft_text": draft + " 감사합니다.", "condition": "A"},
        headers=csrf,
    )
    fb = client.post(
        f"/v1/rewrites/{first.json()['id']}/feedback",
        json={"accepted": False, "reason": "너무 딱딱함"},
        headers=csrf,
    )
    assert fb.status_code == 204

    r = client.get("/v1/admin/overview?days=1")
    body = r.json()

    assert r.status_code == 200
    assert (body["requests"], body["active_users"], body["recommended"], body["kept"]) == (2, 1, 2, 0)
    assert (body["feedback_total"], body["feedback_accepted"]) == (1, 0)
    assert body["by_condition"] == [{"condition": "A", "requests": 2, "kept": 0}]
    assert body["recent_rejections"][0]["has_reason"] is True
    assert body["graph_enabled"] is False and body["daily_rewrite_limit"] == 200
    assert "딱딱함" not in r.text and draft not in r.text
    assert body["users"] == {"total": 1, "pending": 0, "suspended": 0, "new_in_range": 1}
    assert client.get("/v1/admin/overview?days=0").status_code == 422
