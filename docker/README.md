# docker/

- `../docker-compose.yml`: postgres(pgvector/pgvector:pg16) · neo4j:5 · backend
- `postgres/init/*.sql`: 컨테이너 최초 생성 시 1회 실행 (확장 설치). 스키마는 2주차 Alembic이 관리.

Neo4j 자격증명은 `.env`의 `NEO4J_USERNAME`/`NEO4J_PASSWORD`. 기본 비밀번호 `neo4j-dev-only`는 로컬 전용이다.

주의: 2026-11-08 기준 `docker compose config`(문법)만 통과했고 **실행 검증은 아직**이다 (작성 PC의 BIOS 가상화가 꺼져 엔진 미기동). 첫 기동은 `docker compose up -d postgres neo4j`로 두 DB만 올려 확인한다 — backend 이미지는 `db` extra의 torch 때문에 빌드가 수 GB다.

Docker Desktop(WSL2)은 CPU 가상화가 켜져 있어야 한다. `systeminfo`의 "Virtualization Enabled In Firmware"가 No면 UEFI에서 SVM(AMD)/VT-x(Intel)를 켠다.
