"""메시지 → 검색 단위(episode) 묶기 (project-design §5.1 "2~6개 턴의 맥락 단위").

규칙
- 본인(me) 발화가 연속으로 이어진 구간 하나가 묶음 하나의 중심이다. 상대 발화만 있는 구간은 묶음을
  만들지 않는다 (문체 예시는 본인 발화만 쓴다).
- 그 앞의 메시지를 최대 `window - 1`개까지 맥락으로 붙인다. 맥락은 임베딩과 출처 추적에만 쓰고 문체 예시
  (`style_text`)에는 들어가지 않는다 — 상대 말투가 섞이면 안 되기 때문이다.
- `context_text`는 "상대: …" / "나: …" 줄로 만든다. 질의(초안+상황)와 비슷한 장면을 kNN으로 찾기 위한 글이다.
"""

from __future__ import annotations

from app.domain.conversation import EpisodeDraft, Message

DEFAULT_WINDOW = 4
ROLE_LABEL = {"me": "나", "contact": "상대"}


def build_episodes(
    messages: list[Message], conversation_key: str, window: int = DEFAULT_WINDOW
) -> list[EpisodeDraft]:
    """시간 순으로 정렬한 메시지에서 묶음을 만든다. 같은 시각이면 입력 순서를 유지한다."""
    if window < 1:
        raise ValueError("window는 1 이상")
    ordered = sorted(messages, key=lambda m: m.sent_at)
    drafts: list[EpisodeDraft] = []
    i = 0
    while i < len(ordered):
        if ordered[i].speaker_role != "me":
            i += 1
            continue
        j = i
        while j < len(ordered) and ordered[j].speaker_role == "me":
            j += 1
        run = ordered[i:j]
        room = max(window - len(run), 0)
        preceding = ordered[max(0, i - room) : i]
        chunk = preceding + run
        drafts.append(
            EpisodeDraft(
                context_text="\n".join(f"{ROLE_LABEL[m.speaker_role]}: {m.text}" for m in chunk),
                style_text="\n".join(m.text for m in run),
                start_at=chunk[0].sent_at,
                end_at=run[-1].sent_at,
                message_ids=[m.id for m in chunk],
                source_message_ids=[f"{conversation_key}:{m.source_key}" for m in chunk],
            )
        )
        i = j
    return drafts
