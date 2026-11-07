# 팀원은 이 이름만 알면 된다: make up / make test / make eval
# Windows에서 make가 없으면 scripts/*.ps1 또는 README의 명령을 쓴다.

PY ?= .venv/Scripts/python.exe
ifeq (,$(wildcard $(PY)))
PY := .venv/bin/python
endif

.PHONY: help venv up down logs dev test lint eval graph-dry-run

help:
	@echo "make venv      - 가상환경 생성 + 백엔드 설치"
	@echo "make up        - PostgreSQL(pgvector), Neo4j, 백엔드 컨테이너 기동"
	@echo "make down      - 컨테이너 종료"
	@echo "make dev       - 로컬에서 백엔드만 실행 (http://localhost:8000/docs)"
	@echo "make test      - 백엔드 테스트"
	@echo "make lint      - ruff"
	@echo "make eval      - evals/eval_cases.csv 로 A/B 실행, 결과 CSV 저장"
	@echo "make graph-dry-run - 그래프 스파이크 변환만 확인 (네트워크 없음)"

venv:
	py -3.14 -m venv .venv || python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "backend[dev]"

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f backend

dev:
	cd backend && ../$(PY) -m uvicorn app.main:app --reload --port 8000

test:
	cd backend && ../$(PY) -m pytest -q

lint:
	cd backend && ../$(PY) -m ruff check .

eval:
	$(PY) evals/run_eval.py --cases evals/eval_cases.csv --conditions A,B

graph-dry-run:
	cd graph/graphiti_p0 && ../../$(PY) ingest.py --dry-run
