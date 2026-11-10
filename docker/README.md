# docker/

| 파일 | 내용 |
|---|---|
| `../docker-compose.yml` | **개발용**: postgres(pgvector/pgvector:pg16) · neo4j:5 · backend(소스 마운트 + `--reload`) · `test`(프로필, 테스트·lint 전용) |
| `../docker-compose.prod.yml` | **운영용**: caddy(80/443) · backend · postgres · neo4j · backup. 안내는 `../docs/deploy.md` |
| `../backend/Dockerfile` | 백엔드 이미지(다단계). 타깃 `prod`(기본) / `dev` |
| `postgres/init/*.sql` | 컨테이너 최초 생성 시 1회 실행(확장 설치). 스키마는 Alembic이 관리 |
| `caddy/Caddyfile` | 운영 프록시: 자동 HTTPS, 압축, 본문 상한, HSTS, 접근 로그 |
| `backup/*.sh` | 운영 backup 컨테이너 안에서 도는 스크립트(주기 pg_dump, 복원, 헬스체크). 호스트에서는 `scripts/ops/backup.sh`·`restore.sh`가 부른다 |
| `test/entrypoint.py` | `test` 서비스 진입점: 테스트 DB가 없으면 만들고 pytest를 실행 |

Neo4j 자격증명은 `.env`의 `NEO4J_USERNAME`/`NEO4J_PASSWORD`. 기본 비밀번호 `neo4j-dev-only`는 로컬 전용이다.

## backend 이미지 (2026-11-11, 다단계)

| 타깃 | 용도 | 실행 사용자 | 들어 있는 것 |
|---|---|---|---|
| `prod` (기본) | 운영, 그리고 개발 compose의 `backend` 서비스 | `app`(uid 10001) | 의존성(`db`+`embed`+`graph`) · e5 모델 · `app/` · `migrations/` · 헬스체크 |
| `dev` | 테스트·lint (`make test-docker`, `make lint-docker`) | root | 위 + pytest·ruff + `tests/` |

- 레이어 순서: torch(CPU 휠) → 나머지 의존성 → e5 모델 → 앱 소스. **코드만 고치면 마지막 레이어만 다시 만든다.**
  의존성 목록은 `pyproject.toml`에서 뽑아 따로 깔기 때문에, 의존성 줄이 그대로면 `pyproject.toml`의 다른 줄이
  바뀌어도 pip 레이어는 캐시된다. 패키지는 pip로 설치하지 않고 `PYTHONPATH=/app`으로 쓴다.
- 측정(이 PC, 빠른 회선): 첫 빌드 **3분 33초**, 코드 한 줄 바꾼 뒤 재빌드 **3.8초**(예전에는 코드 한 줄에 torch를
  다시 깔아 2분 32초). 크기는 `docker images` 표시로 **3.02GB**(예전 2.26GB + 모델 0.49GB. 레지스트리 전송량은
  0.76GB). torch는 CPU 휠(`--index-url https://download.pytorch.org/whl/cpu`) — 기본 인덱스면 CUDA 휠로 수 GB가 더 붙는다.
- **e5 모델(`intfloat/multilingual-e5-small`, 약 470MB)은 빌드 때 이미지에 넣는다.** 런타임은 `HF_HUB_OFFLINE=1`이라
  huggingface.co에 접속하지 않는다(`--network none`에서 로딩 확인). 로그의 `Could not reach the Hub … offline mode`는
  정상이다. 모델을 바꾸려면 이미지를 다시 만든다: `docker compose build --build-arg EMBEDDING_MODEL=<모델> backend`
  (개발 compose의 `hf-cache` 볼륨은 이제 쓰이지 않는다 — 정의는 그대로 두었다).
- 시작 명령: `alembic upgrade head` → `exec uvicorn … --workers 1 --proxy-headers`. 워커가 1개인 이유는
  `../docs/deploy.md` 1장(로그인 제한·요청 제한·그래프 수집 잠금이 프로세스 메모리).
- `HEALTHCHECK`는 `/healthz`를 본다(`pz-healthcheck`, `TRUSTED_HOSTS`가 있으면 그 이름을 Host 헤더로 보낸다).
  배포 점검에는 `curl <주소>/healthz?deep=true` — Postgres·Neo4j에 실제로 붙어 보고 하나라도 실패하면 503.
- **의존성 버전은 `backend/constraints.txt`로 고정한다**(`pip install -c`). 언제 빌드해도 같은 패키지가 들어간다 —
  고정하기 전에는 같은 날 만든 두 이미지의 anthropic이 2.6.0과 2.8.0으로 달랐다. CI도 같은 파일로 깐다.
  이미지에 실제로 깔린 목록: `docker run --rm --entrypoint cat persona-ize/backend:prod /opt/pz/pip-freeze.txt`
  (Git Bash에서는 앞에 `MSYS_NO_PATHCONV=1`).

### 의존성 버전 올리기
1. `backend/constraints.txt`에서 올릴 패키지의 줄을 지우거나(그 패키지만 최신으로 깔린다) 버전을 고친다.
   `pyproject.toml`에 새 패키지를 더했을 때는 그대로 두면 된다(고정에 없는 패키지는 최신으로 깔린다).
2. 다시 빌드하고 확인한다: `docker compose --profile test build test && make test-docker`, 그리고 배포 리허설
   (`../docs/deploy.md` 14장).
3. `make lock` — 파일을 새 이미지의 설치 목록으로 다시 쓴다(머리말 주석은 남는다). 함께 커밋한다.

### 개발 흐름에서 달라진 점
`make up`(DB 두 개) + `make dev`(호스트 venv), 또는 `make up-all`(전체 컨테이너)은 그대로다. compose의 `postgres`·
`neo4j`·`backend` 정의는 바꾸지 않았다(`docker compose config` 결과가 이전과 같다). 달라진 것은 이미지뿐이다:
- 개발 backend 컨테이너도 **비루트(`app`)** 로 돈다. 컨테이너 안에서 `pip install`을 해 보려면 `docker compose exec -u root backend …`.
- 첫 기동 때 모델을 내려받지 않는다(이미지에 있다).
- `docker ps`에 backend가 `(healthy)`로 보인다(이미지의 HEALTHCHECK).

### 컨테이너에서 테스트·lint
호스트 venv를 못 쓰는 PC(Smart App Control이 `.pyd`를 막는 Windows 등)용이다.
```bash
make test-docker                                   # = docker compose --profile test run --rm test
make test-docker ARGS="tests/test_auth.py -k login"
make lint-docker
docker compose --profile test build test           # 의존성(pyproject.toml)이 바뀌었을 때 이미지 갱신
```
`test` 서비스에는 **`.env`를 넣지 않는다.** 넣으면 `DATABASE_URL`·`NEO4J_URI`·API 키가 환경변수로 들어가 테스트가 실제
개발 DB·그래프에 붙고 가져오기 테스트가 haiku를 부른다. 그래서 저장소 루트를 통째로 마운트하지 않고 필요한 폴더만
읽기 전용으로 붙이며, 통합 테스트 DB(`persona_ize_test`, 개발 postgres 서버 안)만 `TEST_DATABASE_URL`로 준다.
`make lint-docker`만은 저장소 전체를 읽기 전용으로 붙인다(CI와 같은 범위를 보려고 — ruff는 `.env`를 읽지 않는다).
검증(2026-11-11, 별도 프로젝트 이름으로): 122개 통과, Postgres 통합 테스트 건너뜀 없음. lint 164개 파일(CI와 같은 수).

## 검증 (2026-11-08, Windows 11 + Docker Desktop 4.98 + WSL 3.0.3)

`docker compose up -d postgres neo4j` → 10초 안에 둘 다 healthy.

| 서비스 | 확인된 버전 | 확인 방법 |
|---|---|---|
| postgres | PostgreSQL 16.15, `vector` 0.8.7, `pgcrypto` 1.3 | `docker compose exec -T postgres psql -U persona -d persona_ize -c "select extname, extversion from pg_extension"` |
| neo4j | Neo4j 5.26.32, APOC 5.26.32 | 컨테이너 안 `cypher-shell`, 호스트에서 neo4j 드라이버로 `bolt://localhost:7687` 접속 |

**개발은 두 DB만 띄우고 백엔드는 호스트 venv(`make dev`)** 가 빠르다. 전체 컨테이너는 `make up-all`.

2026-11-09 확인(전체 컨테이너): `/healthz`(storage=postgres, graph=True), 화면 `/`, 가져오기 3건 → 백그라운드 그래프 수집
25초 → D 첨삭 201 → 삭제(그래프 에피소드 3 포함). 컨테이너 안에서 Neo4j는 `bolt://neo4j:7687`, Postgres는
`postgres:5432`(compose environment가 .env 값을 덮어쓴다).

## Windows 선행 조건 (새 PC에서 Docker Desktop이 안 뜰 때)

1. **BIOS 가상화**: `systeminfo`에 "Virtualization Enabled In Firmware: No"면 UEFI에서 SVM Mode(AMD) / Intel VT-x를 켠다. ASUS는 Advanced → CPU Configuration.
2. **Windows 기능** (관리자 PowerShell): `Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All` 과 `Microsoft-Windows-Subsystem-Linux`.
3. **WSL 본체**: `wsl --install --no-distribution` (관리자). `wsl --status`가 "설치되어 있지 않습니다"면 이 단계가 빠진 것이다. Docker Desktop 설치 프로그램은 이걸 대신해 주지 않았다.
4. 재부팅 후 Docker Desktop 실행. `docker version`에 Server가 찍히면 끝.
