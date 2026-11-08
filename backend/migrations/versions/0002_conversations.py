"""대화방·가져오기 작업·메시지·검색 단위(episodes, pgvector 384) (project-design §5.1, §6)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 384  # intfloat/multilingual-e5-small. 모델을 바꾸면 새 마이그레이션 + 재색인.


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "source_key", name="uq_conversations_user_source_key"),
    )
    op.create_index("ix_conversations_user_contact", "conversations", ["user_id", "contact_id"])

    op.create_table(
        "import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("imported", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duplicates", sa.Integer, nullable=False, server_default="0"),
        sa.Column("episodes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id", "conversation_id", "source_hash", name="uq_import_jobs_user_conv_hash"
        ),
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("speaker_role", sa.String(8), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "source_key", name="uq_messages_conversation_source_key"),
    )
    op.create_index(
        "ix_messages_user_conversation_sent", "messages", ["user_id", "conversation_id", "sent_at"]
    )

    op.create_table(
        "episodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("context_text", sa.Text, nullable=False),
        sa.Column("style_text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("embedding_version", sa.String(128), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_message_ids", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_episodes_user_contact_end", "episodes", ["user_id", "contact_id", "end_at"])
    # 소유권·상대 조건으로 범위를 좁힌 뒤 정확 검색이 기본(project-design §4). 데이터가 늘면 HNSW를 켠다:
    # CREATE INDEX ix_episodes_embedding ON episodes USING hnsw (embedding vector_cosine_ops);

    op.create_table(
        "episode_messages",
        sa.Column(
            "episode_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("episodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("episode_messages")
    op.drop_index("ix_episodes_user_contact_end", table_name="episodes")
    op.drop_table("episodes")
    op.drop_index("ix_messages_user_conversation_sent", table_name="messages")
    op.drop_table("messages")
    op.drop_table("import_jobs")
    op.drop_index("ix_conversations_user_contact", table_name="conversations")
    op.drop_table("conversations")
