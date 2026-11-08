# Persona_ize (앱 저장소)

mode: work

상대·상황에 맞는 사용자 말투로 메시지를 첨삭하되 원문의 사실과 의도를 보존하는 서비스. 공모전(2026-12) + 포폴.
설계 원본과 팀 운영 문서는 iCloud `WorkSpace_M/projects/persona-ize/` — 이 저장소의 `docs/`는 그 복사본이다. 충돌하면 `docs/project-design.md`를 따른다.

## 명령어 (저장소 루트, Windows는 `.venv/Scripts/python.exe`, 맥은 `.venv/bin/python`)
- 테스트: `cd backend && ../.venv/Scripts/python.exe -m pytest -q`
- lint(저장소 전체, 루트 `ruff.toml`): `.venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m ruff format --check .`
- 접점 파일 검사: `.venv/Scripts/python.exe scripts/validate_data_files.py`
- 평가: `.venv/Scripts/python.exe evals/run_eval.py --conditions A,B [--provider anthropic --repeat 3]`
- 그래프 스파이크: `cd graph/graphiti_p0 && ../../.venv/Scripts/python.exe -m pytest -q` / `ingest.py --dry-run` / `--limit 5`
- 서버: `cd backend && ../.venv/Scripts/python.exe -m uvicorn app.main:app --reload`
- Windows 콘솔에서 한글 깨지면 `PYTHONUTF8=1`.

완료 기준: 위 테스트·lint 통과 + 동작 변경에는 테스트 동반. "됐다"고 말하기 전에 실제로 실행한다.

## 구조
- `backend/app/ai/` 워크플로: `preserve.py`(보존 항목 규칙) → `prompt.py`(prompts/*.md 조립) → `providers/`(fake | anthropic) → `validation.py` → `workflow.py`(재생성 1회, 실패 시 원문 유지)
- `backend/app/ai/retrieval/` 조건별 검색기. 지금은 `NullRetriever`만(A/B). C=pgvector(2주차), D=Graphiti(3주차)
- `repositories/memory.py` 인메모리. 2주차에 PostgreSQL로 교체 (인터페이스 유지)
- `api/deps.py` 인증은 `X-User-Id` 헤더 스텁. 본문의 user_id는 절대 신뢰하지 않는다
- `graph/graphiti_p0/` 스파이크. `data/synthetic/`의 JSON을 읽는다

## 컨벤션
- LLM: **Claude** (`anthropic` SDK, `messages.parse` 구조화 출력, `output_config.effort`). OpenAI 코드 추가 금지. 임베딩은 로컬 sentence-transformers(기본) 또는 Voyage
- 프롬프트는 코드가 아니라 `backend/app/prompts/<version>/*.md`. 버전 올릴 때 폴더를 복사해 새 이름으로
- 사실 보존 실패는 다른 점수로 상쇄하지 않는다. 검증 실패 → 원문 유지, 자동 전송 없음
- 합성 데이터만 외부(Claude API, Neo4j)에 보낸다
- 결정 변경은 `docs/decisions.md`에 5줄 이내로

## 함정
- `ruff format`을 돌린 뒤 테스트. line-length 110
- 보존 항목 추출은 규칙 기반이라 오탐이 있다. 규칙을 바꾸면 `evals/preserve_rules.md`도 같이 고친다
- fake 프로바이더는 원문을 그대로 돌려준다 → 검증은 항상 통과. 실제 품질은 `--provider anthropic`으로만 본다. 2026-11-08 실측(5사례×A/B, Opus·Sonnet 5.5)은 `evals/results/2026-11-08-first-real-run.md`
- `.env`의 `LLM_PROVIDER=fake`여도 `run_eval.py --provider anthropic`은 실제 호출한다 (인자가 .env를 덮어쓴다). 서버(`make dev`)로 실제 호출을 보려면 `.env`를 `anthropic`으로
- graphiti-core 0.31.2 `AnthropicClient`는 **강제 tool_choice**를 쓰고 5.5 계열이 이를 400으로 거부한다 → 그래프 추출 기본은 `claude-haiku-4-5`. 리랭커는 `PassthroughReranker`(기본 리랭커는 OpenAI 호출). 모델별 max_tokens 표도 구버전. anthropic SDK 2.x에 없는 `temperature`를 넘기므로 `GraphAnthropicClient`가 떼어낸다
- Aura(`neo4j+s://`) 접속 시 `self-signed certificate` 오류는 Windows 루트 저장소에 SSL.com 루트가 없어서다 → `load_settings()`가 certifi 번들을 `SSL_CERT_FILE`로 지정한다. `--limit 5` 실측(2026-11-08)은 `graph/graph-spike-report.md`
- 원격: `github.com/SkyWith628/persona-ize`(비공개). `main` 직접 푸시는 팀장만, 나머지는 PR. 11/04 이전 원격 뼈대(`spikes/`)는 `graph/graphiti_p0/`로 대체됨
- 평가셋 열 이름은 `docs/dataset-guide.md`가 기준. `evals/run_eval.py`·`scripts/validate_data_files.py`가 그 열을 읽으므로 열을 바꾸면 셋을 같이 고친다
- 이 Windows 머신: Docker Desktop 4.98은 2026-11-08 설치했으나 **BIOS의 SVM Mode가 꺼져 있어 엔진이 못 뜬다**(ASUS TUF B550M-PLUS: Advanced → CPU Configuration → SVM Mode). 재부팅+SVM 활성화 전까지 compose는 `config`(문법)만 통과한 상태. node·gh는 없다. docker는 새 셸에서만 PATH에 잡힌다
- API 비용은 구독과 별개. Console 사용량 한도 → `--limit 5` → 전체 순서
