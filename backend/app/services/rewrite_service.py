"""첨삭 서비스: 입력 검증 → 소유권 확인 → 멱등성 → 조건 결정(auto) → 사용량 → 워크플로 → 저장.

- 멱등성: `Idempotency-Key` + **요청 그대로의** 입력 해시(조건 `auto`도 `auto`인 채로). auto가 고르는 조건은
  그 사이 대화를 가져오면 달라질 수 있으므로, 고른 값이 아니라 요청 값을 해시한다.
- 저장되는 조건(`rewrite_requests.condition`)과 응답의 `condition`은 실제 적용된 A~D.
- 사용량: 모델을 부르기 전에 요청 수를 예약한다. 전체 차단기(`USAGE_GLOBAL_DAILY_*`)는 사용자 예약보다 먼저
  읽기만으로 확인한다 — 원자적이지 않아 동시 요청 수만큼은 넘을 수 있다(차단기로는 충분).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.ai.workflow import RewriteWorkflow
from app.core.config import Settings
from app.core.errors import InvalidInputError, UsageLimitError, VersionConflictError
from app.domain.contact import Contact
from app.domain.conversation import SpeakerRole
from app.domain.persona import PersonaVersion
from app.domain.rewrite import Condition, Feedback, RewriteInput, RewriteRecord
from app.domain.usage import BudgetExceededError, UsageStatus, usage_day
from app.repositories.base import ContactRepo, ConversationRepo, RewriteRepo
from app.repositories.usage import UsageRepo


def _input_hash(inp: RewriteInput) -> str:
    """요청 본문의 해시. 새 선택 필드는 기본값일 때 넣지 않는다 — 예전 요청의 해시가 그대로 유지된다."""
    payload = inp.model_dump(mode="json")
    if payload.get("adjust") is None:
        payload.pop("adjust", None)
    if payload.get("channel") == "web":
        payload.pop("channel", None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class EvidenceMessage(BaseModel):
    speaker_role: SpeakerRole
    sent_at: datetime
    text: str


class EvidenceItem(BaseModel):
    """참고한 과거 표현 하나와, 지금 남아 있는 그 원문 메시지들."""

    id: str
    source_message_ids: list[str]
    messages: list[EvidenceMessage]


class Evidence(BaseModel):
    items: list[EvidenceItem] = Field(default_factory=list)
    missing: int = Field(0, description="원문이 모두 지워져 보여 줄 수 없는 근거 수")


class RewriteService:
    def __init__(
        self,
        settings: Settings,
        contacts: ContactRepo,
        rewrites: RewriteRepo,
        workflow: RewriteWorkflow,
        usage: UsageRepo,
        *,
        conversations: ConversationRepo | None = None,
        graph_enabled: bool = False,
    ) -> None:
        self._settings = settings
        self._contacts = contacts
        self._rewrites = rewrites
        self._workflow = workflow
        self._usage = usage
        # 조건 auto(쌓인 대화 자료 확인)·근거 원문 조회·그래프 사용량에 쓴다. 없으면 그 기능만 줄어든다.
        self._conversations = conversations
        self.graph_enabled = graph_enabled  # D 조건을 쓸 수 있는가 (NEO4J_URI)

    def _validate(self, inp: RewriteInput) -> None:
        draft = inp.draft_text.strip()
        if not draft:
            raise InvalidInputError("원문이 비어 있습니다.")
        if len(draft) > self._settings.max_draft_chars:
            raise InvalidInputError(f"원문은 {self._settings.max_draft_chars}자 이내여야 합니다.")
        if len(inp.situation) > self._settings.max_situation_chars:
            raise InvalidInputError(f"상황 설명은 {self._settings.max_situation_chars}자 이내여야 합니다.")

    async def resolve_auto(self, user_id: str, contact: Contact, persona: PersonaVersion | None) -> Condition:
        """이 상대에 쓸 수 있는 가장 풍부한 조건.

        그래프 사용 가능 + 이 상대의 그래프 수집 메시지 ≥1 → D / 문체 묶음 ≥1 → C /
        페르소나 있음 → B / 아니면 A.
        C·D는 저장된 페르소나가 없어도 고른다 — 그때는 설정 없이 과거 표현(과 사실 맥락)만 참고한다.
        """
        if self._conversations is not None:
            stats = (await self._conversations.contact_stats(user_id, contact.id)).get(contact.id)
            if stats is not None:
                if self.graph_enabled and stats.graph_ingested >= 1:
                    return "D"
                if stats.episodes >= 1:
                    return "C"
        return "B" if persona is not None else "A"

    async def _check_budget(self, user_id: str, day: date) -> None:
        """한도 확인 (읽기만). 사용자 한도를 먼저 알리고, 그다음 서비스 전체 차단기."""
        s = self._settings
        used = await self._usage.get(user_id, day)
        if used.total_tokens >= s.usage_daily_token_limit:
            raise UsageLimitError(
                f"오늘 토큰 한도({s.usage_daily_token_limit:,})를 모두 썼습니다. 내일 다시 시도하세요."
            )
        if not (s.usage_global_daily_rewrite_limit or s.usage_global_daily_token_limit):
            return
        total = await self._usage.totals(day)
        over_rewrites = 0 < s.usage_global_daily_rewrite_limit <= total.rewrites
        over_tokens = 0 < s.usage_global_daily_token_limit <= total.total_tokens
        if (over_rewrites or over_tokens) and used.rewrites < s.usage_daily_rewrite_limit:
            # 사용자 자신의 한도도 찼으면 아래 예약에서 usage_exceeded로 알린다.
            raise BudgetExceededError(
                "오늘 서비스 전체 사용 한도에 닿았습니다. 개인 한도와는 별개이며, 내일 다시 시도해 주세요."
            )

    async def rewrite(self, inp: RewriteInput, idempotency_key: str | None) -> RewriteRecord:
        self._validate(inp)
        contact = await self._contacts.get(inp.user_id, inp.contact_id)  # 소유권: 없으면 404

        input_hash = _input_hash(inp)
        if idempotency_key:
            existing = await self._rewrites.find_by_idempotency(inp.user_id, idempotency_key)
            if existing is not None:
                if existing.input_hash != input_hash:
                    raise VersionConflictError("같은 Idempotency-Key로 다른 본문이 요청되었습니다.")
                return existing

        if inp.persona_version is not None:
            pv = await self._contacts.get_persona(inp.user_id, inp.contact_id, inp.persona_version)
        else:
            pv = await self._contacts.latest_persona(inp.user_id, inp.contact_id)
        condition: Condition
        if inp.condition == "auto":
            condition = await self.resolve_auto(inp.user_id, contact, pv)
        else:
            condition = inp.condition
            if pv is None and condition != "A":
                raise InvalidInputError(
                    "이 상대에게 저장된 페르소나가 없습니다. 먼저 설정을 저장하세요 (A 조건은 예외)."
                )

        # 사용량: 모델을 부르기 전에 요청 수를 예약하고, 토큰 한도는 지금까지 쓴 양으로 본다.
        day = usage_day(offset_hours=self._settings.usage_day_offset_hours)
        await self._check_budget(inp.user_id, day)
        reserved = await self._usage.reserve_rewrite(
            inp.user_id, day, self._settings.usage_daily_rewrite_limit
        )
        if reserved is None:
            raise UsageLimitError(
                f"오늘 첨삭 요청 한도({self._settings.usage_daily_rewrite_limit}회)를 모두 썼습니다. "
                "내일 다시 시도하세요."
            )

        run_input = inp.model_copy(update={"condition": condition})
        result = await self._workflow.run(run_input, contact, pv.settings if pv else None)
        await self._usage.add_tokens(
            inp.user_id,
            day,
            result.token_usage.get("input_tokens", 0),
            result.token_usage.get("output_tokens", 0),
        )
        return await self._rewrites.save(
            user_id=inp.user_id,
            contact_id=inp.contact_id,
            persona_version_id=pv.id if pv else None,
            draft_version=inp.draft_version,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            result=result,
            condition=condition,
            channel=inp.channel,
        )

    async def get(self, user_id: str, request_id: UUID) -> RewriteRecord:
        return await self._rewrites.get(user_id, request_id)

    async def history(
        self, user_id: str, contact_id: UUID | None, limit: int, before: datetime | None = None
    ) -> list[RewriteRecord]:
        return await self._rewrites.list(user_id, contact_id, limit, before)

    async def feedback(self, user_id: str, fb: Feedback) -> Feedback:
        return await self._rewrites.save_feedback(user_id, fb)

    async def delete(self, user_id: str, request_id: UUID) -> None:
        """첨삭 이력 한 건 삭제 (결과·피드백 포함). 없거나 남의 것이면 404."""
        await self._rewrites.delete(user_id, request_id)

    async def delete_by_contact(self, user_id: str, contact_id: UUID) -> int:
        """한 상대의 첨삭 이력을 모두 지운다. 지운 수."""
        await self._contacts.get(user_id, contact_id)  # 소유권: 없으면 404
        return await self._rewrites.delete_by_contact(user_id, contact_id)

    async def evidence(self, user_id: str, request_id: UUID) -> Evidence:
        """참고한 과거 표현의 **지금 남아 있는** 원문.

        결과에는 근거 id → 출처 메시지 참조만 있고 원문 사본은 없다. 대화방을 지우면 여기서도 사라진다.
        """
        rec = await self._rewrites.get(user_id, request_id)
        refs = {eid: rec.result.evidence_refs.get(eid, []) for eid in rec.result.evidence_ids}
        wanted = sorted({ref for ids in refs.values() for ref in ids})
        found = (
            await self._conversations.find_messages(user_id, wanted)
            if (wanted and self._conversations is not None)
            else {}
        )
        out = Evidence()
        for eid, ids in refs.items():
            messages = sorted((found[ref] for ref in ids if ref in found), key=lambda m: m.sent_at)
            if not messages:
                out.missing += 1
                continue
            out.items.append(
                EvidenceItem(
                    id=eid,
                    source_message_ids=[ref for ref in ids if ref in found],
                    messages=[
                        EvidenceMessage(speaker_role=m.speaker_role, sent_at=m.sent_at, text=m.text)
                        for m in messages
                    ],
                )
            )
        return out

    async def usage(self, user_id: str) -> UsageStatus:
        day = usage_day(offset_hours=self._settings.usage_day_offset_hours)
        used = await self._usage.get(user_id, day)
        graph_ingested = 0
        if self.graph_enabled and self._conversations is not None:
            graph_ingested = await self._conversations.count_graph_ingested(user_id)
        return UsageStatus(
            day=day,
            rewrites=used.rewrites,
            rewrite_limit=self._settings.usage_daily_rewrite_limit,
            input_tokens=used.input_tokens,
            output_tokens=used.output_tokens,
            token_limit=self._settings.usage_daily_token_limit,
            graph_messages_ingested=graph_ingested,
            graph_message_cap=self._settings.graph_ingest_user_message_cap,
        )
