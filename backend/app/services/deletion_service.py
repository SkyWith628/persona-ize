"""상대 삭제·계정 삭제 (project-design §9: 삭제는 원문·에피소드·임베딩·그래프까지 추적).

- 상대 삭제: 그 상대의 대화방을 하나씩 지운다(각 대화방의 그래프 에피소드 → DB CASCADE). 그 뒤 상대 행을
  지우면 페르소나 버전·첨삭 요청·결과·피드백이 FK CASCADE로 따라간다. 다른 상대의 그래프 사실은 남는다.
- 계정 삭제: 비밀번호 확인 → 상대 전부 삭제 → 그래프 group(user_id) 통째로 삭제(남은 노드 정리) → 사용량·
  세션·사용자 행 삭제. 되돌릴 수 없다. 비밀번호가 없는 소셜 전용 계정은 세션(+CSRF)만으로 지우고, 관리자가
  지울 때(`delete_account_as_admin`)는 비밀번호를 묻지 않는다 — 권한 검사와 기록은 부르는 쪽이 한다.
- 그래프 삭제가 실패해도 DB 삭제는 진행하고 사유를 돌려준다(원문 삭제가 우선).
"""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.security import verify_password_async
from app.domain.auth import Session, User
from app.domain.conversation import exception_kind
from app.repositories.auth import AuthRepo
from app.repositories.base import ContactRepo, ConversationRepo, RewriteRepo
from app.repositories.usage import UsageRepo
from app.services.auth_service import UnauthorizedError
from app.services.import_service import ImportService

log = logging.getLogger(__name__)


class ContactDeletion(BaseModel):
    contact_id: UUID
    conversations: int
    messages: int
    episodes: int
    graph_episodes: int
    rewrites: int = 0
    graph_errors: list[str] = Field(default_factory=list)


class AccountDeletion(BaseModel):
    user_id: str
    contacts: int
    conversations: int
    messages: int
    graph_nodes_removed: int = Field(0, description="그래프 group 전체 삭제로 지운 노드 수")
    graph_error: str | None = None


class DeletionService:
    def __init__(
        self,
        contacts: ContactRepo,
        conversations: ConversationRepo,
        rewrites: RewriteRepo,
        import_service: ImportService,
        usage: UsageRepo,
        auth: AuthRepo,
        graph_store=None,  # noqa: ANN001 - GraphStore | None
    ) -> None:
        self._contacts = contacts
        self._conversations = conversations
        self._rewrites = rewrites
        self._imports = import_service
        self._usage = usage
        self._auth = auth
        self._graph = graph_store

    async def delete_contact(
        self, user_id: str, contact_id: UUID, *, remove_graph: bool = True
    ) -> ContactDeletion:
        """`remove_graph=False`는 계정 삭제 전용: 그래프는 부른 쪽이 통째로 지운다."""
        contact = await self._contacts.get(user_id, contact_id)  # 소유권
        result = ContactDeletion(
            contact_id=contact.id, conversations=0, messages=0, episodes=0, graph_episodes=0
        )
        for conv in await self._conversations.list(user_id, contact.id):
            d = await self._imports.delete_conversation(user_id, conv.id, remove_graph=remove_graph)
            result.conversations += 1
            result.messages += d.messages
            result.episodes += d.episodes
            result.graph_episodes += d.graph_episodes
            if d.graph_error:
                result.graph_errors.append(f"{conv.source_key}: {d.graph_error}")
        result.rewrites = await self._rewrites.delete_by_contact(user_id, contact.id)
        await self._contacts.delete(user_id, contact.id)  # 페르소나 버전은 CASCADE
        return result

    async def delete_account(self, session: Session, password: str) -> AccountDeletion:
        """본인 삭제. 비밀번호가 있으면 재확인하고, 소셜 전용 계정(비밀번호 없음)은 세션·CSRF만으로 지운다."""
        user = await self._auth.get_user(session.user_id)
        found = await self._auth.find_user_by_username(user.username)
        if found is None:
            raise UnauthorizedError("비밀번호가 맞지 않습니다.")
        # 비밀번호 해시(scrypt)는 수십 ms짜리 CPU 작업이다 — 이벤트 루프 밖에서 확인한다.
        if found[1] is not None and not await verify_password_async(password, found[1]):
            raise UnauthorizedError("비밀번호가 맞지 않습니다.")
        return await self._wipe(user)

    async def delete_account_as_admin(self, user_id: UUID) -> AccountDeletion:
        """관리자가 지운다 (비밀번호 없이). 권한·자기 보호·기록은 `MemberAdminService`가 맡는다."""
        return await self._wipe(await self._auth.get_user(user_id))

    async def _wipe(self, user: User) -> AccountDeletion:
        user_id = str(user.id)
        result = AccountDeletion(user_id=user_id, contacts=0, conversations=0, messages=0)
        # 그래프가 있으면 이 사용자의 것을 아래에서 통째로 지운다 — 대화방마다 에피소드를 하나씩 지우지 않는다
        # (그래프가 없는 서버에서는 예전처럼 대화방 삭제가 그래프를 건너뛴다).
        per_conversation = self._graph is None
        for contact in await self._contacts.list(user_id):
            d = await self.delete_contact(user_id, contact.id, remove_graph=per_conversation)
            result.contacts += 1
            result.conversations += d.conversations
            result.messages += d.messages
        if self._graph is not None:
            try:
                result.graph_nodes_removed = await self._graph.remove_group(user_id)
            except Exception as exc:  # noqa: BLE001 - DB 삭제는 계속
                result.graph_error = exception_kind(exc)
                log.warning("graph group removal failed user=%s: %s", user_id, result.graph_error)
        await self._usage.delete_user(user_id)
        await self._auth.delete_user(user.id)  # 세션·소셜 연결은 CASCADE
        return result
