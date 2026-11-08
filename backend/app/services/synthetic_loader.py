"""`data/synthetic/*.json` 데이터셋을 한 사용자의 상대·대화방·메시지로 적재한다 (멱등).

`scripts/import_synthetic.py`와 `evals/run_eval.py`(C/D 조건)가 같이 쓴다.
상대는 별칭(데이터셋의 contact 이름)으로 찾고 없으면 만든다. 관계 유형은 대화방의 `relationship_type`,
없으면 `evals/characters.csv`의 값, 그것도 없으면 acquaintance.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.domain.persona import RelationshipType
from app.services.import_service import ImportMessage, ImportPayload

if TYPE_CHECKING:
    from app.api.deps import AppState


@dataclass
class LoadReport:
    user_id: str
    contacts: dict[str, UUID] = field(default_factory=dict)  # 별칭 → contact_id
    conversations: dict[str, UUID] = field(default_factory=dict)  # source_key → conversation_id
    imported: int = 0
    duplicates: int = 0
    episodes: int = 0
    graph_episodes: int = 0  # 그래프에 새로 넣은 에피소드 수 (graph=True일 때)
    failed: list[str] = field(default_factory=list)


def load_dataset(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict) or not isinstance(data.get("conversations"), list):
        raise ValueError(f"데이터셋 형식 오류: {path}")
    return data


def load_relationship_types(characters_csv: Path | None) -> dict[str, str]:
    if characters_csv is None or not characters_csv.is_file():
        return {}
    with characters_csv.open(encoding="utf-8-sig", newline="") as fp:
        return {row["name"].strip(): row["relationship_type"].strip() for row in csv.DictReader(fp)}


async def load_synthetic(
    state: AppState,
    dataset_path: Path,
    user_id: str,
    characters_csv: Path | None = None,
    *,
    graph: bool = False,
) -> LoadReport:
    """graph=True면 적재 뒤 미수집 메시지를 전체 시각 순으로 그래프에 넣는다 (NEO4J_URI 필요, haiku 호출).

    가져오기 중에는 백그라운드 그래프 수집을 끈다 — 스크립트가 끝나면 태스크가 죽고, 순서도 대화방별로 섞인다.
    """
    data = load_dataset(dataset_path)
    rel_types = load_relationship_types(characters_csv)
    report = LoadReport(user_id=user_id)
    me_name = str((data.get("user") or {}).get("name") or "").strip() or None
    previous_mode = state.import_service.graph_mode
    state.import_service.graph_mode = "off"

    by_alias = {c.alias: c for c in await state.contacts.list(user_id)}
    by_key = {c.source_key: c for c in await state.conversations.list(user_id)}

    for conv in data["conversations"]:
        alias = str(conv["contact"]).strip()
        key = str(conv["conversation_id"]).strip()
        contact = by_alias.get(alias)
        if contact is None:
            rel: RelationshipType = conv.get("relationship_type") or rel_types.get(alias) or "acquaintance"  # type: ignore[assignment]
            contact = await state.contacts.create(user_id, alias, rel)
            by_alias[alias] = contact
        report.contacts[alias] = contact.id

        conversation = by_key.get(key)
        if conversation is None:
            conversation = await state.import_service.create_conversation(
                user_id, contact.id, conv.get("title"), f"synthetic:{dataset_path.name}", key, me_name
            )
            by_key[key] = conversation
        elif me_name and not conversation.me_name:
            # me_name 열이 없던 때 만든 대화방. 이미 그래프에 들어간 에피소드의 '사용자' 표기는 그대로다.
            conversation = await state.conversations.set_me_name(user_id, conversation.id, me_name)
            by_key[key] = conversation
        report.conversations[key] = conversation.id

        payload = ImportPayload(
            source=f"synthetic:{dataset_path.name}",
            messages=[
                ImportMessage(id=m["id"], speaker=m["speaker"], sent_at=m["sent_at"], text=m["text"])
                for m in conv["messages"]
            ],
        )
        job = await state.import_service.import_messages(user_id, conversation.id, payload)
        report.imported += job.imported
        report.duplicates += job.duplicates
        report.episodes += job.episodes
        if job.status != "completed":
            report.failed.append(f"{key}: {job.error_code} {job.error_message}")
    state.import_service.graph_mode = previous_mode

    if graph:
        if state.graph_ingest is None:
            raise RuntimeError("그래프 수집에는 NEO4J_URI가 필요합니다 (.env).")
        report.graph_episodes = await state.graph_ingest.ingest_pending(user_id)
    return report
