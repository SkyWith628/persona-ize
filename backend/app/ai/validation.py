"""출력 검증 (design.md §3-5). 하나라도 실패하면 실패.

모델의 '의미 보존 성공' 자기 보고는 쓰지 않는다. 원문과 결과에 같은 추출기를 적용해 집합으로 비교한다.
"""

from __future__ import annotations

import re

from app.ai.preserve import extract_preserved_items, facts_only, polarity
from app.domain.persona import PersonaSettings
from app.domain.rewrite import ModelOutput, PreservedItem, RetrievedExample, ValidationReport, Violation
from app.domain.titles import ORG_TITLES, title_of

# 친족 호칭(부르는 말). 이름 없는 호칭은 말투 요소지만, 친족 호칭은 방향이 틀리기 쉬워 설정과 대조한다
# (11-09 repeat3: 동생에게 '형'을 붙임 — 그래프 사실 "형 또 그랬냐"를 호칭 힌트로 오해).
_KINSHIP_VOCATIVE = re.compile(r"(?<![가-힣])(형님|누님|형|누나|오빠|언니|엄마|아빠)(?=[,\s!?~]|$)")


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

    # 2-b) 친족 호칭: 원문에 없던 형·누나·오빠·언니… 는 설정된 호칭과 같을 때만 허용한다.
    src_kin = set(_KINSHIP_VOCATIVE.findall(draft_text))
    out_kin = set(_KINSHIP_VOCATIVE.findall(result))
    allowed_kin = (
        set(_KINSHIP_VOCATIVE.findall(persona.address_term)) if persona and persona.address_term else set()
    )
    for term in sorted(out_kin - src_kin - allowed_kin):
        violations.append(
            Violation(
                check="fact_added",
                detail=f"원문에 없는 호칭 '{term}'이(가) 추가되었습니다 (설정된 호칭이 아님).",
            )
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


# ── 호칭 누출 표시 (graph-redesign 5.5) ──────────────────────────────────────
# 검증 실패가 아니라 사용자에게 보여 줄 표시다. 이름 없는 직함('파트장님')은 말투 요소라 사실 검증을
# 통과하는데, 초안에도 저장된 설정에도 없던 호칭이 결과에 생겼다면 예시나 과거 대화에서 새어 든 것이다
# (호칭은 저장된 설정만 공급한다). 사람이 보내기 전에 한 번 보게 한다.

_TITLE_ALT = "|".join(
    sorted((t for t in ORG_TITLES | {"선배"} if re.fullmatch("[가-힣]+", t)), key=lambda t: (-len(t), t))
)
# 직함+님. 앞에 붙여 쓴 성·이름('김팀장님')은 3자까지 함께 잡는다. 견줄 때는 직함만 본다.
_TITLE_ADDRESS = re.compile(rf"(?<![가-힣])[가-힣]{{0,3}}?({_TITLE_ALT})님")
# '님' 없는 직함은 부르는 자리(쉼표·느낌표·물결표 앞)에 있을 때만 호칭으로 본다 ('김 대리, 이거 봐줘').
_TITLE_VOCATIVE = re.compile(rf"(?<![가-힣])[가-힣]{{0,3}}?({_TITLE_ALT})(?=[,!~])")
# 이름·명사 + 님/씨가 부르는 자리에 있을 때 ('서연 씨,', '고객님!'). 문장 속의 '날씨'·'솜씨'는 잡지 않는다.
_HONORIFIC_VOCATIVE = re.compile(r"(?<![가-힣])([가-힣]{2,4}) ?(?:님|씨)(?=[,!~])")


def _alias_names(contact_alias: str) -> list[str]:
    """상대 별칭에서 부르는 이름 후보. '이서연' → ['이서연', '서연']. 직함이 섞인 별칭은 쓰지 않는다."""
    alias = contact_alias.strip()
    if not re.fullmatch("[가-힣]{2,4}", alias) or title_of(alias) is not None:
        return []
    return [alias, alias[1:]] if len(alias) == 3 else [alias]


def address_leaks(
    draft_text: str,
    output_text: str,
    persona: PersonaSettings | None,
    contact_alias: str = "",
) -> list[str]:
    """결과에만 있는 직함·호격을 나온 글자 그대로, 나온 순서로 돌려준다. 없으면 빈 목록.

    초안에 있거나 저장된 호칭(`persona.address_term`)에 들어 있는 것은 누출이 아니다. 견주는 것은
    알맹이(직함, 이름, 친족 호칭)다 — 초안의 '파트장이'가 결과에서 '파트장님께서'가 되거나 '서연아'가
    '서연 씨'가 된 것은 말투를 바꾼 것이지 새 호칭이 아니다.
    `contact_alias`를 주면 상대 이름을 부른 것('서연아', '서연,')도 찾는다. 이름 목록 없이 '-아/-야'만으로는
    '괜찮아,' 같은 말과 구별할 수 없어서 별칭이 있을 때만 본다.
    순수 함수다. 모델도 저장소도 부르지 않는다.
    """
    known = (
        re.sub(r"\s+", "", draft_text)
        + "\n"
        + re.sub(r"\s+", "", (persona.address_term or "") if persona else "")
    )
    found: list[tuple[int, str, str]] = []  # (위치, 나온 글자, 알맹이)
    for pattern in (_TITLE_ADDRESS, _TITLE_VOCATIVE, _HONORIFIC_VOCATIVE, _KINSHIP_VOCATIVE):
        found += [(m.start(), m.group(0), m.group(1)) for m in pattern.finditer(output_text)]
    for name in _alias_names(contact_alias):
        # '서연아 ' / '서연아,' / '서연,' — 조사 '이'가 붙은 '서연이 그랬어'는 주어라서 뺀다.
        vocative = re.compile(rf"(?<![가-힣]){re.escape(name)}(?:(?:아|야)(?=[\s,!~?]|$)|(?=[,!~]))")
        found += [(m.start(), m.group(0), name) for m in vocative.finditer(output_text)]
    leaks: list[str] = []
    for _, surface, core in sorted(found):
        if core not in known and surface not in leaks:
            leaks.append(surface)
    return leaks
