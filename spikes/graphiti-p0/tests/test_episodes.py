"""데이터셋 → 에피소드 변환 테스트 (네트워크 불필요)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from p0.episodes import DatasetError, build_episodes, load_dataset

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample_conversations.json"


def _dataset() -> dict[str, Any]:
    return {
        "user": {"name": "김민수"},
        "conversations": [
            {
                "conversation_id": "c1",
                "contact": "박지현",
                "messages": [
                    {"id": "m2", "speaker": "contact", "sent_at": "2026-10-05T10:00:00+09:00", "text": "네"},
                    {"id": "m1", "speaker": "me", "sent_at": "2026-10-05T09:00:00+09:00", "text": "  안녕하세요  "},
                ],
            },
            {
                "conversation_id": "c2",
                "contact": "김준호",
                "messages": [
                    {"id": "m1", "speaker": "contact", "sent_at": "2026-10-05T09:30:00+09:00", "text": "형"},
                ],
            },
        ],
    }


def test_sorted_across_conversations_by_time() -> None:
    names = [ep.name for ep in build_episodes(_dataset())]
    assert names == ["c1:m1", "c2:m1", "c1:m2"]


def test_speaker_mapping_and_strip() -> None:
    first = build_episodes(_dataset())[0]
    assert first.body == "김민수: 안녕하세요"
    assert first.source_description == "과거 대화 (박지현와의 대화)"


def test_same_message_id_allowed_in_different_conversations() -> None:
    assert len(build_episodes(_dataset())) == 3


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["conversations"][0]["messages"][0].update(text="   "), "'text'"),
        (lambda d: d["conversations"][0]["messages"][0].update(sent_at="2026-10-05T10:00:00"), "시간대"),
        (lambda d: d["conversations"][0]["messages"][0].update(sent_at="어제"), "ISO 8601"),
        (lambda d: d["conversations"][0]["messages"][0].update(speaker="boss"), "speaker"),
        (lambda d: d["conversations"][0]["messages"][0].update(id="m1"), "중복"),
        (lambda d: d["conversations"][0].update(messages=[]), "'messages'"),
        (lambda d: d.update(conversations=[]), "'conversations'"),
        (lambda d: d.pop("user"), "'user'"),
    ],
)
def test_invalid_dataset_raises_with_location(mutate: Any, message: str) -> None:
    data = copy.deepcopy(_dataset())
    mutate(data)
    with pytest.raises(DatasetError, match=message):
        build_episodes(data)


def test_sample_dataset_is_valid() -> None:
    episodes = build_episodes(load_dataset(SAMPLE))
    assert len(episodes) == 36
    assert all(ep.reference_time.tzinfo is not None for ep in episodes)
    times = [ep.reference_time for ep in episodes]
    assert times == sorted(times)
