"""평가 사례 × 조건을 백엔드 워크플로로 실행해 결과 CSV를 만든다.

사용 예:
    python evals/run_eval.py                                  # fake 프로바이더, A/B
    python evals/run_eval.py --conditions A,B,C --repeat 3 --provider anthropic

결과: evals/results/eval-<시각>.csv (요약) + .jsonl (원시, 커밋 제외)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai.providers import build_provider  # noqa: E402
from app.ai.retrieval.base import NullRetriever  # noqa: E402
from app.ai.workflow import RewriteWorkflow  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.domain.contact import Contact  # noqa: E402
from app.domain.persona import PersonaSettings  # noqa: E402
from app.domain.rewrite import RewriteInput  # noqa: E402

DEFAULT_CASES = ROOT / "evals" / "eval_cases.csv"
RESULTS_DIR = ROOT / "evals" / "results"

# 관계 유형별 기본 페르소나. 실제 실험에서는 사례별 설정 열을 추가해 덮어쓴다.
DEFAULT_PERSONAS: dict[str, PersonaSettings] = {
    "superior": PersonaSettings(
        formality=5, friendliness=2, directness=3, address_term="팀장님", forbidden_phrases=["ㅋㅋ", "ㅠ"]
    ),
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
    p.add_argument("--conditions", default="A,B", help="쉼표 구분: A,B,C,D")
    p.add_argument("--repeat", type=int, default=1, help="사례당 반복 횟수 (변동 관찰)")
    p.add_argument("--provider", choices=["fake", "anthropic"], default="fake")
    p.add_argument("--model", default=None, help="LLM_MODEL 덮어쓰기")
    p.add_argument(
        "--out", type=Path, default=None, help="요약 CSV 경로 (기본 evals/results/eval-<시각>.csv)"
    )
    args = p.parse_args(argv)
    args.condition_list = [c.strip().upper() for c in args.conditions.split(",") if c.strip()]
    bad = [c for c in args.condition_list if c not in ("A", "B", "C", "D")]
    if bad:
        p.error(f"알 수 없는 조건: {bad}")
    if args.repeat < 1:
        p.error("--repeat은 1 이상")
    return args


def load_cases(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fp:
        rows = list(csv.DictReader(fp))
    if not rows:
        raise SystemExit(f"사례가 없습니다: {path}")
    return rows


async def run(args: argparse.Namespace) -> Path:
    overrides = {"llm_provider": args.provider}
    if args.model:
        overrides["llm_model"] = args.model
    settings = Settings(**overrides)  # .env도 읽는다
    if args.provider == "anthropic" and settings.llm_provider != "anthropic":
        raise SystemExit("설정 오류")

    null = NullRetriever()
    workflow = RewriteWorkflow(
        provider=build_provider(settings),
        retrievers={"A": null, "B": null, "C": null, "D": null},  # C/D는 2~3주차에 교체
        prompt_version=settings.prompt_version,
    )
    cases = load_cases(args.cases)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = args.out or RESULTS_DIR / f"eval-{stamp}.csv"
    out_jsonl = RESULTS_DIR / f"eval-{stamp}.jsonl"

    fields = [
        "case_id",
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
            contact = Contact(
                id=uuid4(),
                user_id="eval",
                alias=case["contact_alias"],
                relationship_type=case["relationship_type"],
                created_at=datetime.now(),  # type: ignore[arg-type]
            )
            persona = DEFAULT_PERSONAS.get(case["relationship_type"], PersonaSettings())
            for cond in args.condition_list:
                for rep in range(1, args.repeat + 1):
                    inp = RewriteInput(
                        user_id="eval",
                        contact_id=contact.id,
                        draft_text=case["draft_text"],
                        draft_version=1,
                        situation=case.get("situation", ""),
                        condition=cond,  # type: ignore[arg-type]
                    )
                    result = await workflow.run(inp, contact, persona)
                    done += 1
                    row = {
                        "case_id": case["case_id"],
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
                    }
                    writer.writerow(row)
                    fjs.write(
                        json.dumps({**row, "full": result.model_dump(mode="json")}, ensure_ascii=False) + "\n"
                    )
                    print(
                        f"[{done}/{total}] {case['case_id']} {cond} r{rep} {result.status} {result.latency_ms}ms"
                    )
    print(f"\n요약 → {out_csv}\n원시 → {out_jsonl}")
    return out_csv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
