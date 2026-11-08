"""팀원 접점 도구: 시트 CSV → 합성 JSON 변환, 블라인드 평가 시트."""

from __future__ import annotations

import pytest

from app.tools.blind_sheet import build_sheet, merge_scores
from app.tools.synthetic_csv import ConversionError, convert, map_columns, parse_sent_at

CHARACTERS = {"박지현": "superior", "김준호": "family"}


def _rows(*tuples: tuple[str, ...], header=("대화방", "번호", "날짜시간", "말한 사람", "내용", "상대")):  # noqa: ANN001
    return [dict(zip(header, t, strict=True)) for t in tuples]


def test_map_columns_accepts_korean_and_english_and_reports_missing() -> None:
    assert map_columns(["﻿대화방", "번호", "날짜시간", "말한 사람", "내용"]) == {
        "conversation": "﻿대화방",
        "id": "번호",
        "sent_at": "날짜시간",
        "speaker": "말한 사람",
        "text": "내용",
    }
    assert (
        map_columns(["conversation", "id", "sent_at", "speaker", "text", "contact"])["contact"] == "contact"
    )
    with pytest.raises(ConversionError, match="필수 열"):
        map_columns(["대화방", "내용"])


def test_parse_sent_at_formats() -> None:
    assert parse_sent_at("2026-10-05 09:25").isoformat() == "2026-10-05T09:25:00+09:00"
    assert parse_sent_at("2026-10-05T09:25:00+09:00").isoformat() == "2026-10-05T09:25:00+09:00"
    assert parse_sent_at("2026/10/05 9:25").isoformat() == "2026-10-05T09:25:00+09:00"
    with pytest.raises(ValueError, match="날짜시간"):
        parse_sent_at("10월 5일")


def test_convert_new_dataset_and_merge_into_existing() -> None:
    rows = _rows(
        ("c5", "m01", "2026-10-20 10:00", "박지현", "민수씨 자료 봤어요?", "박지현"),
        ("c5", "m02", "2026-10-20 10:05", "김민수", "네 팀장님, 수정해서 다시 드리겠습니다.", ""),
        ("c6", "m01", "2026-10-21 20:00", "me", "야 주말에 뭐해", "김준호"),
    )
    dataset, report = convert(rows, "김민수", CHARACTERS)
    assert report.conversations_added == ["c5", "c6"] and report.messages_added == 3
    c5 = dataset["conversations"][0]
    assert (
        c5["contact"] == "박지현"
        and c5["relationship_type"] == "superior"
        and c5["title"] == "박지현와의 대화"
    )
    assert [m["speaker"] for m in c5["messages"]] == ["contact", "me"]
    assert c5["messages"][0]["sent_at"] == "2026-10-20T10:00:00+09:00"

    # 기존 파일에 합치기: 같은 id는 건너뛰고, 새 메시지는 시간 순으로 끼운다. 기존 메시지는 그대로.
    more = _rows(
        ("c5", "m02", "2026-10-20 10:05", "김민수", "(바뀐 내용 — 무시돼야 함)", ""),
        ("c5", "m03", "2026-10-20 09:50", "contact", "아 그리고 회의 10시 반으로", "박지현"),
    )
    merged, report2 = convert(more, "김민수", CHARACTERS, existing=dataset)
    assert (
        report2.messages_skipped == 1
        and report2.messages_added == 1
        and report2.conversations_updated == ["c5"]
    )
    msgs = merged["conversations"][0]["messages"]
    assert [m["id"] for m in msgs] == ["m03", "m01", "m02"]
    assert msgs[2]["text"] == "네 팀장님, 수정해서 다시 드리겠습니다."
    assert dataset["conversations"][0]["messages"][0]["id"] == "m01"  # 원본 dict는 안 바뀜


def test_convert_collects_all_problems_and_writes_nothing() -> None:
    rows = _rows(
        ("c7", "m01", "2026-10-20 10:00", "홍길동", "누구세요", "박지현"),  # 모르는 화자
        ("c7", "m02", "10월 5일", "me", "날짜 틀림", ""),
        ("c8", "m01", "2026-10-20 10:00", "me", "상대 없음", ""),
        ("c9", "m 1", "2026-10-20 10:00", "me", "번호 형식", "박지현"),
        ("c10", "m01", "2026-10-20 10:00", "me", "표에 없는 상대", "아무개"),
    )
    with pytest.raises(ConversionError) as exc:
        convert(rows, "김민수", CHARACTERS)
    text = "\n".join(exc.value.problems)
    for needle in ("홍길동", "날짜시간을 읽을 수 없습니다", "상대(contact)가 없습니다", "m 1", "아무개"):
        assert needle in text
    # --contact 기본값이 있으면 상대 없는 방도 통과
    dataset, _ = convert(
        _rows(("c8", "m01", "2026-10-20 10:00", "me", "상대 없음", "")),
        "김민수",
        CHARACTERS,
        default_contact="김준호",
    )
    assert dataset["conversations"][0]["contact"] == "김준호"


CASES = [
    {
        "case_id": "E001",
        "relation": "팀장",
        "situation": "보고",
        "original_text": "내일 오전까지 보내드릴게요.",
    },
    {"case_id": "E003", "relation": "동생", "situation": "부탁", "original_text": "차 좀 빌려줄 수 있어?"},
]
RESULTS = [
    {
        "case_id": "E001",
        "condition": "A",
        "repeat": "1",
        "result_text": "내일 오전까지 보내드릴게요. 양해 부탁드립니다.",
        "status": "recommended",
    },
    {
        "case_id": "E001",
        "condition": "B",
        "repeat": "1",
        "result_text": "팀장님, 내일 오전까지 보내드리겠습니다.",
        "status": "recommended",
    },
    {
        "case_id": "E001",
        "condition": "C",
        "repeat": "1",
        "result_text": "팀장님, 내일 오전까지 보내드리겠습니다.",
        "status": "recommended",
    },  # B와 동일
    {
        "case_id": "E003",
        "condition": "B",
        "repeat": "1",
        "result_text": "차 좀 빌려줄 수 있어? 돼?",
        "status": "recommended",
    },
    {
        "case_id": "E003",
        "condition": "C",
        "repeat": "1",
        "result_text": "차 좀 빌려줄 수 있어?",
        "status": "fallback_original",
    },  # 원문과 동일
]


def test_build_sheet_merges_identical_candidates_hides_conditions_and_is_reproducible() -> None:
    build = build_sheet(RESULTS, CASES, seed=3)
    # E001: A, B=C → 2줄 + 원문 1줄 / E003: B 1줄 + (C=원문) 1줄 → 총 5줄
    assert len(build.sheet_rows) == 5
    assert all(
        set(r) >= {"item_id", "case_id", "original_text", "candidate_text", "격식", "메모"}
        for r in build.sheet_rows
    )
    assert not any("condition" in r for r in build.sheet_rows)  # 시트에는 조건이 없다
    key = {k["candidate_text"]: k["conditions"] for k in build.key_rows}
    assert key["팀장님, 내일 오전까지 보내드리겠습니다."] == "B;C"
    assert key["차 좀 빌려줄 수 있어?"] == "C;ORIGINAL"  # 원문 유지 결과와 기준선이 합쳐진다
    assert key["내일 오전까지 보내드릴게요."] == "ORIGINAL"
    assert len({r["item_id"] for r in build.sheet_rows}) == 5 and all(
        len(r["item_id"]) == 6 for r in build.sheet_rows
    )
    again = build_sheet(RESULTS, CASES, seed=3)
    assert [r["item_id"] for r in again.sheet_rows] == [
        r["item_id"] for r in build.sheet_rows
    ]  # 같은 seed → 같은 시트
    assert build_sheet(RESULTS, CASES, seed=3, include_original=False).sheet_rows.__len__() == 4


def test_merge_scores_attaches_conditions_and_averages_numeric_columns() -> None:
    build = build_sheet(RESULTS, CASES, seed=3)
    scored = []
    for r in build.sheet_rows:
        s = dict(r)
        s["격식"] = "5" if "팀장님" in r["candidate_text"] else "2"
        s["자연스러움"] = "4"
        s["사실_의심(Y/N)"] = "Y" if "양해" in r["candidate_text"] else "N"
        s["메모"] = "ok"
        scored.append(s)
    long_rows, summary = merge_scores(scored, build.key_rows)
    assert sum(1 for r in long_rows if r["condition"] == "B") == 2  # E001 B(=C 합침), E003 B
    by_cond = {s["condition"]: s for s in summary}
    assert by_cond["A"]["격식"] == "2.00" and by_cond["A"]["사실_의심_비율"] == "1.00"
    assert by_cond["B"]["격식"] == "3.50" and by_cond["B"]["자연스러움"] == "4.00"
    assert "메모" not in by_cond["B"]  # 숫자가 아닌 열은 평균 안 냄
    with pytest.raises(ValueError, match="키에 없는"):
        merge_scores([{**scored[0], "item_id": "zzzzzz"}], build.key_rows)
