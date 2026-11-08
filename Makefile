# 팀원은 이 이름만 알면 된다: make up / make test / make eval
# Windows에서 make가 없으면 scripts/*.ps1 또는 README의 명령을 쓴다.

PY ?= .venv/Scripts/python.exe
ifeq (,$(wildcard $(PY)))
PY := .venv/bin/python
endif

.PHONY: help setup venv up up-all down logs dev migrate import-synthetic demo-prepare test lint eval blind graph-dry-run

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
