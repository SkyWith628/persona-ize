"""회원관리·소셜 로그인: users 확장, user_identities (2026-11-16)

- users.email / email_verified / display_name / role / status / last_login_at
  - 기존 행은 role='user', status='active', email_verified=false로 채워진다 (server_default).
  - email은 소문자로만 저장한다(CHECK) + 유일(UNIQUE, NULL은 여럿 허용).
  - email_verified는 소셜 제공자가 확인해 준 이메일일 때만 true다.
- users.password_hash NULL 허용 — 소셜 전용 계정은 비밀번호가 없다.
- user_identities — 소셜 계정 연결. (provider, subject) 유일, (user_id, provider) 유일.
  제공자의 토큰은 저장하지 않는다.

관리자 작업 기록은 새 표를 만들지 않고 0008의 admin_access_log를 그대로 쓴다.

downgrade 주의: 상태 열이 사라지면 옛 코드는 정지·승인 대기를 모른다. 그런 계정과 비밀번호 없는(소셜 전용)
계정이 옛 코드에서 로그인되지 않게, 어떤 비밀번호로도 검증되지 않는 값('!')으로 해시를 덮고 세션을 지운다.
(다시 올려도 그 계정들의 비밀번호는 돌아오지 않는다 — 운영자가 재설정한다.)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-16
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
    op.add_column("users", sa.Column("email_verified", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("users", sa.Column("display_name", sa.String(40), nullable=True))
    op.add_column("users", sa.Column("role", sa.String(16), nullable=False, server_default="user"))
    op.add_column("users", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("users", "password_hash", existing_type=sa.String(256), nullable=True)
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_check_constraint("ck_users_email_lower", "users", "email = lower(email)")
    op.create_check_constraint("ck_users_role", "users", "role IN ('user', 'admin')")
    op.create_check_constraint("ck_users_status", "users", "status IN ('active', 'pending', 'suspended')")
    op.create_index("ix_users_status_created", "users", ["status", "created_at"])

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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "subject", name="uq_user_identities_provider_subject"),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_identities_user_provider"),
    )


def downgrade() -> None:
    op.drop_table("user_identities")
    # 옛 코드가 모르는 계정(정지·승인 대기·비밀번호 없음)이 로그인되지 않게 막고 세션을 지운다.
    op.execute(
        "DELETE FROM sessions WHERE user_id IN "
        "(SELECT id FROM users WHERE status <> 'active' OR password_hash IS NULL)"
    )
    op.execute("UPDATE users SET password_hash = '!' WHERE status <> 'active' OR password_hash IS NULL")
    op.drop_index("ix_users_status_created", table_name="users")
    op.drop_constraint("ck_users_status", "users", type_="check")
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.drop_constraint("ck_users_email_lower", "users", type_="check")
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.alter_column("users", "password_hash", existing_type=sa.String(256), nullable=False)
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "status")
    op.drop_column("users", "role")
    op.drop_column("users", "display_name")
    op.drop_column("users", "email_verified")
    op.drop_column("users", "email")
