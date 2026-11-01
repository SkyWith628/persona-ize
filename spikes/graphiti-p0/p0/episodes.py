"""대화 데이터셋(JSON)을 Graphiti 에피소드 입력으로 변환한다.

잘못된 메시지는 건너뛰지 않고 위치를 담아 예외를 낸다. 조용히 누락되면 추출 품질을 잘못 판단하게 된다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SPEAKER_ME = "me"
SPEAKER_CONTACT = "contact"


class DatasetError(ValueError):
    """데이터셋 형식이 잘못되었을 때 발생한다."""


@dataclass(frozen=True)
class EpisodeInput:
    """`Graphiti.add_episode()`에 넘길 값 한 건."""

    name: str
    body: str
    reference_time: datetime
    source_description: str


def load_dataset(path: str | Path) -> dict[str, Any]:
    """UTF-8 JSON 데이터셋을 읽는다."""
    with Path(path).open(encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise DatasetError("데이터셋 최상위는 객체여야 합니다.")
    return data


def _require_str(obj: dict[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{where}: '{key}' 값이 비어 있거나 문자열이 아닙니다.")
    return value.strip()


def _parse_time(raw: str, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise DatasetError(f"{where}: sent_at을 ISO 8601로 해석할 수 없습니다: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise DatasetError(f"{where}: sent_at에 시간대가 없습니다 (예: +09:00).")
    return parsed


def build_episodes(dataset: dict[str, Any]) -> list[EpisodeInput]:
    """데이터셋의 모든 메시지를 발화 시각 순으로 정렬한 에피소드 목록으로 만든다.

    Graphiti는 에피소드를 넣는 순서대로 이전 사실을 무효화하므로 시간 순 정렬이 필수다.
    """
    user = dataset.get("user")
    if not isinstance(user, dict):
        raise DatasetError("'user' 객체가 없습니다.")
    user_name = _require_str(user, "name", "user")

    conversations = dataset.get("conversations")
    if not isinstance(conversations, list) or not conversations:
        raise DatasetError("'conversations'가 비어 있습니다.")

    episodes: list[EpisodeInput] = []
    for c_idx, conv in enumerate(conversations):
        where_conv = f"conversations[{c_idx}]"
        if not isinstance(conv, dict):
            raise DatasetError(f"{where_conv}: 객체여야 합니다.")
        conv_id = _require_str(conv, "conversation_id", where_conv)
        contact = _require_str(conv, "contact", where_conv)
        title = conv.get("title") or f"{contact}와의 대화"
        speaker_names = {SPEAKER_ME: user_name, SPEAKER_CONTACT: contact}

        messages = conv.get("messages")
        if not isinstance(messages, list) or not messages:
            raise DatasetError(f"{where_conv}: 'messages'가 비어 있습니다.")

        seen_ids: set[str] = set()
        for m_idx, msg in enumerate(messages):
            where = f"{where_conv}.messages[{m_idx}]"
            if not isinstance(msg, dict):
                raise DatasetError(f"{where}: 객체여야 합니다.")
            msg_id = _require_str(msg, "id", where)
            if msg_id in seen_ids:
                raise DatasetError(f"{where}: 메시지 id가 중복됩니다: {msg_id}")
            seen_ids.add(msg_id)

            speaker = _require_str(msg, "speaker", where)
            if speaker not in speaker_names:
                raise DatasetError(f"{where}: speaker는 'me' 또는 'contact'여야 합니다: {speaker!r}")

            episodes.append(
                EpisodeInput(
                    name=f"{conv_id}:{msg_id}",
                    body=f"{speaker_names[speaker]}: {_require_str(msg, 'text', where)}",
                    reference_time=_parse_time(_require_str(msg, "sent_at", where), where),
                    source_description=f"과거 대화 ({title})",
                )
            )

    # sorted는 안정 정렬이라 같은 시각이면 데이터셋 순서를 유지한다.
    return sorted(episodes, key=lambda ep: ep.reference_time)
