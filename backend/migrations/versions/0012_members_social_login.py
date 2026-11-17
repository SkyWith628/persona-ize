"""회원 상태·소셜 로그인 (2026-11-17)

- users.email(소문자, 유일)·display_name·role·status·last_login_at
- users.password_hash NULL 허용 - 소셜 전용 계정은 비밀번호가 없다
- user_identities - 소셜 계정 연결 (provider + subject 유일)

기존 행은 role=user, status=active로 채워진다 (server_default). 관리자 작업 기록은 0008의
admin_access_log를 그대로 쓴다 (새 표를 만들지 않는다).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(254), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.String(40), nullable=True))
    op.add_column("users", sa.Column("role", sa.String(16), nullable=False, server_default="user"))
    op.add_column("users", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("users", "password_hash", existing_type=sa.String(256), nullable=True)
    op.create_unique_constraint("uq_users_email", "users", ["email"])

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


def downgrade() -> None:
    op.drop_index("ix_user_identities_user", table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_constraint("uq_users_email", "users", type_="unique")
    # A social-only account has no password; fill a value that no password can match.
    op.execute("UPDATE users SET password_hash = '!' WHERE password_hash IS NULL")
    op.alter_column("users", "password_hash", existing_type=sa.String(256), nullable=False)
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "status")
    op.drop_column("users", "role")
    op.drop_column("users", "display_name")
    op.drop_column("users", "email")
