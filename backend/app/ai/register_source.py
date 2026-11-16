"""말투 프로필의 입력 모으기 — 내가 한 상대에게 보낸 메시지 전부.

`ai.register.build_profile`은 순수 함수라 메시지를 받기만 한다. 그 메시지를 저장소에서 읽는 일은 제안
(`services/suggestion_service.py`), 첨삭의 상태 블록·경고(`services/rewrite_service.py`), 문체 예시
걸러내기(`ai/retrieval/vector.py`) 세 곳이 똑같이 한다 — 한 곳에 두어 메시지 id 모양이 어긋나지 않게 한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from app.domain.conversation import Conversation
from app.repositories.base import ConversationRepo

# (메시지 id, 보낸 시각, 본문)
OwnMessage = tuple[str, datetime, str]

# 프로필 계산에 넘기는 내 메시지 수의 상한 (최근 것부터). `style_suggest.MAX_MESSAGES`와 같은 값이다.
# 세 호출 쪽이 같은 상한을 써야 같은 상대에 대해 같은 구간이 나온다. 상한이 없으면 가져온 대화가 수만 건인
# 상대에서 첨삭 요청마다 정규식이 몇 초씩 돈다.
MAX_PROFILE_MESSAGES = 500


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def recent_before(
    messages: Sequence[OwnMessage], as_of: datetime | None, limit: int = MAX_PROFILE_MESSAGES
) -> list[OwnMessage]:
    """`as_of` **이전**의 메시지를 시간 순으로, 최근 `limit`건만. 프로필 계산에 넘기기 전에 거른다.

    시간대가 없는 시각은 UTC로 본다 (`ai.register`와 같다).
    """
    cutoff = _aware(as_of) if as_of is not None else None
    kept = [m for m in messages if cutoff is None or _aware(m[1]) < cutoff]
    kept.sort(key=lambda m: (_aware(m[1]), m[0]))
    return kept[-limit:] if limit > 0 else kept


async def own_messages(
    conversations: ConversationRepo,
    user_id: str,
    contact_id: UUID,
    *,
    convs: Sequence[Conversation] | None = None,
) -> list[OwnMessage]:
    """이 상대의 모든 대화방에서 내가(`speaker_role == "me"`) 보낸 메시지. 순서는 정해져 있지 않다.

    메시지 id는 그래프 에피소드 이름·문체 묶음 출처와 같은 `<대화방 source_key>:<메시지 source_key>`
    (`c3:m08`)다. `convs`는 대화방 목록을 이미 읽은 호출 쪽이 넘긴다(다시 읽지 않게).
    """
    if convs is None:
        convs = await conversations.list(user_id, contact_id)
    out: list[OwnMessage] = []
    for conv in convs:
        out.extend(
            (f"{conv.source_key}:{m.source_key}", m.sent_at, m.text)
            for m in await conversations.list_messages(user_id, conv.id)
            if m.speaker_role == "me"
        )
    return out
