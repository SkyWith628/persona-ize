# 팀원은 이 이름만 알면 된다: make up / make test / make eval
# Windows에서 make가 없으면 scripts/*.ps1 또는 README의 명령을 쓴다.

PY ?= .venv/Scripts/python.exe
ifeq (,$(wildcard $(PY)))
PY := .venv/bin/python
endif

# 운영 구성(docker-compose.prod.yml)에 쓰는 env 파일. 다른 파일: make prod-up PROD_ENV=/path/to/env
PROD_ENV ?= .env.production
PROD_COMPOSE = PZ_ENV_FILE=$(PROD_ENV) docker compose --env-file $(PROD_ENV) -f docker-compose.prod.yml
# 스모크 테스트는 표준 라이브러리만 쓰므로 venv가 아니라 시스템 파이썬으로 돈다.
SMOKE_PY ?= python3
BASE_URL ?= https://localhost

.PHONY: help setup venv up up-all down logs dev migrate import-synthetic demo-prepare test lint eval blind graph-dry-run usage-report graph-reset housekeeping \
	test-docker lint-docker prod-config prod-up prod-down prod-logs prod-ps backup smoke

help:
	@echo "make setup     - .env.example → .env (최초 1회)"
	@echo "make venv      - 가상환경 생성 + 백엔드 설치"
	@echo "make up        - PostgreSQL(pgvector), Neo4j 컨테이너 기동"
	@echo "make up-all    - 위 + backend 컨테이너(이미지 빌드, 마이그레이션 뒤 기동) → http://localhost:8000"
	@echo "make migrate   - Alembic으로 DB 스키마를 최신으로"
	@echo "make down      - 컨테이너 종료"
	@echo "make dev       - 로컬에서 백엔드만 실행 (http://localhost:8000/docs)"
	@echo "make test      - 백엔드 테스트"
	@echo "make lint      - ruff"
	@echo "make import-synthetic - data/synthetic 합성 대화를 DB에 적재 (헤더 스텁 user=demo)"
	@echo "make demo-prepare - 시연 계정(demo)·페르소나·합성 대화·그래프 준비 (docs/demo-script.md)"
	@echo "make eval      - evals/eval_cases.csv 로 A/B/C 실행, 결과 CSV 저장"
	@echo "make blind RESULTS=evals/results/<결과>.csv - 블라인드 채점 시트 생성 (evals/blind/)"
	@echo "make graph-dry-run - 그래프 스파이크 변환만 확인 (네트워크 없음)"
	@echo "make usage-report - 사용자·일·조건별 첨삭 호출 수·토큰 (최근 7일)"
	@echo "make graph-reset USER=demo [ARGS=--dry-run] - 그 사용자의 그래프를 지우고 다시 수집 (확인 질문 있음)"
	@echo "make housekeeping - 만료 세션·오래된 작업·사용량 카운터 정리"
	@echo ""
	@echo "호스트 venv를 못 쓰는 PC (컨테이너에서, .env를 넣지 않는다 — 실제 DB·API를 건드리지 않음)"
	@echo "make test-docker [ARGS='tests/test_auth.py -k login'] - 백엔드 테스트 (Postgres 통합 테스트 포함)"
	@echo "make lint-docker   - ruff (저장소 전체)"
	@echo ""
	@echo "운영 (docs/deploy.md, 값은 .env.production — 다른 파일은 PROD_ENV=...)"
	@echo "make prod-config   - docker-compose.prod.yml + env 파일 검증 (빠진 필수 값이 있으면 실패)"
	@echo "make prod-up       - 이미지 빌드 → 기동 → 전 서비스 healthy까지 대기 (백업·스모크까지 하려면 scripts/ops/deploy.sh)"
	@echo "make prod-down     - 컨테이너만 내린다. 데이터(볼륨)는 남는다"
	@echo "make prod-logs [SERVICE=backend] - 로그 따라 보기"
	@echo "make prod-ps       - 서비스 상태"
	@echo "make backup        - 지금 PostgreSQL 백업 1회 (./backups, 보존 정리 포함)"
	@echo "make smoke BASE_URL=https://<도메인> [ARGS='--no-import'] - 배포 스모크 테스트 (실제 모델이면 호출 비용 발생)"

setup:
	@test -f .env || (cp .env.example .env && echo ".env 생성됨 — 키와 비밀번호를 채우세요")

venv:
	py -3.14 -m venv .venv || python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "backend[dev,db,embed]"

up: setup
	docker compose up -d postgres neo4j

up-all: setup
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f backend

dev:
	cd backend && ../$(PY) -m uvicorn app.main:app --reload --port 8000

migrate:
	cd backend && ../$(PY) -m alembic upgrade head

import-synthetic:
	$(PY) scripts/import_synthetic.py --user demo

demo-prepare:
	$(PY) scripts/prepare_demo.py --graph

test:
	cd backend && ../$(PY) -m pytest -q

lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

eval:
	$(PY) evals/run_eval.py --cases evals/eval_cases.csv --conditions A,B,C

blind:
	$(PY) scripts/make_blind_sheet.py build $(RESULTS)

graph-dry-run:
	cd graph/graphiti_p0 && ../../$(PY) ingest.py --dry-run

usage-report:
	$(PY) scripts/usage_report.py

graph-reset:
	$(PY) scripts/graph_reset.py --user $(USER) $(ARGS)

housekeeping:
	$(PY) scripts/housekeeping.py

# ---- 컨테이너에서 테스트·lint (docker-compose.yml의 test 서비스, 이미지 backend/Dockerfile의 dev 타깃) ----
# 의존성(pyproject.toml)이 바뀌면 이미지를 다시 만든다: docker compose --profile test build test
test-docker:
	docker compose --profile test run --rm test $(if $(ARGS),pytest -q -rs -p no:cacheprovider $(ARGS))

# lint는 저장소 전체를 읽기 전용으로 붙여 CI와 같은 범위(문서의 코드 블록 포함)를 본다. ruff는 .env를 읽지 않는다.
lint-docker:
	docker compose --profile test run --rm --no-deps -v "$(CURDIR):/lint:ro" -w /lint test sh -c "ruff check . && ruff format --check ."

# ---- 운영 (docker-compose.prod.yml). 안내는 docs/deploy.md ----
prod-config:
	$(PROD_COMPOSE) config --quiet && echo "docker-compose.prod.yml 설정 OK ($(PROD_ENV))"

prod-up:
	$(PROD_COMPOSE) up -d --build --wait --wait-timeout 300

# 볼륨(-v)은 지우지 않는다 — DB·그래프·인증서가 거기 있다.
prod-down:
	$(PROD_COMPOSE) down

prod-logs:
	$(PROD_COMPOSE) logs -f --tail 100 $(SERVICE)

prod-ps:
	$(PROD_COMPOSE) ps

backup:
	PZ_ENV_FILE=$(PROD_ENV) bash scripts/ops/backup.sh

smoke:
	$(SMOKE_PY) scripts/smoke_test.py --base-url $(BASE_URL) $(ARGS)
