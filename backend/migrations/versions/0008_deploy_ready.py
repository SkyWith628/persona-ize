"""배포 준비: 동의 기록, 요청 채널, 선택형 피드백 사유, 관리자 접근 기록

- users.consented_at / consent_version — 약관·개인정보 안내에 동의한 시각과 버전
- rewrite_requests.channel — 요청이 온 곳(web/mobile/desktop/api). 기존 행은 'web'
- feedback.reason_code — 선택형 거절 사유 (관리자 화면은 자유 서술을 보여 주지 않는다)
- admin_access_log — 관리자 API 접근 기록

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11
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
    op.add_column("users", sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("consent_version", sa.String(32), nullable=True))
    op.add_column(
        "rewrite_requests", sa.Column("channel", sa.String(16), nullable=False, server_default="web")
    )
    op.add_column("feedback", sa.Column("reason_code", sa.String(32), nullable=True))
    op.create_table(
        "admin_access_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_user_id", sa.String(128), nullable=False),
        sa.Column("admin_username", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("detail", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_access_log_created", "admin_access_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_access_log_created", table_name="admin_access_log")
    op.drop_table("admin_access_log")
    op.drop_column("feedback", "reason_code")
    op.drop_column("rewrite_requests", "channel")
    op.drop_column("users", "consent_version")
    op.drop_column("users", "consented_at")
