"""상대·페르소나 버전·첨삭 요청/결과·피드백 (project-design §6, 1주차 수직 기능 범위)

Revision ID: 0001
Revises:
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 확장은 compose의 init SQL도 깔지만, 다른 환경(테스트 DB 등)을 위해 여기서도 보장한다.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("alias", sa.String(50), nullable=False),
        sa.Column("relationship_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_contacts_user_created", "contacts", ["user_id", "created_at"])

    op.create_table(
        "persona_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("settings", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("contact_id", "version", name="uq_persona_versions_contact_version"),
    )
    op.create_index("ix_persona_versions_user_contact", "persona_versions", ["user_id", "contact_id"])

    op.create_table(
        "rewrite_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "persona_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("persona_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("draft_version", sa.Integer, nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_rewrite_requests_user_idem"),
    )
    op.create_index("ix_rewrite_requests_user_created", "rewrite_requests", ["user_id", "created_at"])

    op.create_table(
        "rewrite_results",
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rewrite_requests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("result_text", sa.Text, nullable=False),
        sa.Column("validation_status", sa.String(16), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("retrieval_version", sa.String(64), nullable=False),
        sa.Column("token_usage", postgresql.JSONB, nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
    )

    op.create_table(
        "feedback",
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rewrite_requests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("accepted", sa.Boolean, nullable=False),
        sa.Column("edited_text", sa.Text, nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("consent_to_reuse", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("feedback")
    op.drop_table("rewrite_results")
    op.drop_index("ix_rewrite_requests_user_created", table_name="rewrite_requests")
    op.drop_table("rewrite_requests")
    op.drop_index("ix_persona_versions_user_contact", table_name="persona_versions")
    op.drop_table("persona_versions")
    op.drop_index("ix_contacts_user_created", table_name="contacts")
    op.drop_table("contacts")
