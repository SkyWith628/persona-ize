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
from app.domain.conversation import local_time
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
# 이번 요청만의 조정 (RewriteInput.adjust). 설정 수치도 함께 바뀌지만(`apply_adjust`),
# 한 줄로도 분명히 알린다.
_ADJUST_KO = {
    "shorter": "지금보다 짧게 쓴다 (사실은 빼지 않는다)",
    "longer": "조금 더 길게 쓴다 (새 사실은 넣지 않는다)",
    "more_formal": "조금 더 격식 있게 쓴다",
    "more_casual": "조금 더 편한 말투로 쓴다",
}
_WEEKDAY_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")
KST = timezone(timedelta(hours=9))

# 검색 예시가 프롬프트에서 차지할 수 있는 글자 수 (설계 예산: 입력 6,000토큰 안쪽 — project-design §5.3).
# 보통 자료(예시 4개, 각 수십~수백 자)는 닿지 않는다.
EXAMPLE_MAX_CHARS = 600
BLOCK_MAX_CHARS = {"style": 2400, "context": 1200}
TRUNCATED_MARK = " …(뒤 생략)"
PHRASE_MAX_CHARS = 40  # [설정] 블록에 넣는 선호·금지 표현 하나의 길이


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


def _clip(phrase: str) -> str:
    """표현 하나가 블록을 채우지 않게 앞부분만 쓴다 (저장되는 표현에는 길이 제한이 없다)."""
    return phrase if len(phrase) <= PHRASE_MAX_CHARS else phrase[:PHRASE_MAX_CHARS] + "…"


def _persona_block(
    contact: Contact,
    persona: PersonaSettings | None,
    *,
    contact_known: bool = False,
    adjust: str | None = None,
) -> str:
    """[설정] 블록. `adjust`가 없고 설정이 있거나 A 조건이면 예전과 같은 글자 그대로다 (평가 재현성)."""
    relation = _RELATION_KO.get(contact.relationship_type, contact.relationship_type)
    if persona is None and not contact_known:
        # A 조건: 일반 첨삭. 상대 정보도 숨겨 기준선을 깨끗하게 유지한다.
        lines = ["상대 정보 없음. 일반적으로 자연스럽고 정중한 한국어로 다듬는다."]
    elif persona is None:
        # 저장된 설정 없이 과거 표현을 쓰는 경우 (조건 auto가 C·D를 골랐는데 페르소나가 없을 때).
        lines = [
            f"상대: {contact.alias} ({relation})",
            "저장된 말투 설정 없음. 문체 예시가 있으면 그 말투를 따른다.",
        ]
    else:
        lines = [
            f"상대: {contact.alias} ({relation})",
            f"격식 {persona.formality}/5 · 친근함 {persona.friendliness}/5 · 직접성 {persona.directness}/5",
            f"길이: {_LENGTH_KO[persona.length]} · 이모지: {_EMOJI_KO[persona.emoji]}",
        ]
        if persona.address_term:
            lines.append(f"호칭: {persona.address_term}")
        if persona.preferred_phrases:
            lines.append("선호 표현: " + ", ".join(_clip(p) for p in persona.preferred_phrases))
        if persona.forbidden_phrases:
            lines.append("금지 표현: " + ", ".join(_clip(p) for p in persona.forbidden_phrases))
        if persona.notes:
            lines.append(f"메모: {persona.notes}")
    if adjust:
        lines.append(f"이번 요청: {_ADJUST_KO[adjust]}")
    return "\n".join(lines)


def _register_block(block: str | None) -> str:
    """[관계 상태] 블록 (rewrite-v4부터). 서비스가 만들어 준 글자 그대로 넣고, 없으면 '(없음)'.

    블록의 내용(확정된 말투 단계와 건수, 위계 힌트)은 `app.ai.register_block`이 정한다.
    여기서는 끼워 넣기만 한다.
    """
    return block.strip() if block and block.strip() else "(없음)"


def _preserved_block(items: list[PreservedItem]) -> str:
    if not items:
        return "(추출된 항목 없음 — 그래도 원문의 모든 사실을 유지한다)"
    return "\n".join(f"- {i.kind}: {i.text}{_PRESERVED_NOTE.get(i.kind, '')}" for i in items)


# 약속은 글자가 아니라 뜻을 지킨다. 설명이 없으면 모델이 '드릴게요'를 글자 그대로 남겨 격식 설정을 못 맞추고
# "보존 항목이라 원문대로 두었습니다"라는 주의를 붙일 때가 있다 (2026-11-24 시연 장면 점검).
_PRESERVED_NOTE = {"promise": " (약속한다는 뜻을 유지한다 — 어미와 격식은 설정에 맞게 바꾼다)"}


def fit_examples(examples: list[RetrievedExample]) -> list[RetrievedExample]:
    """프롬프트에 넣을 만큼만 남긴다: 예시 하나는 `EXAMPLE_MAX_CHARS`자, 용도별 합계는 `BLOCK_MAX_CHARS`자.

    상한에 걸리지 않는 보통 자료에서는 받은 목록 그대로다(프롬프트가 글자까지 같다). 긴 예시는 앞부분만
    남기고 표시를 붙이며, 합계를 넘기는 뒤쪽 예시는 뺀다 — 검색기가 준 순서(가까운 것 먼저)를 믿는다.
    내 연속 발화가 아주 긴 대화(단체방에서 다른 사람을 건너뛴 경우 등)에서 예시 하나가 수십만 자가 되어
    첨삭 한 번이 하루 토큰 한도를 넘던 것을 막는다 (2026-11-11 검토).
    """
    out: list[RetrievedExample] = []
    used: dict[str, int] = {}
    for e in examples:
        text = e.text
        if len(text) > EXAMPLE_MAX_CHARS:
            text = text[:EXAMPLE_MAX_CHARS].rstrip() + TRUNCATED_MARK
        if used.get(e.purpose, 0) + len(text) > BLOCK_MAX_CHARS.get(e.purpose, 0):
            continue
        used[e.purpose] = used.get(e.purpose, 0) + len(text)
        out.append(e if text is e.text else e.model_copy(update={"text": text}))
    return out


def _examples_block(examples: list[RetrievedExample], purpose: str) -> str:
    picked = [e for e in examples if e.purpose == purpose]
    if not picked:
        return "(없음)"
    out = []
    for e in picked:
        # 날짜는 한국 시간으로 읽는다 (PostgreSQL은 UTC로 돌려준다 — 08:01의 메시지가 전날로 찍히지 않게).
        when = local_time(e.sent_at).strftime("%m/%d") if e.sent_at else "-"
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
    adjust: str | None = None,
    contact_known: bool = False,
    register_block: str | None = None,
    voice_block: str | None = None,
) -> PromptBundle:
    """첫 생성용 프롬프트.

    `today`는 v3부터 쓰는 `{today}` 자리표시자를 채운다.
    v1·v2의 user.md에는 그 자리가 없어 본문이 달라지지 않는다.
    `adjust`(이번 요청만의 조정)와 `contact_known`(설정이 없어도 상대를 밝힘)은 [설정] 블록 안에서만 쓰이고,
    기본값이면 블록이 예전과 같다.
    `register_block`(내 과거 메시지에서 관찰한 말투 상태, `RewriteInput.register_block`)은 v4부터 있는
    `{register_block}` 자리를 채운다. v3까지의 user.md에는 그 자리가 없어 값을 줘도 본문이 달라지지 않는다.
    `voice_block`(내 말투 지문, `app.ai.voice`)은 v5부터 있는 `{voice_block}` 자리를 채운다. 없으면 '(없음)'.
    v4까지의 user.md에는 그 자리가 없어 값을 줘도 본문이 달라지지 않는다.
    """
    user = _load(version, "user").format(
        today=_today_line(today or today_kst()),
        persona_block=_persona_block(contact, persona, contact_known=contact_known, adjust=adjust),
        situation=situation.strip() or "(설명 없음)",
        intent=intent,
        preserved_block=_preserved_block(preserved),
        draft_text=draft_text,
        style_block=_examples_block(retrieved, "style"),
        context_block=_examples_block(retrieved, "context"),
        register_block=_register_block(register_block),
        voice_block=_register_block(voice_block),
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
