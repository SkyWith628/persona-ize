"""메시지에 원래 화자 이름을 남긴다 (2026-11-15)

- messages.speaker_name — 내보내기에 적혀 있던 화자 이름. 역할(me/contact)만 저장하던 것에 더한다.

화자를 잘못 지정한 가져오기를 화자별로 고치고(단체방에서 한 사람만 나/상대/제외로), 다시 올릴 때 누가 이미
들어와 있는지 알려 주는 데 쓴다. NULL 허용 — 이 열이 생기기 전의 메시지와 이름 없이 역할로만 올린
메시지(`speaker: "me"`)는 비어 있다. 같은 내보내기를 다시 올리면 빈 이름이 채워진다.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("speaker_name", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "speaker_name")
