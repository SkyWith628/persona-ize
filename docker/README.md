# docker/

- `../docker-compose.yml`: postgres(pgvector/pgvector:pg16) · neo4j:5 · backend
- `postgres/init/*.sql`: 컨테이너 최초 생성 시 1회 실행 (확장 설치). 스키마는 2주차 Alembic이 관리.

Neo4j 자격증명은 `.env`의 `NEO4J_USERNAME`/`NEO4J_PASSWORD`. 기본 비밀번호 `neo4j-dev-only`는 로컬 전용이다.

## 검증 (2026-11-08, Windows 11 + Docker Desktop 4.98 + WSL 3.0.3)

`docker compose up -d postgres neo4j` → 10초 안에 둘 다 healthy.

| 서비스 | 확인된 버전 | 확인 방법 |
|---|---|---|
| postgres | PostgreSQL 16.15, `vector` 0.8.7, `pgcrypto` 1.3 | `docker compose exec -T postgres psql -U persona -d persona_ize -c "select extname, extversion from pg_extension"` |
| neo4j | Neo4j 5.26.32, APOC 5.26.32 | 컨테이너 안 `cypher-shell`, 호스트에서 neo4j 드라이버로 `bolt://localhost:7687` 접속 |

**평소에는 두 DB만 띄운다.** backend 서비스는 `db` extra의 sentence-transformers가 torch를 끌어와 이미지가 수 GB라 아직 빌드하지 않았다(미검증). 백엔드는 호스트 venv에서 `make dev`로 실행한다.

## Windows 선행 조건 (새 PC에서 Docker Desktop이 안 뜰 때)

1. **BIOS 가상화**: `systeminfo`에 "Virtualization Enabled In Firmware: No"면 UEFI에서 SVM Mode(AMD) / Intel VT-x를 켠다. ASUS는 Advanced → CPU Configuration.
2. **Windows 기능** (관리자 PowerShell): `Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All` 과 `Microsoft-Windows-Subsystem-Linux`.
3. **WSL 본체**: `wsl --install --no-distribution` (관리자). `wsl --status`가 "설치되어 있지 않습니다"면 이 단계가 빠진 것이다. Docker Desktop 설치 프로그램은 이걸 대신해 주지 않았다.
4. 재부팅 후 Docker Desktop 실행. `docker version`에 Server가 찍히면 끝.
