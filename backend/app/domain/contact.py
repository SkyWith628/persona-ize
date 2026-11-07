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
