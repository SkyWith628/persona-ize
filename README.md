# Persona_ize

상대·상황에 맞는 **사용자 본인의 말투**로 메시지를 첨삭하되, **원문의 사실과 의도를 보존**하는 서비스.
공모전(2026-12) + 포트폴리오. 설계: [docs/project-design.md](docs/project-design.md) · 그래프 실험: [docs/design.md](docs/design.md) · 결정: [docs/decisions.md](docs/decisions.md) · API: [docs/api.md](docs/api.md) · **배포: [docs/deploy.md](docs/deploy.md)**

```
상대 선택 + 초안 + 상황
  → 의도·보존 항목 추출 → (검색: 페르소나 / 본인 과거 표현 / 그래프 맥락)
  → Claude 첨삭(구조화 출력) → 규칙 검증 → 실패 시 재생성 1회 → 재실패 시 원문 유지
  → 추천 표시 → 사용자가 적용·수정·취소 (자동 전송 없음)
```

실험 조건: **A** 일반 첨삭 / **B** +상대별 페르소나 / **C** +벡터 검색한 본인 과거 표현 / **D** +그래프 검색 맥락.

## 빠른 시작

```bash
cp .env.example .env            # LLM_PROVIDER=fake면 키 없이 동작
py -3.14 -m venv .venv          # 맥: python3 -m venv .venv
.venv/Scripts/python.exe -m pip install -e "backend[dev,db,embed]"   # 그래프(D)까지: backend[dev,db,embed,graph]
make up                          # PostgreSQL(pgvector)·Neo4j 컨테이너 (Docker Desktop 필요)
make migrate                     # DB 스키마 최신으로
make import-synthetic            # 합성 대화 적재 (user=demo). 그래프까지: scripts/import_synthetic.py --graph
make test                        # 백엔드 테스트 (Postgres 없으면 통합 테스트는 skip)
make dev                         # 화면 http://localhost:8000 · Swagger /docs
```

인메모리로만 빠르게 보려면 `.env`의 `DATABASE_URL`을 비운다 (C/D 검색은 가져온 대화를 메모리에 둔다). API 상세: [docs/api.md](docs/api.md).

실제 Claude를 쓰려면 `.env`에 `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, `LLM_MODEL`을 채운다. Console에서 사용량 한도를 먼저 건다.

Docker로 전체(PostgreSQL+pgvector, Neo4j, 백엔드): `make up-all` → 화면 http://localhost:8000 · Swagger `/docs` · 운영 대시보드 `/admin`(`.env`의 `ADMIN_USERNAMES`에 내 아이디) · Neo4j Browser http://localhost:7474
호스트 파이썬을 못 쓰는 PC는 테스트·lint도 컨테이너로: `make test-docker` · `make lint-docker`.

### 배포
리눅스 VM 한 대에 Docker Compose로 올린다(Caddy 자동 HTTPS → 백엔드 → PostgreSQL·Neo4j, 주기 백업 포함). 순서와 점검표는 [docs/deploy.md](docs/deploy.md).
```bash
cp .env.production.example .env.production   # 도메인·비밀번호·초대 코드·관리자 아이디·API 키·비용 상한을 채운다
scripts/ops/deploy.sh                        # 백업 → 이미지 빌드 → 기동 → 스모크 테스트
scripts/ops/run.sh scripts/reset_password.py --username <관리자 아이디> --create
```
로컬에서 운영 구성을 연습하는 법은 deploy.md 14장(가짜 모델, `https://localhost`).

### 수직 기능 시험 (상대 1 · 페르소나 1 · 원문 1)

```bash
curl -s -X POST localhost:8000/v1/contacts -H "X-User-Id: me" -H "Content-Type: application/json" -d '{"alias":"박지현","relationship_type":"superior"}'
```
```bash
curl -s -X PUT localhost:8000/v1/contacts/<id>/persona -H "X-User-Id: me" -H "Content-Type: application/json" -d '{"settings":{"formality":5,"address_term":"팀장님","forbidden_phrases":["ㅋㅋ"]},"expected_version":0}'
```
```bash
curl -s -X POST localhost:8000/v1/rewrites -H "X-User-Id: me" -H "Idempotency-Key: t1" -H "Content-Type: application/json" -d '{"contact_id":"<id>","draft_text":"오늘 정리해서 내일 오전까지 보내드릴게요.","situation":"업무 보고","condition":"B"}'
```

## 폴더 (담당)

| 폴더 | 담당 | 내용 |
|---|---|---|
| `backend/` | 팀장 | FastAPI · 워크플로 · 검증 · 저장소. [app/prompts/](backend/app/prompts) 는 비전공자도 수정 |
| `evals/` | 평가·품질 | 사례 CSV, 보존 규칙, 루브릭, 실행 하니스 |
| `data/synthetic/` | 사용자·시나리오 | 합성 과거 대화 JSON |
| `graph/` | 지식그래프 | 스키마, 호칭 병합·질의 정답, Graphiti 스파이크 |
| `docs/` | 전원 | 설계·결정·일정 |
| `backend/app/web/` | 팀장 | PC 웹 앱(빌드 없는 ES 모듈, 서버 `/`에서 서빙)·운영 대시보드(`/admin`)·시연 프로토타입(`/static/proto.html`). `apps/web/`(React)는 보류 |
| `apps/ios/` | 팀장 | 아이폰 앱(SwiftUI)·보조 키보드 확장. 설치는 `apps/ios/README.md`, 화면 설계는 `docs/iphone-mobile.md` |
| `docker/`, `scripts/ops/` | 팀장 | 운영 구성(Caddy·백업)과 배포·복원 스크립트 |

협업 규칙(GitHub Desktop, PR, 폴더 소유): [CONTRIBUTING.md](CONTRIBUTING.md) · 역할·일정: [docs/team-roles-and-schedule.md](docs/team-roles-and-schedule.md) · **역할별 플레이북**: [시나리오](docs/roles/scenario.md) · [평가·품질](docs/roles/evaluation.md) · [지식그래프](docs/roles/graph.md) · **평가 사례·합성 대화 쓰는 법: [docs/dataset-guide.md](docs/dataset-guide.md)**

## 명령어

| 명령 | 내용 |
|---|---|
| `make test` | 백엔드 테스트 |
| `make lint` | ruff |
| `make test-docker` · `make lint-docker` | 위 둘을 컨테이너에서 (호스트 venv를 못 쓰는 PC) |
| `scripts/ops/deploy.sh` · `make prod-ps` · `make backup` | 운영 배포·상태·백업 ([docs/deploy.md](docs/deploy.md)) |
| `python scripts/smoke_test.py --base-url <주소>` | 떠 있는 서버의 전 흐름 점검(가입 → 첨삭 → 계정 삭제) |
| `make eval` | 평가 사례 A/B 실행 → `evals/results/` |
| `make graph-dry-run` | 그래프 스파이크 변환만 확인 |
| `python scripts/validate_data_files.py` | CSV/JSON 접점 파일 형식 검사 |
| `make usage-report` | 사용자·일(KST)·조건별 첨삭 호출 수·토큰 (최근 7일, `scripts/usage_report.py --help`) |
| `make graph-reset USER=demo` | 그 사용자의 그래프를 지우고 다시 수집 (`--dry-run`으로 먼저 확인, haiku 비용) |
| `make housekeeping` | 만료·폐기 세션, 오래된 가져오기 작업·사용량 카운터 정리 |
| `curl localhost:8000/healthz?deep=true` | Postgres·Neo4j 연결까지 확인 (문제면 503) |

Windows에 `make`가 없으면 Makefile의 명령을 그대로 실행한다.

## 상태 (2026-11-11)
- 저장소: `github.com/SkyWith628/persona-ize` (비공개).
- 백엔드: 상대·페르소나, 첨삭 워크플로(의도·보존 추출 → 검색 → 프롬프트 → 검증 → 재생성 1회 → 원문 유지), PostgreSQL(Alembic), C(pgvector 문체 kNN)·D(Graphiti 그래프 맥락) 검색, 삭제(대화방·상대·계정), 사용량 제어(사용자·전체), 세션 쿠키·Bearer 인증, 대화 가져오기(카카오톡·텔레그램·붙여넣기, 개인정보 패턴 가림, 그래프 수집 상한), 운영 대시보드 API.
- 화면: PC 웹 앱(첫 화면·동의, 첨삭, 상대·페르소나, 대화 가져오기, 이력, 내 데이터, 설정), 운영 대시보드, 안내 문서 초안. 아이폰 앱·보조 키보드는 `apps/ios/`(시뮬레이터까지 확인, 실기기 미검증).
- 배포: 운영 모드 검사·보안 헤더·요청 제한, 운영용 Docker 구성(Caddy HTTPS, 백업), 배포·복원 스크립트, 스모크 테스트. 로컬 리허설(가짜 모델)까지 확인 — 실제 서버·실제 모델 확인은 [docs/backlog.md](docs/backlog.md) A2·A3.
- 실측: Opus 5.5로 5사례 × A/B/C/D ([2주차 메모](evals/results/2026-11-08-week2-abc-run.md), [3주차 메모](evals/results/2026-11-08-week3-abcd-run.md)). 1주차 그래프 스파이크: [graph/graph-spike-report.md](graph/graph-spike-report.md).
- 시연: `make demo-prepare` 뒤 [docs/demo-script.md](docs/demo-script.md) (demo 계정, 3장면, 보험).
- 다음: [docs/backlog.md](docs/backlog.md) — 팀장 단독 항목(A), 팀원 산출물 대기(B), 보류(C).
