"""graph_queries.csv 로딩과 Recall@k 계산 테스트."""

from __future__ import annotations

from pathlib import Path

from p0.queries import load_queries, parse_ids, recall_at_k

REPO_QUERIES = Path(__file__).resolve().parents[2] / "graph_queries.csv"


def test_parse_ids_strips_blanks() -> None:
    assert parse_ids("c1:m03; c1:m04;;") == ("c1:m03", "c1:m04")
    assert parse_ids("") == ()


def test_recall_at_k_dedups_and_cuts() -> None:
    relevant = ("c1:m03", "c1:m04", "c1:m05")
    assert recall_at_k(["c1:m03", "c1:m03", "c1:m04", "c9:m01"], relevant, k=5) == 2 / 3
    # k=2에서 중복 제거 후 상위 2개만 본다
    assert recall_at_k(["c1:m03", "c1:m03", "c1:m04", "c1:m05"], relevant, k=2) == 2 / 3
    assert recall_at_k([], relevant, k=5) == 0.0
    assert recall_at_k(["c1:m03"], (), k=5) == 0.0


def test_load_repo_queries() -> None:
    queries = load_queries(REPO_QUERIES)
    assert [q.query_id for q in queries] == ["q01", "q02", "q03", "q04"]
    q03 = next(q for q in queries if q.query_id == "q03")
    assert q03.needs_graph is True
    assert q03.relevant_ids == ("c3:m05", "c3:m06", "c3:m08")


def test_load_skips_rows_without_answers(tmp_path: Path) -> None:
    csv_path = tmp_path / "q.csv"
    csv_path.write_text(
        "query_id,contact,query_text,relevant_message_ids,needs_graph,note\n"
        "q01,박지현,질의,c1:m01,Y,\n"
        "q02,박지현,정답 없음,,N,\n",
        encoding="utf-8",
    )
    assert [q.query_id for q in load_queries(csv_path)] == ["q01"]
