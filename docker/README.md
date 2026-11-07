# docker/

- `../docker-compose.yml`: postgres(pgvector/pgvector:pg16) · neo4j:5 · backend
- `postgres/init/*.sql`: 컨테이너 최초 생성 시 1회 실행 (확장 설치). 스키마는 2주차 Alembic이 관리.

Neo4j 자격증명은 `.env`의 `NEO4J_USERNAME`/`NEO4J_PASSWORD`. 기본 비밀번호 `neo4j-dev-only`는 로컬 전용이다.

주의: 2026-11-08 기준 이 compose 파일은 Docker가 없는 머신에서 작성되어 **아직 실행 검증 전**이다. 첫 실행 시 `docker compose config`로 문법을 먼저 확인한다.
