"""rewrite_results의 버전 열을 200자로 (D 검색기 버전 문자열이 64자를 넘어 500이 났다, 11-08 E2E)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("model_version", "prompt_version", "retrieval_version")


def upgrade() -> None:
    for name in COLUMNS:
        op.alter_column("rewrite_results", name, type_=sa.String(200), existing_type=sa.String(64))


def downgrade() -> None:
    for name in COLUMNS:
        op.alter_column("rewrite_results", name, type_=sa.String(64), existing_type=sa.String(200))
