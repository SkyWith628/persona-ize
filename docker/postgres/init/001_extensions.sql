-- 컨테이너 최초 생성 시 1회 실행된다. 스키마 자체는 2주차에 Alembic 마이그레이션으로 관리한다.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
