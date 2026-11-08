"""그래프 수집 상태: conversations.me_name, messages.graph_ingested_at, import_jobs.graph_*

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 그래프 에피소드 본문에 쓰는 사용자 표시 이름 (데이터셋의 user.name). 없으면 "사용자".
    op.add_column("conversations", sa.Column("me_name", sa.String(50), nullable=True))
    # 그래프에 넣은 시각. NULL이면 미수집 → 재실행이 멱등.
    op.add_column("messages", sa.Column("graph_ingested_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_messages_graph_pending",
        "messages",
        ["user_id", "conversation_id"],
        postgresql_where=sa.text("graph_ingested_at IS NULL"),
    )
    op.add_column(
        "import_jobs",
        sa.Column("graph_status", sa.String(16), nullable=False, server_default="skipped"),
    )
    op.add_column("import_jobs", sa.Column("graph_episodes", sa.Integer, nullable=False, server_default="0"))
    op.add_column("import_jobs", sa.Column("graph_error", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("import_jobs", "graph_error")
    op.drop_column("import_jobs", "graph_episodes")
    op.drop_column("import_jobs", "graph_status")
    op.drop_index("ix_messages_graph_pending", table_name="messages")
    op.drop_column("messages", "graph_ingested_at")
    op.drop_column("conversations", "me_name")
