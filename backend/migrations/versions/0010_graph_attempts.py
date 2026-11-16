"""그래프 수집의 메시지별 실패 횟수와 보류 (2026-11-13)

- messages.graph_attempts — 이 메시지를 그래프에 넣다 실패한 횟수 (일시 장애는 세지 않는다).
- messages.graph_held_at — 횟수를 다 써서 더 시도하지 않기로 한 시각. 보류된 메시지는 미수집으로 세지 않는다.

모델이 늘 실패하는 메시지 하나가 그 사용자의 수집을 영영 막고, 재시작·주기 이어받기 때마다 비용을 쓰던 것을
막는다. 둘 다 기본값이 있거나 NULL을 허용한다 (기존 행·직접 INSERT하는 코드에 영향 없음).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages", sa.Column("graph_attempts", sa.SmallInteger(), nullable=False, server_default="0")
    )
    op.add_column("messages", sa.Column("graph_held_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "graph_held_at")
    op.drop_column("messages", "graph_attempts")
