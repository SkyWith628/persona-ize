"""대화 상대. 실명 대신 별칭을 쓸 수 있다."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.persona import RelationshipType


class Contact(BaseModel):
    """사용자 소유의 상대 레코드. 모든 조회는 user_id로 격리한다."""

    id: UUID
    user_id: str
    alias: str = Field(min_length=1, max_length=50)
    relationship_type: RelationshipType
    created_at: datetime
    graph_name: str | None = Field(
        None, description="이 상대의 메시지를 처음 그래프에 넣을 때의 이름. 수집 전이면 None"
    )

    @property
    def name_in_graph(self) -> str:
        """그래프 에피소드·검색에서 이 상대를 가리키는 이름 (이름을 바꿔도 처음 수집한 이름을 쓴다)."""
        return self.graph_name or self.alias
