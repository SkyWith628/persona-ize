"""메시지 → 검색 단위(episode) 묶기 (project-design §5.1 "2~6개 턴의 맥락 단위").

규칙
- 본인(me) 발화가 연속으로 이어진 구간이 묶음의 중심이다. 상대 발화만 있는 구간은 묶음을 만들지 않는다
  (문체 예시는 본인 발화만 쓴다).
- 연속 구간이 길면 **`MAX_RUN_MESSAGES`건·`MAX_RUN_CHARS`자**마다 끊어 여러 묶음으로 나눈다. 단체방에서 다른
  사람을 건너뛰었거나(skip) 텔레그램 '저장한 메시지'처럼 전부 내 말인 대화는 구간 하나가 수백~수천 건이 되어,
  묶음 하나의 `style_text`가 통째로 프롬프트에 들어가 첨삭 한 번이 하루 토큰 한도를 넘었다 (2026-11-11 검토).
  보통 대화(구간이 몇 건 안 되는 것)의 묶음은 예전과 같다.
- 그 앞의 메시지를 최대 `window - 1`개까지 맥락으로 붙인다. 맥락은 임베딩과 출처 추적에만 쓰고 문체 예시
  (`style_text`)에는 들어가지 않는다 — 상대 말투가 섞이면 안 되기 때문이다.
- `context_text`는 "상대: …" / "나: …" 줄로 만든다. 질의(초안+상황)와 비슷한 장면을 kNN으로 찾기 위한 글이다.
"""

from __future__ import annotations

from app.domain.conversation import EpisodeDraft, Message

DEFAULT_WINDOW = 4
ROLE_LABEL = {"me": "나", "contact": "상대"}
MAX_RUN_MESSAGES = 6  # 묶음 하나에 담는 내 연속 발화 수 (설계의 "2~6개 턴")
MAX_RUN_CHARS = 600  # 묶음 하나의 style_text 글자 수 — 프롬프트의 예시 하나 상한(ai/prompt.py)과 같다


def split_run(run: list[Message]) -> list[list[Message]]:
    """내 연속 발화를 건수·글자 수 상한으로 나눈다. 한 건이 상한보다 길면 그 한 건이 묶음 하나다."""
    chunks: list[list[Message]] = []
    current: list[Message] = []
    chars = 0
    for m in run:
        added = len(m.text) + (1 if current else 0)  # 줄바꿈으로 이어 붙인 길이
        if current and (len(current) >= MAX_RUN_MESSAGES or chars + added > MAX_RUN_CHARS):
            chunks.append(current)
            current, chars, added = [], 0, len(m.text)
        current.append(m)
        chars += added
    if current:
        chunks.append(current)
    return chunks


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
        start = i
        for run in split_run(ordered[i:j]):
            room = max(window - len(run), 0)
            preceding = ordered[max(0, start - room) : start]
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
            start += len(run)
        i = j
    return drafts
