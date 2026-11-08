"""rewrite_requests.condition — 요청한 실험 조건(A/B/C/D)을 저장 (이력에서 A·B 구분, 블라인드 병합)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 기존 행은 검색기 버전으로 유추할 수 없는 A/B를 구분 못 하므로 'B'(기본 조건)로 채운다.
    op.add_column(
        "rewrite_requests", sa.Column("condition", sa.String(1), nullable=False, server_default="B")
    )


def downgrade() -> None:
    op.drop_column("rewrite_requests", "condition")
