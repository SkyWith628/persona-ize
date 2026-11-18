"""같은 초안을 여러 상대에게 돌려, 워크플로의 층마다 두 상대의 값이 얼마나 겹치는지 숫자로 낸다.

"상대가 달라도 결과 문장이 비슷하다"는 문제가 어느 층에서 생기는지 좁히는 도구다 (AI Chains, CHI 2022의
"단계 하나씩 떼어 보기"). 층은 셋이다.

- `style_similarity`: 프롬프트에 들어간 문체 예시 글끼리의 글자 유사도(0~1). 한쪽이라도 예시가 없으면 빈칸.
  예시는 상대마다 따로라 id는 겹칠 수 없다 — 그래서 글이 얼마나 닮았는지를 잰다.
- `prompt_similarity`: 사용자 프롬프트의 글자 유사도(0~1). 초안은 두 상대가 같으므로 빼고 잰다.
- `result_similarity`: 결과 문장의 글자 유사도. `--generate`를 줄 때만 잰다(모델을 부른다).

읽는 법: 프롬프트 유사도는 낮은데 결과 유사도가 높으면 재료는 다른데 모델이 비슷하게 쓴 것이다(지시문을
본다). 프롬프트 유사도부터 높으면 검색·설정·지문 쪽이 상대를 가르지 못한 것이다.

사용 예:
    python evals/layer_diff.py --embedding fake                          # 모델 호출 없음, 조건 C
    python evals/layer_diff.py --conditions B,C --voice --prompt rewrite-v5
    python evals/layer_diff.py --voice --prompt rewrite-v5 --generate --provider anthropic --case E001,E004

`--generate` 없이는 모델을 부르지 않는다. `--generate --provider anthropic`은 사례 × 조건 × 상대 수만큼
호출한다(비용). 글자 유사도는 거칠다 — 호칭 하나만 달라도 숫자는 거의 같다. 블라인드 평가를 대신하지
못하고 어디를 볼지 좁히는 데만 쓴다.

결과: evals/results/layer-diff-<시각>.csv — 사례 id, 조건, 상대 쌍, 숫자뿐이다. 초안·프롬프트·대화 원문은
파일에 남기지 않는다. D 조건은 그래프가 필요하다(run_eval.py와 같다).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import datetime
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "evals"))

import run_eval  # noqa: E402

from app.api.deps import AppState, build_state  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.domain.contact import Contact  # noqa: E402
from app.domain.persona import PersonaSettings  # noqa: E402
from app.domain.rewrite import RewriteInput  # noqa: E402

FIELDS = [
    "case_id",
    "condition",
    "relation_a",
    "relation_b",
    "style_count_a",
    "style_count_b",
    "style_similarity",
    "prompt_similarity",
    "result_similarity",
    "voice_a",
    "voice_b",
    "prompt_version",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="같은 초안, 다른 상대: 층별 겹침")
    p.add_argument("--cases", type=Path, default=run_eval.DEFAULT_CASES)
    p.add_argument("--characters", type=Path, default=run_eval.DEFAULT_CHARACTERS)
    p.add_argument("--dataset", type=Path, default=run_eval.DEFAULT_DATASET)
    p.add_argument("--user", default="eval", help="과거 대화를 적재·검색할 사용자 id")
    p.add_argument("--conditions", default="C", help="쉼표 구분: A,B,C,D")
    p.add_argument("--split", choices=["dev", "test", "all"], default="all")
    p.add_argument("--case", default=None, help="이 case_id들만 (쉼표 구분)")
    p.add_argument("--prompt", default=None, help="PROMPT_VERSION 덮어쓰기 (말투 지문은 rewrite-v5부터)")
    p.add_argument("--embedding", choices=["local", "fake"], default=None)
    p.add_argument("--voice", action="store_true", help="C·D 입력에 말투 지문을 채운다")
    p.add_argument("--generate", action="store_true", help="결과 문장까지 만들어 견준다 (모델 호출)")
    p.add_argument("--provider", choices=["fake", "anthropic"], default="fake")
    p.add_argument("--model", default=None, help="LLM_MODEL 덮어쓰기")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)
    args.condition_list = [c.strip().upper() for c in args.conditions.split(",") if c.strip()]
    bad = [c for c in args.condition_list if c not in ("A", "B", "C", "D")]
    if bad:
        p.error(f"알 수 없는 조건: {bad}")
    if args.provider == "anthropic" and not args.generate:
        p.error("--provider는 --generate와 함께 씁니다 (없으면 모델을 부르지 않는다)")
    args.load_dataset = True  # 지문과 문체 예시가 과거 대화를 읽는다
    return args


async def layers(
    workflow: Any, inp: RewriteInput, contact: Contact, persona: PersonaSettings | None
) -> dict[str, Any]:
    """생성 앞의 세 단계(analyze → retrieve → compose)만 돌린 중간 결과. 모델을 부르지 않는다.

    워크플로의 단계 함수를 직접 부른다 — `workflow.py`의 단계 구성이 바뀌면 여기도 고친다.
    """
    state: dict[str, Any] = {"inp": inp, "contact": contact, "persona": persona, "started": 0.0}
    for step in (workflow._analyze, workflow._retrieve, workflow._compose):  # noqa: SLF001
        state |= await step(state)
    return {
        "style": [ex.text for ex in state["retrieved"] if ex.purpose == "style"],
        "prompt": state["prompt"].user.replace(inp.draft_text, ""),  # 초안은 두 상대가 같다
    }


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def overlap(a: dict[str, Any], b: dict[str, Any]) -> tuple[float | None, float]:
    """(문체 예시 글의 유사도, 프롬프트 글자 유사도). 한쪽이라도 예시가 없으면 앞의 값은 None."""
    both = a["style"] and b["style"]
    style = similarity("\n".join(a["style"]), "\n".join(b["style"])) if both else None
    return style, similarity(a["prompt"], b["prompt"])


def _num(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


async def run(args: argparse.Namespace) -> Path:
    overrides: dict[str, str] = {"llm_provider": args.provider}
    for key, value in (
        ("llm_model", args.model),
        ("prompt_version", args.prompt),
        ("embedding_provider", args.embedding),
    ):
        if value:
            overrides[key] = value
    state = build_state(Settings(**overrides))  # type: ignore[arg-type]  # .env도 읽는다
    try:
        return await run_with_state(state, args)
    finally:
        await state.close()


async def run_with_state(state: AppState, args: argparse.Namespace) -> Path:
    workflow = state.workflow
    characters = run_eval.load_characters(args.characters)
    cases = run_eval.load_cases(args.cases, args.split)
    if args.case:
        wanted = {c.strip() for c in args.case.split(",") if c.strip()}
        cases = [c for c in cases if c["case_id"] in wanted]
        if not cases:
            raise SystemExit(f"--case에 맞는 사례가 없습니다: {sorted(wanted)}")
    if len(characters) < 2:  # noqa: PLR2004
        raise SystemExit("상대가 둘 이상 있어야 견줄 수 있습니다 (characters.csv)")
    contacts = await run_eval.prepare_contacts(state, args, characters)
    personas = {relation: run_eval.build_persona(ch) for relation, ch in characters.items()}

    run_eval.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = args.out or run_eval.RESULTS_DIR / f"layer-diff-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    sums: dict[str, dict[str, list[float]]] = {}
    with out.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=FIELDS)
        writer.writeheader()
        for case in cases:
            as_of = run_eval.parse_as_of(case.get("as_of", ""))
            voices = {
                relation: await run_eval.voice_block_for(state, args.user, contact, as_of)
                if args.voice
                else None
                for relation, contact in contacts.items()
            }
            for cond in args.condition_list:
                per: dict[str, dict[str, Any]] = {}
                for relation, contact in contacts.items():
                    with_history = cond in run_eval.RETRIEVAL_CONDITIONS
                    inp = RewriteInput(
                        user_id=args.user,
                        contact_id=contact.id,
                        draft_text=case["original_text"],
                        draft_version=1,
                        situation=case.get("situation", ""),
                        condition=cond,  # type: ignore[arg-type]
                        as_of=as_of,
                        voice_block=voices[relation] if with_history else None,
                    )
                    layer = await layers(workflow, inp, contact, personas[relation])
                    layer["voice"] = bool(inp.voice_block)
                    if args.generate:
                        layer["result"] = (await workflow.run(inp, contact, personas[relation])).result_text
                    per[relation] = layer
                for a, b in combinations(per, 2):
                    ids, prompt_sim = overlap(per[a], per[b])
                    result_sim = similarity(per[a]["result"], per[b]["result"]) if args.generate else None
                    writer.writerow(
                        {
                            "case_id": case["case_id"],
                            "condition": cond,
                            "relation_a": a,
                            "relation_b": b,
                            "style_count_a": len(per[a]["style"]),
                            "style_count_b": len(per[b]["style"]),
                            "style_similarity": _num(ids),
                            "prompt_similarity": _num(prompt_sim),
                            "result_similarity": _num(result_sim),
                            "voice_a": per[a]["voice"],
                            "voice_b": per[b]["voice"],
                            "prompt_version": workflow.prompt_version,
                        }
                    )
                    bucket = sums.setdefault(cond, {"style": [], "prompt": [], "result": []})
                    for name, value in (("style", ids), ("prompt", prompt_sim), ("result", result_sim)):
                        if value is not None:
                            bucket[name].append(value)
    for cond, bucket in sums.items():
        mean = {k: (f"{sum(v) / len(v):.2f}" if v else "-") for k, v in bucket.items()}
        print(
            f"{cond}: 쌍 {len(bucket['prompt'])}  문체 예시 유사도 {mean['style']}  "
            f"프롬프트 유사도 {mean['prompt']}  결과 유사도 {mean['result']}"
        )
    print(f"\n결과 → {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    asyncio.run(run(parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
