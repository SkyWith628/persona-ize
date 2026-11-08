"""첨삭 서비스: 입력 검증 → 소유권 확인 → 멱등성 → 워크플로 → 저장."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from app.ai.workflow import RewriteWorkflow
from app.core.config import Settings
from app.core.errors import InvalidInputError, UsageLimitError, VersionConflictError
from app.domain.rewrite import Feedback, RewriteInput, RewriteRecord
from app.domain.usage import UsageStatus, usage_day
from app.repositories.base import ContactRepo, RewriteRepo
from app.repositories.usage import UsageRepo


def _input_hash(inp: RewriteInput) -> str:
    payload = inp.model_dump(mode="json")
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class RewriteService:
    def __init__(
        self,
        settings: Settings,
        contacts: ContactRepo,
        rewrites: RewriteRepo,
        workflow: RewriteWorkflow,
        usage: UsageRepo,
    ) -> None:
        self._settings = settings
        self._contacts = contacts
        self._rewrites = rewrites
        self._workflow = workflow
        self._usage = usage

    def _validate(self, inp: RewriteInput) -> None:
        draft = inp.draft_text.strip()
        if not draft:
            raise InvalidInputError("원문이 비어 있습니다.")
        if len(draft) > self._settings.max_draft_chars:
            raise InvalidInputError(f"원문은 {self._settings.max_draft_chars}자 이내여야 합니다.")
        if len(inp.situation) > self._settings.max_situation_chars:
            raise InvalidInputError(f"상황 설명은 {self._settings.max_situation_chars}자 이내여야 합니다.")

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
        if pv is None and inp.condition != "A":
            raise InvalidInputError(
                "이 상대에게 저장된 페르소나가 없습니다. 먼저 설정을 저장하세요 (A 조건은 예외)."
            )

        # 사용량: 모델을 부르기 전에 요청 수를 예약하고, 토큰 한도는 지금까지 쓴 양으로 본다.
        day = usage_day(offset_hours=self._settings.usage_day_offset_hours)
        used = await self._usage.get(inp.user_id, day)
        if used.total_tokens >= self._settings.usage_daily_token_limit:
            raise UsageLimitError(
                f"오늘 토큰 한도({self._settings.usage_daily_token_limit:,})를 모두 썼습니다. "
                "내일 다시 시도하세요."
            )
        reserved = await self._usage.reserve_rewrite(
            inp.user_id, day, self._settings.usage_daily_rewrite_limit
        )
        if reserved is None:
            raise UsageLimitError(
                f"오늘 첨삭 요청 한도({self._settings.usage_daily_rewrite_limit}회)를 모두 썼습니다. "
                "내일 다시 시도하세요."
            )

        result = await self._workflow.run(inp, contact, pv.settings if pv else None)
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
        )

    async def get(self, user_id: str, request_id: UUID) -> RewriteRecord:
        return await self._rewrites.get(user_id, request_id)

    async def feedback(self, user_id: str, fb: Feedback) -> Feedback:
        return await self._rewrites.save_feedback(user_id, fb)

    async def usage(self, user_id: str) -> UsageStatus:
        day = usage_day(offset_hours=self._settings.usage_day_offset_hours)
        used = await self._usage.get(user_id, day)
        return UsageStatus(
            day=day,
            rewrites=used.rewrites,
            rewrite_limit=self._settings.usage_daily_rewrite_limit,
            input_tokens=used.input_tokens,
            output_tokens=used.output_tokens,
            token_limit=self._settings.usage_daily_token_limit,
        )
