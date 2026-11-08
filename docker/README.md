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

**개발은 두 DB만 띄우고 백엔드는 호스트 venv(`make dev`)** 가 빠르다. 전체 컨테이너는 `make up-all`.

### backend 이미지 (2026-11-09 검증)
- `docker compose build backend` 2분 32초, 이미지 **2.26GB**. torch는 CPU 휠(`--index-url https://download.pytorch.org/whl/cpu`)로 깐다 — 기본 인덱스면 CUDA 휠로 수 GB가 더 붙는다. `db`+`graph` extra 모두 설치(그래프 D 경로 동작).
- 컨테이너 시작 때 `alembic upgrade head` → uvicorn. e5 모델(~470MB)은 첫 기동 때 내려받고 `hf-cache` 볼륨에 남는다(워밍업 완료 로그 `embedder warmed up`까지 약 30초).
- 점검: `curl localhost:8000/healthz?deep=true` — Postgres·Neo4j에 실제로 붙어 보고 하나라도 실패하면 503 `degraded`(`checks` 항목별). 컨테이너 헬스체크나 배포 점검에 이걸 쓴다.
- 확인한 것: `/healthz`(storage=postgres, graph=True), 화면 `/`, 가져오기 3건 → 백그라운드 그래프 수집 25초 → D 첨삭 201 → 삭제(그래프 에피소드 3 포함). 컨테이너 안에서 Neo4j는 `bolt://neo4j:7687`, Postgres는 `postgres:5432`(compose environment가 .env 값을 덮어쓴다).

## Windows 선행 조건 (새 PC에서 Docker Desktop이 안 뜰 때)

1. **BIOS 가상화**: `systeminfo`에 "Virtualization Enabled In Firmware: No"면 UEFI에서 SVM Mode(AMD) / Intel VT-x를 켠다. ASUS는 Advanced → CPU Configuration.
2. **Windows 기능** (관리자 PowerShell): `Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All` 과 `Microsoft-Windows-Subsystem-Linux`.
3. **WSL 본체**: `wsl --install --no-distribution` (관리자). `wsl --status`가 "설치되어 있지 않습니다"면 이 단계가 빠진 것이다. Docker Desktop 설치 프로그램은 이걸 대신해 주지 않았다.
4. 재부팅 후 Docker Desktop 실행. `docker version`에 Server가 찍히면 끝.
