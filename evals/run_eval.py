"""평가 사례 × 조건을 백엔드 워크플로로 실행해 결과 CSV를 만든다.

사용 예:
    python evals/run_eval.py                                  # fake 프로바이더, A/B, dev+test
    python evals/run_eval.py --split dev --conditions A,B,C --repeat 3 --provider anthropic

입력 열은 docs/dataset-guide.md 기준 (case_id, relation, situation, original_text, ..., as_of, split).
relation → 상대 정보는 evals/characters.csv 에서 찾는다.

C/D 조건은 과거 대화가 필요하다. `--dataset`(기본 data/synthetic/sample_conversations.json)을 `--user`로
적재한 뒤(멱등) 그 사용자의 상대 id로 검색한다. 저장소는 .env의 DATABASE_URL(없으면 인메모리),
임베딩은 EMBEDDING_PROVIDER(local=e5-small, 첫 실행에 모델 다운로드; `--embedding fake`로 빠른 점검).
검색 기준 시각은 사례의 `as_of` 날짜 당일 끝(한국 시간)이다 — 그 날까지의 대화만 본다.

결과: evals/results/eval-<시각>.csv (요약) + .jsonl (원시, 커밋 제외)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.deps import AppState, build_state  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.domain.contact import Contact  # noqa: E402
from app.domain.persona import PersonaSettings  # noqa: E402
from app.domain.rewrite import RewriteInput  # noqa: E402
from app.services.synthetic_loader import load_synthetic  # noqa: E402

DEFAULT_CASES = ROOT / "evals" / "eval_cases.csv"
DEFAULT_CHARACTERS = ROOT / "evals" / "characters.csv"
DEFAULT_DATASET = ROOT / "data" / "synthetic" / "sample_conversations.json"
RESULTS_DIR = ROOT / "evals" / "results"
KST = timezone(timedelta(hours=9))
RETRIEVAL_CONDITIONS = ("C", "D")

# 관계 유형별 기본 페르소나. 실제 실험에서는 사례별 설정 열을 추가해 덮어쓴다.
DEFAULT_PERSONAS: dict[str, PersonaSettings] = {
    "superior": PersonaSettings(formality=5, friendliness=2, directness=3, forbidden_phrases=["ㅋㅋ", "ㅠ"]),
    "colleague": PersonaSettings(formality=3, friendliness=4, directness=4),
    "subordinate": PersonaSettings(formality=3, friendliness=3, directness=4),
    "friend": PersonaSettings(formality=1, friendliness=5, directness=5, emoji="rare"),
    "family": PersonaSettings(formality=1, friendliness=5, directness=5),
    "partner": PersonaSettings(formality=1, friendliness=5, directness=3, emoji="often"),
    "acquaintance": PersonaSettings(formality=4, friendliness=2, directness=3),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Persona_ize 비교 실험")
    p.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    p.add_argument("--characters", type=Path, default=DEFAULT_CHARACTERS)
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="C/D용 과거 대화 (data/synthetic)")
    p.add_argument("--user", default="eval", help="과거 대화를 적재·검색할 사용자 id")
    p.add_argument("--conditions", default="A,B", help="쉼표 구분: A,B,C,D")
    p.add_argument(
        "--split",
        choices=["dev", "test", "all"],
        default="all",
        help="실행할 사례 묶음",
    )
    p.add_argument("--repeat", type=int, default=1, help="사례당 반복 횟수 (변동 관찰)")
    p.add_argument("--provider", choices=["fake", "anthropic"], default="fake")
    p.add_argument("--model", default=None, help="LLM_MODEL 덮어쓰기")
    p.add_argument("--embedding", choices=["local", "fake"], default=None, help="EMBEDDING_PROVIDER 덮어쓰기")
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="요약 CSV 경로 (기본 evals/results/eval-<시각>.csv)",
    )
    args = p.parse_args(argv)
    args.condition_list = [c.strip().upper() for c in args.conditions.split(",") if c.strip()]
    bad = [c for c in args.condition_list if c not in ("A", "B", "C", "D")]
    if bad:
        p.error(f"알 수 없는 조건: {bad}")
    if args.repeat < 1:
        p.error("--repeat은 1 이상")
    return args


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fp:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fp)]


def load_characters(path: Path) -> dict[str, dict[str, str]]:
    """relation(팀장/동기/…) → 등장인물 행. 가이드 4단계의 표."""
    rows = load_csv(path)
    if not rows:
        raise SystemExit(f"등장인물이 없습니다: {path}")
    return {row["relation"]: row for row in rows}


def load_cases(path: Path, split: str) -> list[dict[str, str]]:
    rows = load_csv(path)
    if split != "all":
        rows = [r for r in rows if r.get("split", "dev") == split]
    if not rows:
        raise SystemExit(f"사례가 없습니다: {path} (split={split})")
    return rows


def build_persona(character: dict[str, str]) -> PersonaSettings:
    base = DEFAULT_PERSONAS.get(character["relationship_type"], PersonaSettings())
    aliases = [a.strip() for a in character.get("aliases", "").split(";") if a.strip()]
    if base.formality >= 4 and aliases:
        # 상사·지인은 첫 호칭을 기본 호칭으로 쓴다 (검증에서 호칭 추가를 허용하기 위해).
        return base.model_copy(update={"address_term": aliases[0]})
    return base


def parse_as_of(raw: str) -> datetime | None:
    """사례의 as_of(YYYY-MM-DD) → 그날 끝(KST). 비어 있으면 None(지금)."""
    if not raw:
        return None
    day = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=KST)
    return day + timedelta(days=1)


def parse_ids(raw: str) -> set[str]:
    return {x.strip() for x in raw.split(";") if x.strip()}


async def prepare_contacts(
    state: AppState, args: argparse.Namespace, characters: dict[str, dict[str, str]]
) -> dict[str, Contact]:
    """relation → Contact. 검색 조건이 있으면 데이터셋을 적재하고 저장소의 상대를 쓴다."""
    if any(c in RETRIEVAL_CONDITIONS for c in args.condition_list):
        report = await load_synthetic(state, args.dataset, args.user, args.characters)
        if report.failed:
            raise SystemExit("과거 대화 적재 실패: " + "; ".join(report.failed))
        print(
            f"과거 대화 적재: user={args.user} storage={state.storage} embedder={state.embedder.version} "
            f"상대 {len(report.contacts)} 대화방 {len(report.conversations)} "
            f"새 메시지 {report.imported} 중복 {report.duplicates} 묶음 {report.episodes}"
        )
        by_alias = {c.alias: c for c in await state.contacts.list(args.user)}
        out: dict[str, Contact] = {}
        for relation, ch in characters.items():
            c = by_alias.get(ch["name"])
            if c is None:  # 데이터셋에 없는 등장인물은 검색 없이 가짜 상대로
                c = await state.contacts.create(args.user, ch["name"], ch["relationship_type"])  # type: ignore[arg-type]
            out[relation] = c
        return out
    return {
        relation: Contact(
            id=uuid4(),
            user_id=args.user,
            alias=ch["name"],
            relationship_type=ch["relationship_type"],  # type: ignore[arg-type]
            created_at=datetime.now(UTC),
        )
        for relation, ch in characters.items()
    }


async def run(args: argparse.Namespace) -> Path:
    overrides: dict[str, str] = {"llm_provider": args.provider}
    if args.model:
        overrides["llm_model"] = args.model
    if args.embedding:
        overrides["embedding_provider"] = args.embedding
    settings = Settings(**overrides)  # type: ignore[arg-type]  # .env도 읽는다
    if args.provider == "anthropic" and settings.llm_provider != "anthropic":
        raise SystemExit("설정 오류")

    state = build_state(settings)
    try:
        return await _run_with_state(state, args)
    finally:
        await state.close()


async def _run_with_state(state: AppState, args: argparse.Namespace) -> Path:
    workflow = state.workflow
    characters = load_characters(args.characters)
    cases = load_cases(args.cases, args.split)
    unknown = sorted({c["relation"] for c in cases} - characters.keys())
    if unknown:
        raise SystemExit(f"characters.csv 에 없는 relation: {unknown} (가이드 4단계 표를 먼저 채운다)")
    contacts = await prepare_contacts(state, args, characters)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = args.out or RESULTS_DIR / f"eval-{stamp}.csv"
    out_jsonl = RESULTS_DIR / f"eval-{stamp}.jsonl"

    fields = [
        "case_id",
        "split",
        "relation",
        "condition",
        "repeat",
        "status",
        "validation_passed",
        "violations",
        "intent",
        "result_text",
        "attempts",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "model_version",
        "prompt_version",
        "retrieval_version",
        "retrieved_ids",
        "evidence_ids",
        "recall",
    ]
    total = len(cases) * len(args.condition_list) * args.repeat
    done = 0
    with (
        out_csv.open("w", encoding="utf-8", newline="") as fcsv,
        out_jsonl.open("w", encoding="utf-8") as fjs,
    ):
        writer = csv.DictWriter(fcsv, fieldnames=fields)
        writer.writeheader()
        for case in cases:
            character = characters[case["relation"]]
            contact = contacts[case["relation"]]
            persona = build_persona(character)
            relevant = parse_ids(case.get("relevant_msg_ids", ""))
            as_of = parse_as_of(case.get("as_of", ""))
            for cond in args.condition_list:
                for rep in range(1, args.repeat + 1):
                    inp = RewriteInput(
                        user_id=args.user,
                        contact_id=contact.id,
                        draft_text=case["original_text"],
                        draft_version=1,
                        situation=case.get("situation", ""),
                        condition=cond,  # type: ignore[arg-type]
                        as_of=as_of,
                    )
                    retrieved = await workflow.retrievers[cond].retrieve(  # type: ignore[index]
                        user_id=inp.user_id,
                        contact_id=inp.contact_id,
                        draft_text=inp.draft_text,
                        situation=inp.situation,
                        as_of=inp.as_of,
                    )
                    retrieved_ids = sorted({mid for ex in retrieved for mid in ex.source_message_ids})
                    recall = (
                        f"{len(relevant & set(retrieved_ids)) / len(relevant):.2f}"
                        if relevant and cond in RETRIEVAL_CONDITIONS
                        else ""
                    )
                    result = await workflow.run(inp, contact, persona)
                    done += 1
                    row = {
                        "case_id": case["case_id"],
                        "split": case.get("split", "dev"),
                        "relation": case["relation"],
                        "condition": cond,
                        "repeat": rep,
                        "status": result.status,
                        "validation_passed": result.validation.passed,
                        "violations": " | ".join(v.detail for v in result.validation.violations),
                        "intent": result.intent,
                        "result_text": result.result_text,
                        "attempts": result.attempts,
                        "latency_ms": result.latency_ms,
                        "input_tokens": result.token_usage.get("input_tokens", 0),
                        "output_tokens": result.token_usage.get("output_tokens", 0),
                        "model_version": result.model_version,
                        "prompt_version": result.prompt_version,
                        "retrieval_version": result.retrieval_version,
                        "retrieved_ids": ";".join(retrieved_ids),
                        "evidence_ids": ";".join(result.evidence_ids),
                        "recall": recall,
                    }
                    writer.writerow(row)
                    fjs.write(
                        json.dumps(
                            {
                                **row,
                                "retrieved": [ex.model_dump(mode="json") for ex in retrieved],
                                "full": result.model_dump(mode="json"),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    print(
                        f"[{done}/{total}] {case['case_id']} {cond} r{rep} "
                        f"{result.status} {result.latency_ms}ms" + (f" recall={recall}" if recall else "")
                    )
    print(f"\n요약 → {out_csv}\n원시 → {out_jsonl}")
    return out_csv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
