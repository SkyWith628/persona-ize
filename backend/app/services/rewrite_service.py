"""첨삭 서비스: 입력 검증 → 소유권 확인 → 멱등성 → 워크플로 → 저장."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from app.ai.workflow import RewriteWorkflow
from app.core.config import Settings
from app.core.errors import InvalidInputError, VersionConflictError
from app.domain.rewrite import Feedback, RewriteInput, RewriteRecord
from app.repositories.base import ContactRepo, RewriteRepo


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
    ) -> None:
        self._settings = settings
        self._contacts = contacts
        self._rewrites = rewrites
        self._workflow = workflow

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

        result = await self._workflow.run(inp, contact, pv.settings if pv else None)
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
