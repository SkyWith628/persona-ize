"""메시지 → Graphiti 에피소드 본문 (스파이크 `p0/episodes.py`의 규칙 그대로).

`[2026-10-05 월 09:31] 김민수 → 박지현: 다음 주 수요일(=2026-10-14)까지 가능합니다`
- 요일: 모델이 상대 날짜를 요일 없이 계산하면 틀린다 (스파이크 3차).
- 수신자: 짧은 답장에서 자기참조 엣지가 생기는 것을 막는다 (3차).
- `(=YYYY-MM-DD)`: `dates.py`가 계산한 절대 날짜. 담화 문맥(앞 메시지가 특정한 요일 날짜)은 대화방
  안에서만 이어진다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.conversation import Message, local_time
from app.graph.dates import DiscourseContext, annotate_relative_dates

WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")
DEFAULT_ME_NAME = "사용자"


@dataclass(frozen=True)
class GraphEpisode:
    message_id: UUID
    name: str  # `c1:m03`
    body: str
    reference_time: datetime


def format_body(
    sent_at: datetime, speaker: str, listener: str, text: str, context: DiscourseContext | None = None
) -> str:
    # 날짜·요일·시각은 한국 시간으로 읽는다. PostgreSQL은 시각을 UTC로 돌려주므로 그대로 쓰면 화요일 08:01의
    # "내일"이 `[월 23:01] … 내일(=화요일)`로 들어간다 (2026-11-11 정확성 검토).
    local = local_time(sent_at)
    stamp = f"{local:%Y-%m-%d} {WEEKDAYS_KO[local.weekday()]} {local:%H:%M}"
    return f"[{stamp}] {speaker} → {listener}: {annotate_relative_dates(text, local, context)}"


def build_graph_episodes(
    messages: list[Message], conversation_key: str, me_name: str | None, contact_name: str
) -> list[GraphEpisode]:
    """대화방 하나의 메시지 전부를 시간 순 에피소드로.

    담화 문맥 때문에 일부만 넘기면 날짜가 달라질 수 있다.
    """
    me = (me_name or DEFAULT_ME_NAME).strip() or DEFAULT_ME_NAME
    context = DiscourseContext()
    out: list[GraphEpisode] = []
    for m in sorted(messages, key=lambda x: x.sent_at):
        speaker, listener = (me, contact_name) if m.speaker_role == "me" else (contact_name, me)
        out.append(
            GraphEpisode(
                message_id=m.id,
                name=f"{conversation_key}:{m.source_key}",
                body=format_body(m.sent_at, speaker, listener, m.text, context),
                reference_time=m.sent_at,
            )
        )
    return out
