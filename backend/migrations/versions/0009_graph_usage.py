"""그래프 수집 사용량 카운터와 상대의 그래프 이름 (2026-11-11 보안·정확성 검토)

- usage_counters.graph_episodes — 사용자·일 단위로 그래프에 넣은 양(메시지 1건 = 1, 긴 메시지는 가중).
  대화를 지워도 줄지 않는다. 그래프 수집 상한(GRAPH_INGEST_*_CAP)이 "지금 들어 있는 수"만 보던 탓에
  가져오기→삭제를 되풀이하면 상한이 무력해졌다.
- contacts.graph_name — 이 상대의 메시지를 처음 그래프에 넣을 때의 이름. 이름(alias)을 바꿔도 그래프 노드
  이름은 그대로라 D 검색이 상대 노드를 못 찾던 것을 막는다. 수집 전이면 NULL.

둘 다 기본값이 있거나 NULL을 허용한다 (기존 행·직접 INSERT하는 코드에 영향 없음).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usage_counters", sa.Column("graph_episodes", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("contacts", sa.Column("graph_name", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "graph_name")
    op.drop_column("usage_counters", "graph_episodes")
