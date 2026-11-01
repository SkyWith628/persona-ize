.PHONY: setup up down logs test eval

setup:  ## 최초 1회: .env 만들기
	@test -f .env || (cp .env.example .env && echo ".env 생성됨 — 비밀번호 값을 확인하세요")

up: setup  ## 전체 환경 실행 (Postgres, Neo4j, backend)
	docker compose up -d --build
	@echo "백엔드 http://localhost:8000/health | Neo4j Browser http://localhost:7474"

down:  ## 중지 (데이터 유지)
	docker compose down

logs:
	docker compose logs -f --tail=100

test:  ## 백엔드 테스트 (컨테이너 안에서 실행)
	docker compose run --rm --no-deps backend python -m pytest -q

eval:  ## 평가 실행 (구현 예정: evals/ 하니스)
	@echo "아직 구현 전입니다. evals/README.md 참고"
