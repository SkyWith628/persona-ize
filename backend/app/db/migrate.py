"""코드에서 Alembic을 돌리는 진입점. 테스트 DB 준비와 `make migrate`가 쓴다.

주의: Alembic env.py가 `asyncio.run`을 호출하므로 이미 돌고 있는 이벤트 루프 안에서는 부르지 않는다.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade_to_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")
