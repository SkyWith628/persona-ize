"""시나리오 담당의 시트 CSV → `data/synthetic/*.json` (docs/dataset-guide.md 5절, data/synthetic/README.md).

CSV 열(한글·영문 둘 다 받는다, 순서 무관):
  대화방|conversation   번호|id   날짜시간|sent_at   말한 사람|speaker   내용|text
  선택: 상대|contact (대화방마다 한 번 이상)   제목|title
- 말한 사람: `me`/`contact`, 또는 이름. 이름이 사용자 이름이면 me, 상대 이름이면 contact. 그 밖의 이름은 오류.
- 날짜시간: `2026-10-05 09:25`(한국 시간으로 본다), `2026-10-05T09:25:00+09:00`, `2026-10-05 9:25` 모두 가능.
- 상대는 `evals/characters.csv`에 있어야 한다 (relationship_type을 거기서 가져온다).
- 기존 JSON에 합칠 때 같은 대화방·메시지 id는 건너뛴다(보고에 적는다). 기존 메시지는 바꾸지 않는다 —
  다른 파일이 `c1:m03` 형식으로 가리키기 때문이다.
잘못된 줄은 전부 모아서 한 번에 보고하고 아무것도 쓰지 않는다.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))

COLUMN_ALIASES = {
    "conversation": ("대화방", "conversation", "conversation_id", "방"),
    "id": ("번호", "id", "message_id", "메시지"),
    "sent_at": ("날짜시간", "sent_at", "시각", "datetime", "일시"),
    "speaker": ("말한 사람", "말한사람", "speaker", "화자"),
    "text": ("내용", "text", "본문"),
    "contact": ("상대", "contact"),
    "title": ("제목", "title"),
}


class ConversionError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class ConversionReport:
    conversations_added: list[str] = field(default_factory=list)
    conversations_updated: list[str] = field(default_factory=list)
    messages_added: int = 0
    messages_skipped: int = 0  # 이미 있는 id
    warnings: list[str] = field(default_factory=list)


def _norm_header(name: str) -> str:
    return name.strip().lstrip("﻿").lower().replace("_", " ")


def map_columns(header: list[str]) -> dict[str, str]:
    """표준 이름 → 실제 열 이름. 필수 열이 없으면 ConversionError."""
    found: dict[str, str] = {}
    normalized = {_norm_header(h): h for h in header}
    for key, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if _norm_header(alias) in normalized:
                found[key] = normalized[_norm_header(alias)]
                break
    missing = [k for k in ("conversation", "id", "sent_at", "speaker", "text") if k not in found]
    if missing:
        raise ConversionError(
            [
                f"필수 열이 없습니다: {missing}. 열 이름은 대화방·번호·날짜시간·말한 사람·내용 (영문 가능). "
                f"받은 열: {header}"
            ]
        )
    return found


_TIME_PATTERNS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y/%m/%d %H:%M")


def parse_sent_at(raw: str) -> datetime:
    raw = (
        raw.strip().replace("T", " ")
        if "T" in raw.strip() and "+" not in raw and "Z" not in raw
        else raw.strip()
    )
    try:
        parsed = datetime.fromisoformat(raw.replace(" ", "T") if "+" in raw or raw.endswith("Z") else raw)
        if parsed.tzinfo is not None:
            return parsed
        return parsed.replace(tzinfo=KST)
    except ValueError:
        pass
    for pattern in _TIME_PATTERNS:
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=KST)
        except ValueError:
            continue
    raise ValueError(f"날짜시간을 읽을 수 없습니다: {raw!r} (예: 2026-10-05 09:25)")


def load_characters(path: Path) -> dict[str, str]:
    """이름 → relationship_type."""
    with path.open(encoding="utf-8-sig", newline="") as fp:
        return {row["name"].strip(): row["relationship_type"].strip() for row in csv.DictReader(fp)}


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        if reader.fieldnames is None:
            raise ConversionError(["CSV가 비어 있습니다."])
        rows = [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in reader]
    return rows


def convert(
    rows: list[dict[str, str]],
    me_name: str,
    characters: dict[str, str],
    existing: dict[str, Any] | None = None,
    default_contact: str | None = None,
) -> tuple[dict[str, Any], ConversionReport]:
    """CSV 행들을 데이터셋 dict로.

    existing이 있으면 거기에 합친다 (원본 dict는 건드리지 않고 새 dict를 돌려준다).
    """
    if not rows:
        raise ConversionError(["CSV에 데이터 줄이 없습니다."])
    cols = map_columns(list(rows[0].keys()))
    problems: list[str] = []
    report = ConversionReport()

    # 대화방별로 모은다
    by_conv: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for idx, row in enumerate(rows, start=2):  # 1행은 머리글
        conv_id = row.get(cols["conversation"], "")
        if not conv_id:
            if any(row.values()):
                problems.append(f"{idx}행: 대화방이 비어 있습니다.")
            continue
        by_conv.setdefault(conv_id, []).append((idx, row))

    dataset: dict[str, Any] = (
        json.loads(json.dumps(existing)) if existing else {"user": {"name": me_name}, "conversations": []}
    )
    dataset.setdefault("user", {"name": me_name})
    dataset.setdefault("conversations", [])
    user_name = str(dataset["user"].get("name") or me_name).strip()
    conv_index = {c["conversation_id"]: c for c in dataset["conversations"]}

    for conv_id, items in by_conv.items():
        contact = next(
            (r.get(cols["contact"], "") for _, r in items if cols.get("contact") and r.get(cols["contact"])),
            "",
        )
        existing_conv = conv_index.get(conv_id)
        if not contact:
            contact = (existing_conv or {}).get("contact") or default_contact or ""
        if not contact:
            problems.append(
                f"대화방 {conv_id}: 상대(contact)가 없습니다. '상대' 열을 채우거나 --contact를 주세요."
            )
            continue
        if contact not in characters:
            problems.append(
                f"대화방 {conv_id}: 상대 '{contact}'가 evals/characters.csv에 없습니다. "
                "등장인물 표에 먼저 추가하세요."
            )
            continue
        if existing_conv is not None and existing_conv.get("contact") != contact:
            problems.append(
                f"대화방 {conv_id}: 기존 상대 '{existing_conv.get('contact')}'와 다른 '{contact}'입니다."
            )
            continue

        title = next(
            (r.get(cols["title"], "") for _, r in items if cols.get("title") and r.get(cols["title"])), ""
        )
        messages = list(existing_conv["messages"]) if existing_conv else []
        seen = {m["id"] for m in messages}
        new_msgs: list[dict[str, Any]] = []
        for idx, row in items:
            mid = row.get(cols["id"], "")
            if not mid:
                problems.append(f"{idx}행 (대화방 {conv_id}): 번호가 비어 있습니다.")
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", mid):
                problems.append(f"{idx}행: 번호 '{mid}'는 영문·숫자·_·-만 됩니다 (예: m01).")
                continue
            if mid in seen:
                report.messages_skipped += 1
                continue
            speaker_raw = row.get(cols["speaker"], "")
            if speaker_raw in ("me", "contact"):
                speaker = speaker_raw
            elif speaker_raw == user_name:
                speaker = "me"
            elif speaker_raw == contact:
                speaker = "contact"
            else:
                problems.append(
                    f"{idx}행: 말한 사람 '{speaker_raw}'를 모릅니다. "
                    f"me/contact 또는 '{user_name}'/'{contact}'를 쓰세요."
                )
                continue
            try:
                sent_at = parse_sent_at(row.get(cols["sent_at"], ""))
            except ValueError as exc:
                problems.append(f"{idx}행: {exc}")
                continue
            text = row.get(cols["text"], "")
            if not text:
                problems.append(f"{idx}행: 내용이 비어 있습니다.")
                continue
            seen.add(mid)
            new_msgs.append({"id": mid, "speaker": speaker, "sent_at": sent_at.isoformat(), "text": text})

        if problems:
            continue
        new_msgs.sort(key=lambda m: m["sent_at"])
        if existing_conv is None:
            dataset["conversations"].append(
                {
                    "conversation_id": conv_id,
                    "title": title or f"{contact}와의 대화",
                    "contact": contact,
                    "relationship_type": characters[contact],
                    "messages": new_msgs,
                }
            )
            report.conversations_added.append(conv_id)
        else:
            if new_msgs:
                existing_conv["messages"] = sorted(messages + new_msgs, key=lambda m: m["sent_at"])
                report.conversations_updated.append(conv_id)
            if title and existing_conv.get("title") != title:
                report.warnings.append(
                    f"대화방 {conv_id}: 제목은 기존 것을 유지했습니다 ('{existing_conv.get('title')}')."
                )
        report.messages_added += len(new_msgs)

    if problems:
        raise ConversionError(problems)
    if report.messages_added == 0 and not report.conversations_added:
        report.warnings.append("새로 추가된 메시지가 없습니다 (전부 이미 있음).")
    return dataset, report


def write_dataset(dataset: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
