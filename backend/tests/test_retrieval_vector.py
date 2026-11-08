"""VectorStyleRetriever: 같은 상대 범위, as_of 이전만, 유사도 순, 결과는 본인 발화만."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.ai.embeddings import FakeEmbedder
from app.ai.retrieval.vector import VectorStyleRetriever
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService

pytestmark = pytest.mark.anyio


async def _setup():  # noqa: ANN202
    contacts = InMemoryContactRepo()
    conversations = InMemoryConversationRepo()
    embedder = FakeEmbedder(dim=128)
    svc = ImportService(contacts, conversations, embedder)
    boss = await contacts.create("u", "박지현", "superior")
    bro = await contacts.create("u", "준호", "family")
    c1 = await svc.create_conversation("u", boss.id, None, "json", "c1")
    c2 = await svc.create_conversation("u", bro.id, None, "json", "c2")
    await svc.import_messages(
        "u",
        c1.id,
        ImportPayload(
            messages=[
                ImportMessage(
                    id="m01",
                    speaker="contact",
                    sent_at="2026-10-05T09:25:00+09:00",
                    text="언제까지 가능해요?",
                ),
                ImportMessage(
                    id="m02",
                    speaker="me",
                    sent_at="2026-10-05T09:31:00+09:00",
                    text="팀장님, 이번 주 금요일은 어렵고 다음 주 수요일까지는 가능합니다.",
                ),
                ImportMessage(
                    id="m03", speaker="contact", sent_at="2026-10-14T10:00:00+09:00", text="자료 받았어요."
                ),
                ImportMessage(
                    id="m04",
                    speaker="me",
                    sent_at="2026-10-14T10:05:00+09:00",
                    text="네 팀장님, 확인 감사합니다.",
                ),
            ]
        ),
    )
    await svc.import_messages(
        "u",
        c2.id,
        ImportPayload(
            messages=[
                ImportMessage(
                    id="m01", speaker="me", sent_at="2026-10-06T20:00:00+09:00", text="야 금요일에 국밥 ㄱ?"
                ),
            ]
        ),
    )
    return boss, bro, VectorStyleRetriever(conversations, embedder)


async def test_scope_is_same_contact_and_results_are_my_utterances() -> None:
    boss, bro, retriever = await _setup()
    got = await retriever.retrieve(
        user_id="u",
        contact_id=boss.id,
        draft_text="금요일은 어렵고 수요일까지는 가능합니다",
        situation="일정",
    )
    assert [e.purpose for e in got] == ["style", "style"]
    assert all(e.speaker_role == "me" for e in got)
    assert got[0].text.startswith("팀장님, 이번 주 금요일은 어렵고")  # 가장 비슷한 묶음이 먼저
    assert got[0].source_message_ids == ["c1:m01", "c1:m02"]  # 앞 맥락(상대 발화)도 출처에 포함
    assert "언제까지" not in got[0].text  # 상대 발화는 문체 예시 본문에 없다

    other = await retriever.retrieve(user_id="u", contact_id=bro.id, draft_text="국밥", situation="")
    assert [e.text for e in other] == ["야 금요일에 국밥 ㄱ?"]
    assert (
        await retriever.retrieve(user_id="someone-else", contact_id=boss.id, draft_text="국밥", situation="")
        == []
    )


async def test_as_of_excludes_later_messages_and_top_k() -> None:
    boss, _, retriever = await _setup()
    before = await retriever.retrieve(
        user_id="u",
        contact_id=boss.id,
        draft_text="확인 감사합니다",
        situation="",
        as_of=datetime(2026, 10, 10, tzinfo=UTC),
    )
    assert [e.source_message_ids for e in before] == [["c1:m01", "c1:m02"]]
    assert (
        len(
            await retriever.retrieve(
                user_id="u", contact_id=boss.id, draft_text="확인", situation="", top_k=1
            )
        )
        == 1
    )
    assert await retriever.retrieve(user_id="u", contact_id=boss.id, draft_text="  ", situation="") == []
