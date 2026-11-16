"""회원관리·소셜 로그인: users 확장, user_identities, admin_audit_log

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 기존 행은 role=user, status=active로 채워진다 (server_default).
    op.add_column("users", sa.Column("email", sa.String(254), nullable=True))  # 소문자로 저장
    op.add_column("users", sa.Column("display_name", sa.String(40), nullable=True))
    op.add_column("users", sa.Column("role", sa.String(16), nullable=False, server_default="user"))
    op.add_column("users", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    # 소셜 전용 계정은 비밀번호가 없다.
    op.alter_column("users", "password_hash", existing_type=sa.String(256), nullable=True)
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_index("ix_users_created", "users", ["created_at"])

    op.create_table(
        "user_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "subject", name="uq_user_identities_provider_subject"),
    )
    op.create_index("ix_user_identities_user", "user_identities", ["user_id"])

    # 사용자 행에 FK를 걸지 않는다 - 계정을 지운 기록도 남아야 한다.
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admin_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("admin_username", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_username", sa.String(32), nullable=True),
        sa.Column("detail", sa.String(300), nullable=False, server_default=""),
    )
    op.create_index("ix_admin_audit_log_at", "admin_audit_log", ["at"])


def downgrade() -> None:
    op.drop_index("ix_admin_audit_log_at", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")
    op.drop_index("ix_user_identities_user", table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_index("ix_users_created", table_name="users")
    op.drop_constraint("uq_users_email", "users", type_="unique")
    # 비밀번호 없는(소셜 전용) 계정은 어떤 비밀번호로도 검증되지 않는 값으로 채운다.
    op.execute("UPDATE users SET password_hash = '!' WHERE password_hash IS NULL")
    op.alter_column("users", "password_hash", existing_type=sa.String(256), nullable=False)
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "status")
    op.drop_column("users", "role")
    op.drop_column("users", "display_name")
    op.drop_column("users", "email")
