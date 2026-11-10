"""프롬프트 조립 (design.md §3-3). 순서: 시스템 규칙 → 현재 설정 → 원문·보존 항목 → 검색 예시.

프롬프트 본문은 `app/prompts/<version>/*.md`에 둬서 비전공자가 문서처럼 수정할 수 있게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from app.core.config import PROMPTS_DIR
from app.domain.contact import Contact
from app.domain.persona import PersonaSettings
from app.domain.rewrite import Intent, PreservedItem, RetrievedExample, Violation

_RELATION_KO = {
    "superior": "상사",
    "colleague": "동료",
    "subordinate": "후배",
    "friend": "친구",
    "family": "가족",
    "partner": "연인",
    "acquaintance": "지인·거래처",
}
_LENGTH_KO = {"shorter": "원문보다 짧게", "same": "원문과 비슷하게", "longer": "원문보다 조금 길게"}
_EMOJI_KO = {"none": "없음", "rare": "가끔", "often": "자주"}
_WEEKDAY_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")
KST = timezone(timedelta(hours=9))


def today_kst(as_of: datetime | None = None) -> date:
    """메시지를 쓰는 날(한국 날짜). 평가 사례는 `as_of`, 실제 요청은 지금."""
    moment = as_of or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(KST).date()


def _today_line(today: date) -> str:
    return f"{today.isoformat()} ({_WEEKDAY_KO[today.weekday()]})"


@dataclass(frozen=True)
class PromptBundle:
    system: str
    user: str
    version: str


@lru_cache
def _load(version: str, name: str) -> str:
    path = PROMPTS_DIR / version / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"프롬프트 파일이 없습니다: {path}")
    return path.read_text(encoding="utf-8")


def _persona_block(contact: Contact, persona: PersonaSettings | None) -> str:
    if persona is None:
        # A 조건: 일반 첨삭. 상대 정보도 숨겨 기준선을 깨끗하게 유지한다.
        return "상대 정보 없음. 일반적으로 자연스럽고 정중한 한국어로 다듬는다."
    lines = [
        f"상대: {contact.alias} ({_RELATION_KO.get(contact.relationship_type, contact.relationship_type)})",
        f"격식 {persona.formality}/5 · 친근함 {persona.friendliness}/5 · 직접성 {persona.directness}/5",
        f"길이: {_LENGTH_KO[persona.length]} · 이모지: {_EMOJI_KO[persona.emoji]}",
    ]
    if persona.address_term:
        lines.append(f"호칭: {persona.address_term}")
    if persona.preferred_phrases:
        lines.append("선호 표현: " + ", ".join(persona.preferred_phrases))
    if persona.forbidden_phrases:
        lines.append("금지 표현: " + ", ".join(persona.forbidden_phrases))
    if persona.notes:
        lines.append(f"메모: {persona.notes}")
    return "\n".join(lines)


def _preserved_block(items: list[PreservedItem]) -> str:
    if not items:
        return "(추출된 항목 없음 — 그래도 원문의 모든 사실을 유지한다)"
    return "\n".join(f"- {i.kind}: {i.text}" for i in items)


def _examples_block(examples: list[RetrievedExample], purpose: str) -> str:
    picked = [e for e in examples if e.purpose == purpose]
    if not picked:
        return "(없음)"
    out = []
    for e in picked:
        when = e.sent_at.strftime("%m/%d") if e.sent_at else "-"
        out.append(f'- id={e.id} ({when}) "{e.text}"')
    return "\n".join(out)


def build_prompt(
    *,
    version: str,
    contact: Contact,
    persona: PersonaSettings | None,
    intent: Intent,
    preserved: list[PreservedItem],
    draft_text: str,
    situation: str,
    retrieved: list[RetrievedExample],
    today: date | None = None,
) -> PromptBundle:
    """첫 생성용 프롬프트.

    `today`는 v3부터 쓰는 `{today}` 자리표시자를 채운다.
    v1·v2의 user.md에는 그 자리가 없어 본문이 달라지지 않는다.
    """
    user = _load(version, "user").format(
        today=_today_line(today or today_kst()),
        persona_block=_persona_block(contact, persona),
        situation=situation.strip() or "(설명 없음)",
        intent=intent,
        preserved_block=_preserved_block(preserved),
        draft_text=draft_text,
        style_block=_examples_block(retrieved, "style"),
        context_block=_examples_block(retrieved, "context"),
    )
    return PromptBundle(system=_load(version, "system"), user=user, version=version)


def build_regenerate_suffix(version: str, previous_text: str, violations: list[Violation]) -> str:
    """재생성 1회에 덧붙일 피드백 (Self-Refine 형태)."""
    return _load(version, "regenerate").format(
        previous_text=previous_text,
        violations_block="\n".join(f"- {v.detail}" for v in violations),
    )


def available_versions() -> list[str]:
    return sorted(p.name for p in Path(PROMPTS_DIR).iterdir() if p.is_dir())
