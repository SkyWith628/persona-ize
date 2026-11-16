"""평가 사례 × 조건을 백엔드 워크플로로 실행해 결과 CSV를 만든다.

사용 예:
    python evals/run_eval.py                                  # fake 프로바이더, A/B, dev+test
    python evals/run_eval.py --split dev --conditions A,B,C --repeat 3 --provider anthropic

입력 열은 docs/dataset-guide.md 기준 (case_id, relation, situation, original_text, ..., as_of, split).
relation → 상대 정보는 evals/characters.csv 에서 찾는다.

C/D 조건은 과거 대화가 필요하다. `--dataset`(기본 data/synthetic/sample_conversations.json)을 `--user`로
적재한 뒤(멱등) 그 사용자의 상대 id로 검색한다. D는 추가로 그래프 수집이 필요하다(NEO4J_URI, haiku 호출,
메시지당 10~15초; 한 번 넣으면 다음부터는 건너뛴다). 저장소는 .env의 DATABASE_URL(없으면 인메모리),
임베딩은 EMBEDDING_PROVIDER(local=e5-small, 첫 실행에 모델 다운로드; `--embedding fake`로 빠른 점검).
검색 기준 시각은 사례의 `as_of` 날짜 당일 끝(한국 시간)이다 — 그 날까지의 대화만 본다.

맥락 지표 (2026-11-16) — 그래프(D)가 문장이 아니라 사실 맥락·경고로 드러나서, 그쪽을 따로 센다
- `context_facts`·`context_msg_ids`: 프롬프트의 [사실 맥락]에 들어간 사실 문장과 그 출처 메시지.
- `context_recall`: `relevant_msg_ids` 중 사실 맥락의 출처가 덮은 비율 (`recall`은 문체 예시까지 합친 값).
- `context_hit`·`warning_hit`: 사례에 **선택 열** `expected_context`(`;`로 나눈 핵심어, 예: `파트장;존댓말`)가
  있으면 그 핵심어가 사실 맥락 / 경고 문장에 나온 비율. 열이 없거나 비어 있으면 빈칸.

설정을 바꿔 보는 실험 (2026-11-16, docs/graph-redesign-2026-11-16.md 6.2) — 새 조건 없이 설정만 바꾼다
- `--persona-override FILE`: 사람이 쓴 정답 설정을 사례의 설정에 덮어쓴다(오라클 = 상한). CSV 열은 `relation`
  또는 `contact`, `formality`, `address_term`(`-`는 호칭 없음), 선택 `as_of`·`friendliness`·`directness`·
  `length`·`emoji`·`notes`. `formality`가 비고 `speech_level`(반말/해요체/합쇼체/존댓말)이 있으면 격식으로
  바꿔 넣는다 — 정답 타임라인(`gold_personas.csv`)을 그대로 줄 수 있다. `as_of`가 있으면 사례 날짜 이전의
  가장 늦은 줄을 쓴다.
- `--apply-suggestion all|matching`: 규칙 말투 프로필의 제안(`sources={"register"}`)을 승인한 것처럼 사례의
  설정에 적용한다. `matching`은 `--gold` 정답과 맞는 항목만 적용한다. 평가 안에서만 적용하고 저장하지 않는다.
  이 플래그를 쓰면 A/B 조건에서도 과거 대화를 적재한다(제안이 메시지를 읽는다).
- `--invalidation trust|artifacts|ignore`: 그래프 검색의 무효화 처리(`GRAPH_INVALIDATION_MODE`)를 덮어쓴다.
- 새 열: `validator_version`(보존 규칙 버전), `persona_formality`·`persona_address_term`(실제로 쓴 설정),
  `suggestion_applied`(빈칸 = 안 씀 / `none` = 적용할 것이 없었음 / `formality=4;address_term=…` /
  정답 설정이면 `override:…`).

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
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "evals"))  # 같은 폴더의 suggestion_accuracy (테스트는 경로로 불러온다)

from suggestion_accuracy import (  # noqa: E402
    GoldRow,
    gold_for,
    load_gold,
    load_suggester,
    matching_fields,
    proposed_changes,
    read_table,
)

from app.ai.prompt import fit_examples  # noqa: E402
from app.api.deps import AppState, build_state  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.domain.contact import Contact  # noqa: E402
from app.domain.persona import PersonaSettings  # noqa: E402
from app.domain.rewrite import RewriteInput  # noqa: E402
from app.services.synthetic_loader import load_synthetic, persona_from_aliases  # noqa: E402

DEFAULT_CASES = ROOT / "evals" / "eval_cases.csv"
DEFAULT_CHARACTERS = ROOT / "evals" / "characters.csv"
DEFAULT_DATASET = ROOT / "data" / "synthetic" / "sample_conversations.json"
RESULTS_DIR = ROOT / "evals" / "results"
KST = timezone(timedelta(hours=9))
RETRIEVAL_CONDITIONS = ("C", "D")
# 정답 타임라인의 말투 단계 → 격식 (설계 5.1: 반말 1~2, 해요체 3, 합쇼체 4~5). 값은 (하한, 상한, 기본) —
# 기준 설정이 이미 그 범위 안이면 그대로 둔다(팀장 기본값 5를 합쇼체라고 4로 내리지 않는다).
SPEECH_LEVEL_FORMALITY: dict[str, tuple[int, int, int]] = {
    "반말": (1, 2, 1),
    "해체": (1, 2, 1),
    "해요체": (3, 3, 3),
    "합쇼체": (4, 5, 4),
    "존댓말": (3, 5, 3),
}
MAX_ADDRESS_TERM = 30  # PersonaSettings.address_term 길이 상한

# 관계 유형별 기본 페르소나는 app.services.synthetic_loader.DEFAULT_PERSONAS (시연 준비와 공유).


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
    p.add_argument("--prompt", default=None, help="PROMPT_VERSION 덮어쓰기 (예: rewrite-v2)")
    p.add_argument("--case", default=None, help="이 case_id들만 (쉼표 구분, 예: E003,E005)")
    p.add_argument("--embedding", choices=["local", "fake"], default=None, help="EMBEDDING_PROVIDER 덮어쓰기")
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="요약 CSV 경로 (기본 evals/results/eval-<시각>.csv)",
    )
    p.add_argument(
        "--apply-suggestion",
        choices=["off", "all", "matching"],
        default="off",
        help="규칙 말투 제안을 사례의 설정에 적용: all=전부 / matching=--gold 정답과 맞는 항목만",
    )
    p.add_argument("--gold", type=Path, default=None, help="정답 설정 타임라인 CSV (matching에 필요)")
    p.add_argument(
        "--persona-override",
        type=Path,
        default=None,
        help="정답 설정 CSV (relation 또는 contact, formality, address_term, …) — 오라클 조건",
    )
    p.add_argument(
        "--invalidation",
        choices=["trust", "artifacts", "ignore"],
        default=None,
        help="GRAPH_INVALIDATION_MODE 덮어쓰기 (그래프 검색의 무효화 처리)",
    )
    args = p.parse_args(argv)
    if args.apply_suggestion == "matching" and args.gold is None:
        p.error("--apply-suggestion matching에는 --gold가 필요합니다")
    if args.apply_suggestion != "off" and args.persona_override is not None:
        p.error("--apply-suggestion과 --persona-override는 함께 쓰지 않습니다 (조건을 나눠 돌린다)")
    if args.persona_override is not None and not args.persona_override.is_file():
        p.error(f"--persona-override 파일이 없습니다: {args.persona_override}")
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
    """관계 유형 기본값 + 호칭 (규칙은 synthetic_loader.persona_from_aliases — 시연 준비와 같다)."""
    aliases = [a.strip() for a in character.get("aliases", "").split(";") if a.strip()]
    return persona_from_aliases(character["relationship_type"], aliases)


def load_overrides(path: Path) -> list[dict[str, str]]:
    rows = read_table(path)  # `#` 설명 줄은 건너뛴다
    if not rows:
        raise SystemExit(f"정답 설정이 없습니다: {path}")
    if not any("relation" in r or "contact" in r for r in rows):
        raise SystemExit(f"정답 설정에는 relation 또는 contact 열이 있어야 합니다: {path}")
    return rows


def override_for(
    rows: list[dict[str, str]], character: dict[str, str], as_of: str = ""
) -> dict[str, str] | None:
    """이 등장인물·시점에 쓸 정답 설정 줄. 없으면 None(기준 설정 그대로).

    `relation`(팀장/동기) 또는 `contact`(이름)로 찾는다. 줄에 `as_of`가 있으면 사례 날짜 이전의 가장 늦은
    줄을 고른다 — 관계가 바뀌는 상대는 시점마다 정답이 다르다. `as_of`가 빈 줄은 언제나 해당하고 날짜 있는
    줄에 밀린다. 사례의 날짜가 비면(지금) 가장 늦은 줄.
    """
    day = as_of[:10]
    mine = [
        r
        for r in rows
        if (r.get("relation") and r.get("relation") == character.get("relation"))
        or (r.get("contact") and r.get("contact") == character.get("name"))
    ]
    eligible = [r for r in mine if not day or not r.get("as_of") or r["as_of"][:10] <= day]
    return max(eligible, key=lambda r: r.get("as_of", "")[:10]) if eligible else None


def apply_override(persona: PersonaSettings, row: dict[str, str]) -> tuple[PersonaSettings, list[str]]:
    """정답 설정 줄을 덮어쓴 사본과 바뀐 항목(`formality=4`). 빈칸은 기준 설정 그대로다.

    `address_term`의 `-`는 호칭 없음. 값이 범위를 벗어나면(격식 6 등) 설정 검증이 오류를 낸다.
    """
    updates: dict[str, Any] = {}
    if row.get("formality"):
        updates["formality"] = int(row["formality"])
    elif row.get("speech_level") in SPEECH_LEVEL_FORMALITY:
        low, high, default = SPEECH_LEVEL_FORMALITY[row["speech_level"]]
        updates["formality"] = persona.formality if low <= persona.formality <= high else default
    address = row.get("address_term", "")
    if address == "-":
        updates["address_term"] = None
    elif address:
        updates["address_term"] = address
    for name in ("friendliness", "directness"):
        if row.get(name):
            updates[name] = int(row[name])
    for name in ("length", "emoji", "notes"):
        if row.get(name):
            updates[name] = row[name]
    return _with_updates(persona, updates)


def apply_suggestion(
    persona: PersonaSettings, suggestion: Any, *, mode: str = "all", gold: GoldRow | None = None
) -> tuple[PersonaSettings, list[str]]:
    """규칙 말투 제안을 승인한 것처럼 적용한 사본과 바뀐 항목. 격식과 호칭만 건드린다.

    `mode="matching"`이면 정답(`gold`)과 맞는 항목만 적용한다(정답 줄이 없으면 아무것도 안 한다).
    원본도 저장소도 바꾸지 않는다 — 제품에서는 사용자가 승인해야만 설정이 바뀐다.
    """
    proposed = proposed_changes(suggestion, "register")
    if len(str(proposed.get("address_term", ""))) > MAX_ADDRESS_TERM:
        del proposed["address_term"]
    if mode == "matching":
        ok = matching_fields(proposed, gold, persona.formality, persona.address_term)
        proposed = {k: v for k, v in proposed.items() if k in ok}
    return _with_updates(persona, proposed)


def _with_updates(persona: PersonaSettings, updates: dict[str, Any]) -> tuple[PersonaSettings, list[str]]:
    changed = {k: v for k, v in updates.items() if getattr(persona, k) != v}
    if not changed:
        return persona, []
    # model_copy는 검증하지 않는다 — 범위를 벗어난 값이 프롬프트까지 가지 않게 다시 검증한다
    updated = PersonaSettings.model_validate({**persona.model_dump(), **changed})
    return updated, [f"{k}={'' if v is None else v}" for k, v in changed.items()]


def parse_as_of(raw: str) -> datetime | None:
    """사례의 as_of(YYYY-MM-DD) → 그날 끝(KST). 비어 있으면 None(지금)."""
    if not raw:
        return None
    day = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=KST)
    return day + timedelta(days=1)


def parse_keywords(raw: str) -> list[str]:
    return [x.strip() for x in raw.replace(",", ";").split(";") if x.strip()]


def keyword_hit(keywords: list[str], text: str) -> str:
    """핵심어 가운데 본문에 나온 비율 (`0.50`). 핵심어가 없으면 빈칸."""
    if not keywords:
        return ""
    return f"{sum(1 for k in keywords if k in text) / len(keywords):.2f}"


def parse_ids(raw: str) -> set[str]:
    return {x.strip() for x in raw.split(";") if x.strip()}


async def prepare_contacts(
    state: AppState, args: argparse.Namespace, characters: dict[str, dict[str, str]]
) -> dict[str, Contact]:
    """relation → Contact. 검색 조건이 있으면 데이터셋을 적재하고 저장소의 상대를 쓴다."""
    # 제안 적용은 과거 메시지를 읽으므로 A/B만 돌려도 데이터셋을 적재한다 (플래그가 꺼져 있으면 예전과 같다).
    wants_suggestion = getattr(args, "apply_suggestion", "off") != "off"
    if wants_suggestion or any(c in RETRIEVAL_CONDITIONS for c in args.condition_list):
        need_graph = "D" in args.condition_list
        if need_graph and not state.graph_enabled:
            raise SystemExit(
                "D 조건은 그래프가 필요합니다. .env의 NEO4J_URI를 채우세요 (로컬 compose: bolt://localhost:7687)."
            )
        report = await load_synthetic(state, args.dataset, args.user, args.characters, graph=need_graph)
        if report.failed:
            raise SystemExit("과거 대화 적재 실패: " + "; ".join(report.failed))
        print(
            f"과거 대화 적재: user={args.user} storage={state.storage} embedder={state.embedder.version} "
            f"상대 {len(report.contacts)} 대화방 {len(report.conversations)} "
            f"새 메시지 {report.imported} 중복 {report.duplicates} 묶음 {report.episodes}"
            + (f" 그래프 에피소드 새로 {report.graph_episodes}" if need_graph else "")
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
    if args.prompt:
        overrides["prompt_version"] = args.prompt
    if args.embedding:
        overrides["embedding_provider"] = args.embedding
    if args.invalidation:
        overrides["graph_invalidation_mode"] = args.invalidation
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
    if args.case:
        wanted = {c.strip() for c in args.case.split(",") if c.strip()}
        cases = [c for c in cases if c["case_id"] in wanted]
        if not cases:
            raise SystemExit(f"--case에 맞는 사례가 없습니다: {sorted(wanted)}")
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
        "warnings",
        "first_attempt_violations",
        "context_facts",
        "context_msg_ids",
        "context_recall",
        "context_hit",
        "warning_hit",
        "validator_version",
        "persona_formality",
        "persona_address_term",
        "suggestion_applied",
    ]
    apply_mode = getattr(args, "apply_suggestion", "off")
    override_path = getattr(args, "persona_override", None)
    override_rows = load_overrides(override_path) if override_path else []
    gold_path = getattr(args, "gold", None)
    gold = load_gold(gold_path) if gold_path and apply_mode == "matching" else []
    suggest = load_suggester() if apply_mode != "off" else None
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
            expected_context = parse_keywords(case.get("expected_context") or "")
            # 설정 실험: 정답 설정 덮어쓰기 또는 규칙 제안 적용. 둘 다 꺼져 있으면 persona는 예전 그대로다.
            suggestion_applied = ""
            changed: list[str] = []
            if override_rows:
                row_override = override_for(override_rows, character, case.get("as_of", ""))
                if row_override is not None:
                    persona, changed = apply_override(persona, row_override)
                suggestion_applied = "override:" + (";".join(changed) or "none")
            elif suggest is not None:
                suggestion = await suggest(
                    state, args.user, contact, as_of=as_of, sources=frozenset({"register"}), base=persona
                )
                gold_row = gold_for(gold, (character["name"], case["relation"]), case.get("as_of", ""))
                persona, changed = apply_suggestion(persona, suggestion, mode=apply_mode, gold=gold_row)
                suggestion_applied = ";".join(changed) or "none"
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
                    context = [ex for ex in fit_examples(retrieved) if ex.purpose == "context"]
                    context_ids = sorted({mid for ex in context for mid in ex.source_message_ids})
                    context_text = " | ".join(ex.text for ex in context)
                    context_recall = (
                        f"{len(relevant & set(context_ids)) / len(relevant):.2f}"
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
                        "warnings": " | ".join(result.warnings),
                        # 재생성이 있었을 때 1차가 왜 걸렸는지 (최종 validation에는 안 남는다)
                        "first_attempt_violations": " | ".join(
                            v.detail for v in result.attempt_log[0].violations
                        )
                        if result.attempt_log
                        else "",
                        "context_facts": context_text,
                        "context_msg_ids": ";".join(context_ids),
                        "context_recall": context_recall,
                        "context_hit": keyword_hit(expected_context, context_text),
                        "warning_hit": keyword_hit(expected_context, " | ".join(result.warnings)),
                        "validator_version": getattr(result, "validator_version", ""),
                        "persona_formality": persona.formality,
                        "persona_address_term": persona.address_term or "",
                        "suggestion_applied": suggestion_applied,
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
