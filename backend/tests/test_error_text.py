"""오류 기록에 사용자의 글이 남지 않는다 (2026-11-11 보안 검토 D10).

DB·그래프·모델 클라이언트의 예외 문구에는 SQL 매개변수나 에피소드 본문 — 대화 원문 — 이 들어 있을 수
있다. 작업 기록(`import_jobs.error_message`·`graph_error`), 삭제 응답, 로그 줄에는 예외의 종류만 남긴다.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.ai.embeddings import FakeEmbedder
from app.core.errors import NotFoundError
from app.domain.conversation import exception_kind
from app.graph.ingest import GraphIngestService
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService
from tests.test_graph import FakeGraphStore

SECRET = "ZQSECRET-금요일은-어렵습니다"  # 예외 문구에 섞여 나오는 대화 원문 흉내
MESSAGES = [
    ImportMessage(id="m01", speaker="contact", sent_at="2026-10-05T09:25:00+09:00", text="언제까지 돼요?"),
    ImportMessage(id="m02", speaker="me", sent_at="2026-10-05T09:31:00+09:00", text=SECRET),
]


def test_exception_kind_keeps_the_class_and_well_formed_codes_only() -> None:
    assert exception_kind(RuntimeError(SECRET)) == "RuntimeError"
    assert exception_kind(NotFoundError(SECRET)) == "NotFoundError(404)"

    class ApiError(Exception):
        status_code = 529

    class GraphError(Exception):
        code = "Neo.ClientError.Statement.SyntaxError"

    class WrappedError(Exception):  # SQLAlchemy가 드라이버 예외를 감싼 모양
        def __init__(self, orig: Exception) -> None:
            super().__init__(f"INSERT ... [parameters: ('{SECRET}',)]")
            self.orig = orig

    class DriverError(Exception):
        sqlstate = "23505"

    class ChattyError(Exception):
        code = f"자유로운 글 {SECRET}"  # 코드 자리에 글이 들어 있으면 쓰지 않는다

    assert exception_kind(ApiError(SECRET)) == "ApiError(529)"
    assert exception_kind(GraphError(SECRET)) == "GraphError(Neo.ClientError.Statement.SyntaxError)"
    assert exception_kind(WrappedError(DriverError(SECRET))) == "WrappedError(23505)"
    assert exception_kind(ChattyError(SECRET)) == "ChattyError"


class LeakyStore(FakeGraphStore):
    """실패할 때 에피소드 본문을 예외 문구에 실어 보낸다 (그래프·모델 라이브러리가 그러듯)."""

    async def add_episode(self, *, name, body, reference_time, group_id, source_description) -> None:  # noqa: ANN001
        raise RuntimeError(f"extraction failed for episode body: {body}")

    async def remove_episodes(self, group_id, names) -> int:  # noqa: ANN001
        raise ConnectionError(f"bolt://neo4j:7687 unreachable while deleting {names} {SECRET}")


@pytest.mark.anyio
async def test_graph_failures_record_only_the_exception_kind(caplog: pytest.LogCaptureFixture) -> None:
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    store = LeakyStore()
    ingest = GraphIngestService(store, conversations, contacts)
    svc = ImportService(contacts, conversations, FakeEmbedder(dim=32), graph_ingest=ingest, graph_mode="sync")
    boss = await contacts.create("u", "박지현", "superior")
    conv = await svc.create_conversation("u", boss.id, None, "json", "c1", me_name="김민수")

    with caplog.at_level(logging.DEBUG):
        job = await svc.import_messages("u", conv.id, ImportPayload(messages=MESSAGES[1:]))
        assert (job.graph_status, job.graph_error) == ("failed", "RuntimeError")
        await conversations.mark_graph_ingested(
            "u", [m.id for m in await conversations.list_messages("u", conv.id)], job.created_at
        )
        deleted = await svc.delete_conversation("u", conv.id)  # 그래프 삭제가 실패해도 DB는 지운다
        assert (deleted.messages, deleted.graph_error) == (1, "ConnectionError")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "graph ingest failed" in logged and "graph removal failed" in logged
    assert SECRET not in logged and "ZQSECRET" not in logged


def test_failed_reindex_job_does_not_keep_the_exception_text(
    client: TestClient, user_a: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    state = client.app.state.container
    cid = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=user_a
    ).json()["id"]
    conv_id = client.post("/v1/conversations", json={"contact_id": cid}, headers=user_a).json()["id"]

    async def leaky(texts):  # noqa: ANN001, ANN202
        raise RuntimeError(f"could not embed: {texts}")  # 묶음 본문 = 대화 원문

    state.embedder.embed_passages = leaky
    text = f"2026-10-05 09:31 | me | {SECRET}\n"
    with caplog.at_level(logging.DEBUG):
        r = client.post(f"/v1/conversations/{conv_id}/imports/raw", json={"text": text}, headers=user_a)
    assert r.status_code == 202, r.text
    job = r.json()
    assert (job["status"], job["error_code"], job["error_message"]) == (
        "failed",
        "reindex_failed",
        "RuntimeError",
    )
    assert "ZQSECRET" not in r.text
    assert "ZQSECRET" not in "\n".join(rec.getMessage() for rec in caplog.records)


@pytest.mark.anyio
async def test_account_deletion_reports_graph_failure_by_kind() -> None:
    from app.core.config import Settings
    from app.main import create_app

    settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]
    state = create_app(settings).state.container

    class DownStore(FakeGraphStore):
        async def remove_group(self, group_id) -> int:  # noqa: ANN001
            raise ConnectionRefusedError(f"bolt://neo4j:7687 {SECRET}")

    state.deletion_service._graph = DownStore()
    registered = await state.auth_service.register("leak-test-user", "correct-horse-battery")
    session = await state.auth_service.resolve(registered.session_token)
    assert session is not None
    result = await state.deletion_service.delete_account(session, "correct-horse-battery")
    assert result.graph_error == "ConnectionRefusedError"
