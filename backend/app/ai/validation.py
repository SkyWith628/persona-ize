"""출력 검증 (design.md §3-5). 하나라도 실패하면 실패.

모델의 '의미 보존 성공' 자기 보고는 쓰지 않는다. 원문과 결과에 같은 추출기를 적용해 집합으로 비교한다.
"""

from __future__ import annotations

from app.ai.preserve import extract_preserved_items, facts_only, polarity
from app.domain.persona import PersonaSettings
from app.domain.rewrite import ModelOutput, PreservedItem, RetrievedExample, ValidationReport, Violation


def validate_output(
    draft_text: str,
    output: ModelOutput,
    preserved: list[PreservedItem],
    persona: PersonaSettings | None,
    retrieved: list[RetrievedExample],
) -> ValidationReport:
    """보존 항목 대조 → 추가된 사실 → 부정/가능성 방향 → 금지 표현 → 근거 검증."""
    violations: list[Violation] = []
    result = output.rewritten_text.strip()

    if not result:
        return ValidationReport(
            passed=False, violations=[Violation(check="empty", detail="결과가 비어 있습니다.")]
        )

    result_items = extract_preserved_items(result)
    result_facts = facts_only(result_items)
    source_facts = facts_only(preserved)

    # 1) 보존 항목 대조: 원문의 날짜·수량·사람은 결과에 그대로 있어야 한다.
    for kind, norm in sorted(source_facts - result_facts):
        label = next(i.text for i in preserved if (i.kind, i.normalized) == (kind, norm))
        violations.append(
            Violation(check="preserve_missing", detail=f"{kind} '{label}' 이(가) 결과에 없습니다.")
        )

    # 1-b) 약속 표현은 어떤 형태로든 남아 있어야 한다 (문체 변형은 허용).
    if any(i.kind == "promise" for i in preserved) and not any(i.kind == "promise" for i in result_items):
        violations.append(
            Violation(check="preserve_missing", detail="원문의 약속·의지 표현이 결과에 없습니다.")
        )

    # 2) 추가된 사실: 결과에만 있는 날짜·수량·사람. 설정된 호칭(address_term)은 말투 요소라 허용한다.
    allowed_names = set()
    if persona and persona.address_term:
        allowed_names = {
            i.normalized for i in extract_preserved_items(persona.address_term) if i.kind == "person"
        }
    for kind, norm in sorted(result_facts - source_facts):
        if kind == "person" and norm in allowed_names:
            continue
        label = next(i.text for i in result_items if (i.kind, i.normalized) == (kind, norm))
        violations.append(
            Violation(check="fact_added", detail=f"원문에 없는 {kind} '{label}' 이(가) 추가되었습니다.")
        )

    # 3) 가능/불가/부정 방향.
    src_pol, out_pol = polarity(preserved), polarity(result_items)
    if "불가" in src_pol and "불가" not in out_pol and "부정" not in out_pol:
        violations.append(
            Violation(check="negation_changed", detail="원문의 '불가/어려움' 표현이 결과에서 사라졌습니다.")
        )
    if "불가" not in src_pol and "부정" not in src_pol and "불가" in out_pol:
        violations.append(
            Violation(check="negation_changed", detail="원문에 없던 '불가/어려움' 표현이 생겼습니다.")
        )

    # 4) 금지 표현.
    if persona:
        for phrase in persona.forbidden_phrases:
            if phrase and phrase in result:
                violations.append(Violation(check="forbidden_phrase", detail=f"금지 표현 '{phrase}' 포함"))

    # 5) 근거: evidence_ids ⊆ 실제 검색 결과.
    known = {r.id for r in retrieved}
    for eid in output.evidence_ids:
        if eid not in known:
            violations.append(Violation(check="evidence_unknown", detail=f"검색되지 않은 근거 id '{eid}'"))

    return ValidationReport(passed=not violations, violations=violations)
