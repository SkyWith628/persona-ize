"""첨삭 서비스: 입력 검증 → 소유권 확인 → 멱등성 → 조건 결정(auto) → 사용량 → 워크플로 → 저장.

- 멱등성: `Idempotency-Key` + **요청 그대로의** 입력 해시(조건 `auto`도 `auto`인 채로). auto가 고르는 조건은
  그 사이 대화를 가져오면 달라질 수 있으므로, 고른 값이 아니라 요청 값을 해시한다.
  같은 키의 요청이 겹치면 (사용자, 키) 잠금으로 한 번에 하나씩 처리해 모델을 한 번만 부른다.
- 저장에서 실패할 값(128자를 넘는 키, 저장할 수 없는 문자)은 모델을 부르기 전에 422로 돌려보낸다.
- 저장되는 조건(`rewrite_requests.condition`)과 응답의 `condition`은 실제 적용된 A~D.
- 사용량: 모델을 부르기 전에 한도 확인 → 요청 수 예약 → "진행 중" 등록을 **한 번에 하나씩** 한다
  (`_admit`, 프로세스 안 잠금). 토큰은 호출이 끝나야 기록되므로, 그 사이에 들어온 요청은
  "기록된 토큰 + 진행 중 요청 수 × 요청당 추정"으로 본다 — 동시에 수십 건을 보내 한도를 몇 배씩
  넘기지 못하게 (2026-11-11 검토). 서버 프로세스가 여럿이면 프로세스 사이의 경합은 남는다.
- 모델 호출이 실패·시간 초과로 끝나도 보낸 프롬프트만큼의 추정 입력 토큰을 사용량에 더한다
  (`token_usage.estimated_input_tokens` — 워크플로가 채운다).
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
from app.domain.rewrite import IDEMPOTENCY_KEY_MAX, Condition, Feedback, RewriteInput, RewriteRecord
from app.domain.usage import BudgetExceededError, UsageStatus, usage_day
from app.importers.base import has_unstorable_text
from app.repositories.base import ContactRepo, ConversationRepo, RewriteRepo
from app.repositories.usage import UsageRepo
from app.services.locks import KeyedLocks

# 진행 중인 요청 하나가 쓸 토큰의 추정값. 끝난 요청의 실제 사용량으로 조금씩 맞춰 간다.
DEFAULT_REQUEST_TOKENS = 2_000
MIN_REQUEST_TOKENS, MAX_REQUEST_TOKENS = 500, 20_000


def _input_hash(inp: RewriteInput) -> str:
    """요청 본문의 해시. 새 선택 필드는 기본값일 때 넣지 않는다 — 예전 요청의 해시가 그대로 유지된다."""
    payload = inp.model_dump(mode="json")
    if payload.get("adjust") is None:
        payload.pop("adjust", None)
    if payload.get("channel") == "web":
        payload.pop("channel", None)
    # 서버가 내 과거 메시지에서 만들어 붙이는 값이다 — 요청 본문이 아니므로 해시에 넣지 않는다
    payload.pop("register_block", None)
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
    refs_recorded: bool = Field(
        True,
        description="이 결과에 근거의 출처 참조가 남아 있는가. 거짓이면 참조를 남기기 전에 만든 결과다",
    )


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
        # 진행 중(모델 호출이 끝나지 않은) 요청 — 프로세스 메모리. 한도 계산에만 쓴다.
        self._locks = KeyedLocks()
        self._in_flight: dict[str, int] = {}
        self._in_flight_total = 0
        self._request_tokens = DEFAULT_REQUEST_TOKENS

    def _validate(self, inp: RewriteInput, idempotency_key: str | None = None) -> None:
        """모델을 부르기 전에 걸러야 하는 것들 — 뒤늦게 저장에서 실패하면 비용만 쓰고 결과가 남지 않는다."""
        draft = inp.draft_text.strip()
        if not draft:
            raise InvalidInputError("원문이 비어 있습니다.")
        if len(draft) > self._settings.max_draft_chars:
            raise InvalidInputError(f"원문은 {self._settings.max_draft_chars}자 이내여야 합니다.")
        if len(inp.situation) > self._settings.max_situation_chars:
            raise InvalidInputError(f"상황 설명은 {self._settings.max_situation_chars}자 이내여야 합니다.")
        if has_unstorable_text([inp.draft_text, inp.situation]):
            raise InvalidInputError("원문이나 상황 설명에 저장할 수 없는 문자가 있습니다 (NUL 등).")
        if idempotency_key and (
            len(idempotency_key) > IDEMPOTENCY_KEY_MAX
            or not idempotency_key.isprintable()
            or has_unstorable_text(idempotency_key)
        ):
            raise InvalidInputError(
                f"Idempotency-Key는 {IDEMPOTENCY_KEY_MAX}자 이내의 보이는 문자여야 합니다."
            )

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

    async def _admit(self, user_id: str, day: date) -> None:
        """한도 확인 → 요청 수 예약 → 진행 중 등록. 통과하면 반드시 `_release`로 짝을 맞춘다.

        사용자 한도를 먼저 알리고, 그다음 서비스 전체 차단기. 전체를 잠금 하나로 직렬화해서
        확인과 예약 사이에 다른 요청이 끼어들지 못한다.
        """
        s = self._settings
        async with self._locks.hold("admission"):
            used = await self._usage.get(user_id, day)
            if used.total_tokens >= s.usage_daily_token_limit:
                raise UsageLimitError(
                    f"오늘 토큰 한도({s.usage_daily_token_limit:,})를 모두 썼습니다. 내일 다시 시도하세요."
                )
            waiting = self._in_flight.get(user_id, 0) * self._request_tokens
            if used.total_tokens + waiting >= s.usage_daily_token_limit:
                raise UsageLimitError(
                    f"진행 중인 요청까지 합치면 오늘 토큰 한도({s.usage_daily_token_limit:,})에 "
                    "닿습니다. 진행 중인 요청이 끝난 뒤 다시 시도하세요."
                )
            if s.usage_global_daily_rewrite_limit or s.usage_global_daily_token_limit:
                total = await self._usage.totals(day)
                expected = total.total_tokens + self._in_flight_total * self._request_tokens
                over_rewrites = 0 < s.usage_global_daily_rewrite_limit <= total.rewrites
                over_tokens = 0 < s.usage_global_daily_token_limit <= expected
                if (over_rewrites or over_tokens) and used.rewrites < s.usage_daily_rewrite_limit:
                    # 사용자 자신의 한도도 찼으면 아래 예약에서 usage_exceeded로 알린다.
                    raise BudgetExceededError(
                        "오늘 서비스 전체 사용 한도에 닿았습니다. 개인 한도와는 별개이며, "
                        "내일 다시 시도해 주세요."
                    )
            reserved = await self._usage.reserve_rewrite(user_id, day, s.usage_daily_rewrite_limit)
            if reserved is None:
                raise UsageLimitError(
                    f"오늘 첨삭 요청 한도({s.usage_daily_rewrite_limit}회)를 모두 썼습니다. "
                    "내일 다시 시도하세요."
                )
            self._in_flight[user_id] = self._in_flight.get(user_id, 0) + 1
            self._in_flight_total += 1

    def _release(self, user_id: str) -> None:
        left = self._in_flight.get(user_id, 0) - 1
        if left > 0:
            self._in_flight[user_id] = left
        else:
            self._in_flight.pop(user_id, None)
        self._in_flight_total = max(self._in_flight_total - 1, 0)

    def _learn(self, tokens: int) -> None:
        """끝난 요청의 사용량으로 요청당 추정값을 맞춘다 (이동 평균, 상·하한 안에서)."""
        if tokens > 0:
            blended = (self._request_tokens * 4 + tokens) // 5
            self._request_tokens = min(max(blended, MIN_REQUEST_TOKENS), MAX_REQUEST_TOKENS)

    async def rewrite(self, inp: RewriteInput, idempotency_key: str | None) -> RewriteRecord:
        self._validate(inp, idempotency_key)
        if not idempotency_key:
            return await self._rewrite(inp, None)
        # 같은 키의 요청이 겹치면(두 번 눌림, 재시도) 한 번에 하나씩 처리한다: 뒤 요청은 앞 요청이 저장한
        # 결과를 돌려받는다. 예전에는 둘 다 모델을 부르고 한쪽이 유일 제약에 걸려 409였다.
        async with self._locks.hold(("idempotency", inp.user_id, idempotency_key)):
            return await self._rewrite(inp, idempotency_key)

    async def _rewrite(self, inp: RewriteInput, idempotency_key: str | None) -> RewriteRecord:
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

        # 사용량: 모델을 부르기 전에 한도를 확인하고 요청 수를 예약한다 (진행 중 요청까지 센다).
        day = usage_day(offset_hours=self._settings.usage_day_offset_hours)
        await self._admit(inp.user_id, day)
        try:
            run_input = inp.model_copy(update={"condition": condition})
            result = await self._workflow.run(run_input, contact, pv.settings if pv else None)
            # 실패·시간 초과한 호출은 실제 토큰 수를 모른다 — 보낸 프롬프트만큼의 추정치를 입력에 더한다.
            input_tokens = result.token_usage.get("input_tokens", 0) + result.token_usage.get(
                "estimated_input_tokens", 0
            )
            output_tokens = result.token_usage.get("output_tokens", 0)
            await self._usage.add_tokens(inp.user_id, day, input_tokens, output_tokens)
            self._learn(input_tokens + output_tokens)
        finally:
            self._release(inp.user_id)
        try:
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
        except VersionConflictError:
            # 다른 서버 프로세스가 같은 키의 요청을 방금 저장했다 (프로세스 안에서는 위 잠금이 막는다).
            # 본문이 같으면 그 결과를 돌려준다.
            if idempotency_key:
                other = await self._rewrites.find_by_idempotency(inp.user_id, idempotency_key)
                if other is not None and other.input_hash == input_hash:
                    return other
            raise

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
        참조가 없는 옛 결과는 `refs_recorded=False`(items·missing은 비어 있다)로 알린다.
        """
        rec = await self._rewrites.get(user_id, request_id)
        if rec.result.evidence_ids and not rec.result.evidence_refs:
            # 출처 참조를 남기기 전에 만든 결과다. 원문이 지워진 것(missing)과 구분해 알린다 —
            # 예전에는 근거 수만큼 missing으로 세어 '대화를 지웠다'는 뜻으로 읽혔다.
            return Evidence(refs_recorded=False)
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
        graph_used = 0
        if self._conversations is not None:
            # 그래프 수집 상한이 보는 값과 같다: max(지금 그래프에 든 양, 오늘 넣은 양).
            # 오늘 넣었다가 지운 것도 오늘은 남는다. 그래프를 꺼 둔 서버에서도 예전에 넣은 것이 있으면
            # 그대로 보인다 (데이터 요약의 graph_ingested와 어긋나지 않게).
            graph_used = max(
                await self._conversations.graph_units_in_graph(user_id),
                await self._conversations.graph_usage(day, user_id),
            )
        return UsageStatus(
            day=day,
            rewrites=used.rewrites,
            rewrite_limit=self._settings.usage_daily_rewrite_limit,
            input_tokens=used.input_tokens,
            output_tokens=used.output_tokens,
            token_limit=self._settings.usage_daily_token_limit,
            graph_messages_ingested=graph_used,
            graph_message_cap=self._settings.graph_ingest_user_message_cap,
        )
