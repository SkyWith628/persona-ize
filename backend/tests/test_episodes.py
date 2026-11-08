"""메시지 → 묶음 규칙과 가져오기 입력 정규화(화자 지정·시간대·중복)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.ai.embeddings import FakeEmbedder, cosine
from app.ai.episodes import build_episodes
from app.core.errors import InvalidInputError
from app.domain.conversation import Message
from app.services.import_service import ImportMessage, ImportPayload, normalize_messages, source_hash

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
CONV = uuid4()


def _msg(i: int, role: str, text: str) -> Message:
    return Message(
        id=uuid4(),
        user_id="u",
        conversation_id=CONV,
        source_key=f"m{i:02d}",
        speaker_role=role,  # type: ignore[arg-type]
        sent_at=T0 + timedelta(minutes=i),
        text=text,
    )


def test_build_episodes_groups_me_runs_with_preceding_context() -> None:
    msgs = [
        _msg(1, "contact", "언제까지 가능해요?"),
        _msg(2, "me", "금요일은 어렵고"),
        _msg(3, "me", "다음 주 수요일까지는 가능합니다."),
        _msg(4, "contact", "목요일은요?"),
        _msg(5, "contact", "고객 미팅이 있어서요."),
        _msg(6, "me", "목요일이면 테스트를 못 돌립니다."),
    ]
    eps = build_episodes(msgs, "c1", window=4)
    assert len(eps) == 2
    first, second = eps
    # 연속 본인 발화는 한 묶음, 문체 예시에는 본인 발화만
    assert first.style_text == "금요일은 어렵고\n다음 주 수요일까지는 가능합니다."
    assert first.context_text.splitlines() == [
        "상대: 언제까지 가능해요?",
        "나: 금요일은 어렵고",
        "나: 다음 주 수요일까지는 가능합니다.",
    ]
    assert first.source_message_ids == ["c1:m01", "c1:m02", "c1:m03"]
    assert first.end_at == msgs[2].sent_at
    # 두 번째: 앞 맥락은 window-1=3개까지 → m03(나), m04, m05
    assert second.source_message_ids == ["c1:m03", "c1:m04", "c1:m05", "c1:m06"]
    assert second.style_text == "목요일이면 테스트를 못 돌립니다."


def test_build_episodes_without_me_messages_is_empty_and_sorts_by_time() -> None:
    assert build_episodes([_msg(1, "contact", "안녕"), _msg(2, "contact", "뭐해")], "c9") == []
    shuffled = [_msg(3, "me", "셋"), _msg(1, "contact", "하나"), _msg(2, "me", "둘")]
    eps = build_episodes(shuffled, "c1", window=3)
    assert [e.source_message_ids for e in eps] == [
        ["c1:m01", "c1:m02", "c1:m03"]
    ]  # 연속 me(m02,m03) + 앞 맥락 1개 (window 3 - 2)


def test_normalize_messages_speaker_map_and_errors() -> None:
    payload = ImportPayload(
        speaker_map={"김민수": "me", "박지현": "contact"},
        messages=[
            ImportMessage(id="m01", speaker="박지현", sent_at="2026-10-05T09:25:00+09:00", text="언제까지?"),
            ImportMessage(
                id="m02", speaker="김민수", sent_at="2026-10-05T09:31:00+09:00", text=" 수요일까지요 "
            ),
            ImportMessage(id="m03", speaker="me", sent_at="2026-10-05T09:40:00+09:00", text="네"),
        ],
    )
    out = normalize_messages(payload)
    assert [(m.source_key, m.speaker_role, m.text) for m in out] == [
        ("m01", "contact", "언제까지?"),
        ("m02", "me", "수요일까지요"),
        ("m03", "me", "네"),
    ]
    # 같은 내용은 순서가 달라도 같은 해시
    assert source_hash(out) == source_hash(list(reversed(out)))

    with pytest.raises(InvalidInputError, match="화자 지정"):
        normalize_messages(
            ImportPayload(messages=[ImportMessage(id="m01", speaker="홍길동", sent_at=T0, text="안녕")])
        )
    with pytest.raises(InvalidInputError, match="시간대"):
        normalize_messages(
            ImportPayload(
                messages=[ImportMessage(id="m01", speaker="me", sent_at="2026-10-05T09:00:00", text="안녕")]
            )
        )
    with pytest.raises(InvalidInputError, match="중복"):
        normalize_messages(
            ImportPayload(
                messages=[
                    ImportMessage(id="m01", speaker="me", sent_at=T0, text="a"),
                    ImportMessage(id="m01", speaker="me", sent_at=T0, text="b"),
                ]
            )
        )


@pytest.mark.anyio
async def test_fake_embedder_is_deterministic_and_similarity_ordered() -> None:
    emb = FakeEmbedder(dim=64)
    a, b = await emb.embed_passages(["팀장님, 내일 오전까지 보내드리겠습니다.", "야 ㅋㅋ 국밥 먹을래?"])
    q = await emb.embed_query("팀장님, 내일 오전까지 전달드리겠습니다.")
    assert a == (await emb.embed_passages(["팀장님, 내일 오전까지 보내드리겠습니다."]))[0]
    assert len(q) == 64
    assert cosine(q, a) > cosine(q, b)
