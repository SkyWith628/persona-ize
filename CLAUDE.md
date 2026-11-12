# Persona_ize (앱 저장소)

mode: work

상대·상황에 맞는 사용자 말투로 메시지를 첨삭하되 원문의 사실과 의도를 보존하는 서비스. 공모전(2026-12) + 포폴.
설계 원본과 팀 운영 문서는 iCloud `WorkSpace_M/projects/persona-ize/` — 이 저장소의 `docs/`는 그 복사본이다. 충돌하면 `docs/project-design.md`를 따른다.

## 명령어 (저장소 루트, Windows는 `.venv/Scripts/python.exe`, 맥은 `.venv/bin/python`)
- DB 기동·마이그레이션: `docker compose up -d postgres neo4j` → `cd backend && ../.venv/Scripts/python.exe -m alembic upgrade head` (`make up`, `make migrate`)
- 테스트: `cd backend && ../.venv/Scripts/python.exe -m pytest -q` — Postgres 통합 테스트(`test_repos_postgres.py`)는 `.env`의 DATABASE_URL과 같은 서버에 `persona_ize_test` DB를 만들어 돌고, 접속이 안 되면 건너뛴다
- lint(저장소 전체, 루트 `ruff.toml`): `.venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m ruff format --check .`
- 접점 파일 검사: `.venv/Scripts/python.exe scripts/validate_data_files.py`
- 합성 대화 적재(멱등): `.venv/Scripts/python.exe scripts/import_synthetic.py --user demo [--graph]` (DATABASE_URL 필요, 첫 실행에 e5 모델 다운로드. `--graph`는 Neo4j에 haiku로 수집, 메시지당 10~15초)
- 평가: `.venv/Scripts/python.exe evals/run_eval.py --conditions A,B,C,D [--provider anthropic --repeat 3 --embedding fake]` — C/D는 `--dataset`을 `--user eval`로 적재한 뒤 검색하고 `recall`(relevant_msg_ids 대비) 열을 채운다. D는 그래프 수집까지 한다(처음 한 번만 느리다)
- 그래프 스파이크: `cd graph/graphiti_p0 && ../../.venv/Scripts/python.exe -m pytest -q` / `ingest.py --dry-run` / `--limit 5`
- 서버: `cd backend && ../.venv/Scripts/python.exe -m uvicorn app.main:app --reload`
- 운영: `scripts/usage_report.py [--days 7 --user <id> --csv out.csv]`(사용자·일 KST·조건별 호출·토큰), `scripts/graph_reset.py --user <username|id> [--conversation c1] [--dry-run|--yes] [--no-reingest]`(그래프 지우고 재수집, haiku 비용), `scripts/housekeeping.py`(만료 세션·오래된 작업·카운터). 깊은 헬스체크 `GET /healthz?deep=true`(503이면 DB/Neo4j 문제)
- 호스트 venv를 못 쓰는 PC(아래 함정의 Smart App Control): `make test-docker [ARGS="tests/test_auth.py -k login"]` · `make lint-docker` — `test` 프로필 컨테이너(이미지 `dev` 타깃)에서 돈다. **test 서비스에는 `.env`를 넣지 않는다**
- 화면 단위 테스트(node 22, 이 PC에는 node가 없어 Docker로): `MSYS_NO_PATHCONV=1 docker run --rm -v "<저장소>:/repo" -w /repo node:22-alpine node --experimental-default-type=module --test "backend/app/web/tests/*.test.js"` (디렉터리 인자는 실패한다 — 글롭으로)
- 운영 배포([docs/deploy.md](docs/deploy.md)): `scripts/ops/deploy.sh`(백업 → 빌드 → 기동 → 스모크) · `rollback.sh` · `backup.sh` · `restore.sh` · `run.sh scripts/<스크립트>.py`(운영 스택 안에서 실행). `make prod-config|prod-up|prod-down|prod-logs|prod-ps|backup|smoke`. 스모크만: `python scripts/smoke_test.py --base-url https://<주소> [--insecure --invite-code …]`
- 계정: `scripts/reset_password.py --username <이름> [--create]` — 비밀번호 재설정(세션 전부 폐기). 운영에서 관리자 계정은 `--create`로만 만든다
- 의존성 버전 고정: `backend/constraints.txt`(Dockerfile·CI가 `pip -c`). 올릴 때는 줄을 지우고 빌드 → 테스트 → `make lock` (`docker/README.md`)
- Windows 콘솔에서 한글 깨지면 `PYTHONUTF8=1`. Git Bash의 curl은 한글 JSON 본문을 CP949로 보내 400이 난다 → `--data-binary @파일`

완료 기준: 위 테스트·lint 통과 + 동작 변경에는 테스트 동반. "됐다"고 말하기 전에 실제로 실행한다.

## 구조
- `backend/app/ai/` 워크플로: `preserve.py`(보존 항목 규칙) → `prompt.py`(prompts/*.md 조립) → `providers/`(fake | anthropic) → `validation.py` → `workflow.py`(재생성 1회, 실패 시 원문 유지)
- `backend/app/ai/retrieval/` 조건별 검색기. A/B `NullRetriever`, C `vector.py VectorStyleRetriever`(같은 상대에게 쓴 본인 발화 묶음 kNN, 저장소가 벡터 검색 담당), D `composite.py` = C + `graph/retriever.py GraphContextRetriever`. NEO4J_URI가 비면 D = C
- `backend/app/graph/` D 경로. `store.py` Protocol(테스트는 FakeGraphStore) ← `GraphitiStore`; `ingest.py`가 미수집 메시지를 **사용자별 잠금 아래 전체 시각 순**으로 `add_episode`(group_id=user_id, `messages.graph_ingested_at`으로 멱등; 잠금은 프로세스 안에서만); `retriever.py`(v2)는 상대 노드 중심 검색 → 상대·연결 사건 노드 엣지만 → as_of 이후 출처·무효화·같은 출처 중복 제외 → e5 유사도+최근성+사건 유형·중요도로 재정렬(상수 표), `purpose=context`. `schema.py`·`dates.py`는 스파이크 `graph/graphiti_p0/p0`의 복사본이며 **백엔드 쪽이 정본**(스파이크는 동결)
- 가져오기: `services/import_service.py`(화자 지정 `speaker_map`, 해시 중복, 대화방 단위 재색인) → `ai/episodes.py`(본인 발화 연속 구간 + 앞 맥락 최대 window-1개 = 묶음 1개, 문체 예시는 본인 발화만) → `ai/embeddings.py`(local e5-small `query:`/`passage:` 접두, fake는 테스트용). `services/synthetic_loader.py`가 data/synthetic JSON을 적재
- `repositories/base.py` Protocol(비동기) ← `postgres.py`(기본, SQLAlchemy 2 + asyncpg) / `memory.py`(테스트·DATABASE_URL 비었을 때). `db/models.py` 테이블, `db/session.py` 엔진, 스키마는 `backend/migrations/`(Alembic)
- `api/deps.py build_state`가 DATABASE_URL 유무로 저장소를 고른다. `/healthz`의 `storage`로 확인
- 사용량 제어: `repositories/usage.py`(사용자·일 KST 카운터, Postgres는 원자적 UPSERT) → `RewriteService`가 모델 호출 전 요청 수를 예약하고 호출 뒤 토큰을 더한다. 한도는 `USAGE_DAILY_*`, 초과는 429 `usage_exceeded`, 조회는 `GET /v1/usage`(멱등 재요청은 세지 않음)
- 인증(`api/deps.py current_user_id`·`current_session`): `Authorization: Bearer`(비브라우저용, `POST /v1/auth/token`, CSRF 불필요 — Bearer가 틀리면 쿠키로 넘어가지 않고 401) → 세션 쿠키 `pi_session`(HttpOnly, SameSite=Lax, 쓰기 요청은 `X-CSRF-Token` 필수) → `AUTH_ALLOW_HEADER_STUB`일 때만 `X-User-Id` 헤더(개발·테스트·평가용, 운영은 거부). 본문의 user_id는 절대 신뢰하지 않는다. 비밀번호는 stdlib scrypt(`core/security.py`), 세션 토큰은 해시만 저장. 공개 가입은 `AuthService.signup`(초대 코드·동의·운영 모드의 관리자 이름 차단), `register`·`create_account`는 스크립트용 안쪽 단계. 인증 스키마는 `api/schemas_auth.py`. 관리자 판정은 `Settings.is_admin_name` 한 곳(대소문자까지 일치)
- 플랫폼(`core/`): `guard.py` 운영 모드 검사(`APP_ENV=production`에서 위험 설정이면 시작 거부) · `middleware.py` 순수 ASGI 미들웨어(요청 id·접근 로그·보안 헤더·CORS·500 통일·Host 검사·요청 제한·본문 상한, 순서는 파일 머리말) · `http_errors.py` 오류 모양(최상위 `code`·`message`, 예전 `detail`도 유지) · `ratelimit.py` IP 요청 제한 · `logs.py` 요청 id·`LOG_LEVEL` · `periodic.py` 주기 작업(정리 + 그래프 미수집분 이어받기, `HOUSEKEEPING_INTERVAL_HOURS`)
- 가져오기 파서(`importers/`): 형식 감지 + 카카오톡(.txt 안드로이드·iOS·PC, .csv)·텔레그램 `result.json`·붙여넣기 → 공통 결과 → `ImportService`. 개인정보 패턴 가림(`pii.py`)은 **API 경로에만**(평가·합성 적재 스크립트 결과는 그대로). 카카오톡은 합성 픽스처 기준 — 실물 샘플 검증 전
- 관리자: `api/routes/admin.py`(접근 검사 401 → 403 → 503) → `services/admin_service.py`(기간·비율·비용·id 줄이기, 순수 함수) → `repositories/admin.py`(집계 SQL, PostgreSQL 전용). 화면 `web/admin.html` + `css/admin.css` + `js/admin.js` + `js/admin/*`
- `backend/app/web/` PC 웹 앱(빌드 없는 ES 모듈, 해시 라우팅 `#/compose`·`#/contacts`·`#/import`·`#/history`·`#/data`·`#/settings`): `index.html`(셸) + `css/tokens.css`(디자인 토큰, 모든 화면이 먼저 불러온다)·`css/app*.css` + `js/{main,api,session,store}.js`·`js/lib/`(순수 로직)·`js/ui/`(부품)·`js/views/`(화면). 안내 문서 `privacy.html`·`terms.html`(`/privacy`·`/terms`). 테스트는 `web/tests/*.test.js`(HTTP로는 안 열린다). `proto.*`는 시연 프로토타입(모바일 쪽이 고친다 — 건드리지 않는다). `apps/web/`(React)는 보류, `apps/ios/`는 아이폰 앱·보조 키보드(11/12 병합, PR에서만 `ios.yml`이 macOS 러너로 빌드)
- 운영 구성: `backend/Dockerfile`(다단계: `prod` 비루트·e5 모델 내장·오프라인 / `dev` pytest·ruff) · `docker-compose.prod.yml`(caddy만 80/443 공개, 나머지는 내부 네트워크) · `docker/caddy/Caddyfile` · `docker/backup/` · `scripts/ops/` · `.env.production.example`
- `graph/graphiti_p0/` 스파이크. `data/synthetic/`의 JSON을 읽는다

## 컨벤션
- LLM: **Claude** (`anthropic` SDK, `messages.parse` 구조화 출력, `output_config.effort`). OpenAI 코드 추가 금지. 임베딩은 로컬 sentence-transformers(기본) 또는 Voyage
- 프롬프트는 코드가 아니라 `backend/app/prompts/<version>/*.md`. 버전 올릴 때 폴더를 복사해 새 이름으로. 기본은 `rewrite-v3`(v2의 호칭 규칙 + `[오늘]` 날짜와 날짜 대조 규칙, 11-10). v1·v2는 비교용으로 남긴다. 평가는 `run_eval.py --prompt rewrite-v2`로 옛 버전 재현. 프롬프트에 새 값을 넣을 때는 `build_prompt`에 인자를 더하고 새 버전의 user.md에만 자리표시자를 둔다(옛 버전 본문은 그대로)
- 호칭은 **페르소나 설정**이 공급한다. `synthetic_loader.persona_from_aliases`가 상사·지인·친구·가족·연인에 characters.csv 호칭을 넣고 동료는 비운다 — 모델은 설정에 없는 호칭을 만들지 않으므로 호칭이 필요하면 설정에 넣는다
- 사실 보존 실패는 다른 점수로 상쇄하지 않는다. 검증 실패 → 원문 유지, 자동 전송 없음
- 합성 데이터만 외부(Claude API, Neo4j)에 보낸다
- 결정 변경은 `docs/decisions.md`에 5줄 이내로
- 남은 일은 `docs/backlog.md`(A 바로 가능 / B 팀원 대기 / C 보류). 끝낸 줄은 지우고 decisions에 결과를 남긴다

## 함정
- `ruff format`을 돌린 뒤 테스트. line-length 110
- 화면: 서버 문자열은 전부 text node로 넣는다(innerHTML 금지 — 테스트가 강제). 인라인 `<script>`·인라인 핸들러·외부 리소스 금지(CSP `script-src 'self'`). `js/`에 파일을 더하거나 빼면 `index.html`의 modulepreload 목록도 고친다(`shell.test.js`가 잡는다). 클립보드 복사는 포커스 없으면 실패하므로 피드백 저장과 분리
- 정적 파일·화면 HTML은 `Cache-Control: no-cache`로 나가야 한다(미들웨어가 붙인다) — 없으면 모듈 버전이 섞여 화면이 뜨지 않는다
- 여러 포트의 서버를 같은 `localhost`로 열면 `pi_session` 쿠키가 겹쳐 서로 로그아웃시킨다 → `web.localhost:8101`처럼 이름을 달리해 연다
- 서버 시작 때 e5 임베더를 백그라운드로 워밍업한다(`_warm_up_embedder`). 로그 `embedder warmed up`이 뜨기 전 첫 C/D 요청은 10초쯤 걸릴 수 있다
- `/v1/rewrites/usage`처럼 `/{request_id}` 뒤에 고정 경로를 붙이면 UUID 파싱 422가 난다 → 고정 경로는 별도 라우터(`/v1/usage`)
- fake 프로바이더도 글자 수 기반 토큰 수를 보고한다(사용량 테스트용). 실제 토큰과 다르니 비용 계산엔 anthropic 결과만 쓴다
- 설치 extra: `dev`(테스트·ruff), `db`(SQLAlchemy·asyncpg·pgvector·alembic, torch 없음 — CI가 이 조합으로 Postgres 통합 테스트를 돌린다), `embed`(sentence-transformers·torch), `graph`(graphiti). 로컬 개발은 `backend[dev,db,embed,graph]`
- 저장소 메서드는 전부 `async`다(Postgres가 기준). 라우트·서비스에서 `await`를 빼먹으면 코루틴 객체가 그대로 응답에 들어간다
- 워커는 **1개**가 전제다: 로그인 실패 제한·요청 제한·그래프 수집 잠금·진행 중 요청 집계가 프로세스 메모리에 있다. 프록시 뒤에서는 uvicorn `--proxy-headers`(운영 이미지가 넣는다) — 앱에서 `X-Forwarded-For`를 직접 읽지 않는다
- 정규식은 `re`가 GIL을 놓지 않아 스레드로 옮겨도 서버 전체가 멈춘다. 사용자 입력에 도는 패턴에 `\s*`가 맞닿는 식의 중첩 반복을 두지 않는다(2026-11-11 보안 검토: 가림 패턴 하나로 30KB 요청에 23초 정지)
- 라우트 함수 안에서 import한 타입을 인자 주석에 쓰면(`from __future__ import annotations`) FastAPI가 쿼리 인자로 보고 422를 낸다 → 모듈 맨 위에서 import
- ruff E501은 한글을 2칸으로 센다(한글 주석은 55자쯤에서 줄바꿈)
- 관리자 응답에 문장이 들어가는 필드를 더하지 않는다(`test_admin.py`의 표식 검사가 잡는다). `test_admin.py`는 표에 직접 INSERT한다 — 기본값 없는 NOT NULL 열을 더하면 `_add_*` 헬퍼도 고친다
- `rewrite_requests.condition`은 `String(1)`이라 `auto`를 저장하면 안 된다(멱등 해시만 요청 값). `RewriteInput`에 선택 필드를 더할 때는 `_input_hash`에서 기본값이면 빼야 예전 해시가 유지된다
- asyncpg는 timestamptz를 UTC로 돌려준다. 날짜·요일을 글자로 만들 때는 `local_time()`으로 한국 시간으로 바꾼 뒤에(그래프 에피소드 본문, 프롬프트의 예시 날짜)
- 그래프 수집 상한은 지워지지 않는 카운터(`usage_counters.graph_episodes`, 0009) 기준이고 예산이 모자라면 최근 메시지부터 넣는다. 상한은 API 경로(`ingest_user`)에만 걸리고 합성 적재·평가(`ingest_pending`)와 `graph_reset.py`에는 안 걸린다
- 문체 묶음은 내 연속 발화 6건·600자마다 나눈다. 프롬프트 예시는 하나 600자·문체 합계 2,400자·사실 맥락 합계 1,200자 상한 — 상한에 안 걸리는 데이터에서는 프롬프트가 예전과 글자까지 같아야 한다(평가 재현성)
- 오류 기록(`import_jobs.error_message`·`graph_error`, 삭제 응답, 로그)에는 예외의 종류만 남긴다. 예외를 문자열로 찍지 말고 `exception_kind`/`core/safe_exc.py`를 쓴다(문장에 SQL 매개변수 = 대화 원문이 섞인다). DB 엔진은 `hide_parameters=True`
- NUL·짝 없는 대리 문자가 든 입력은 전역 의존성(`core/text_guard.py`)이 저장 전에 422로 끝낸다. 가져오기의 대화 본문만 예외(가져오기 쪽이 `?`로 바꿔 읽는다)
- Git Bash heredoc으로 파이썬을 넘길 때: `PYTHONUTF8=1`을 붙이고, 본문에 작은따옴표 세 개나 `\n`이 있으면 깨진다(파일로 써서 실행). `docker run`에 stdin을 넘기려면 `-i`
- `backend/alembic.ini`는 ASCII만. alembic이 로케일 인코딩(cp949)으로 읽어 한글 주석이 있으면 UnicodeDecodeError
- 스키마 변경은 `db/models.py`와 `migrations/versions/`를 함께 고친다. 자동 생성(`alembic revision --autogenerate`)은 결과를 꼭 읽고 커밋
- `episodes.embedding`은 vector(384)로 고정(0002). 임베딩 모델을 바꾸면 새 마이그레이션 + `ImportService.reindex`. 테스트는 `embedding_provider="fake"`(conftest)라 torch를 안 올린다
- relationship 없는 두 모델을 같은 세션에 add하면 SQLAlchemy가 FK 순서를 안 지켰다(episode_messages가 episodes보다 먼저 INSERT) → 부모를 add한 뒤 `await s.flush()` 하고 자식을 add
- 재부팅 뒤 Docker Desktop은 자동 실행되지 않는다. 테스트의 Postgres 항목이 전부 skip이면 Docker Desktop 실행 → `docker compose up -d postgres neo4j`
- `graphiti_core`는 import 시 `load_dotenv()`로 .env 전체를 os.environ에 올린다 → `deps._build_graph`가 import 뒤 새로 생긴 환경변수를 지운다. 이걸 빼면 테스트의 `Settings(_env_file=None)`이 환경변수로 DATABASE_URL을 읽어 Postgres에 붙는다
- `.env`의 NEO4J_URI는 2026-11-08 밤부터 로컬 `bolt://localhost:7687`. 1주차 스파이크 결과는 Aura에 남아 있고 그 URI는 `.env` 주석(`NEO4J_URI_AURA`)에 있다. 로컬 Neo4j 비밀번호는 컨테이너 생성 때의 NEO4J_PASSWORD와 같다
- 삭제는 모두 동기이고 그래프 → DB 순서다: `DELETE /v1/conversations/{id}`(에피소드 `remove_episode`), `DELETE /v1/contacts/{id}`(대화방별 삭제 → 첨삭 이력 → 상대·페르소나 CASCADE), `DELETE /v1/auth/me`(비밀번호 재확인, 상대 전부 → group `DETACH DELETE` → 사용량·세션·사용자). 그래프 실패는 `graph_error(s)`로 남고 DB는 지워진다. 서비스는 `services/deletion_service.py`
- E2E는 컨테이너로 띄운다(`make up-all`, 또는 운영 구성 리허설 `docs/deploy.md` 14장). 호스트 venv의 uvicorn은 Smart App Control 때문에 이 PC에서 뜨지 않는다
- DB 문자열 열 길이: `rewrite_results.*_version`은 200자(0004). D 검색기 버전 문자열이 64자를 넘어 500이 났었다. 새 버전 문자열을 만들면 열 길이를 확인한다
- 가져오기 API 뒤 그래프 수집은 백그라운드(`asyncio.create_task`)라 응답의 `graph_status`는 pending이다. `GET /v1/imports/{id}`로 completed를 확인한다. 서버 프로세스가 죽으면 미수집분은 다음 서버 시작 때 `resume_all`이 이어받는다(`GRAPH_RESUME_ON_START`, 로그 `graph resume:`), 또는 다음 가져오기·`import_synthetic.py --graph`·`graph_reset.py`
- 보존 항목 추출은 규칙 기반이라 오탐이 있다. 규칙을 바꾸면 `evals/preserve_rules.md`도 같이 고친다
- fake 프로바이더는 원문을 그대로 돌려준다 → 검증은 항상 통과. 실제 품질은 `--provider anthropic`으로만 본다. 2026-11-08 실측(5사례×A/B, Opus·Sonnet 5.5)은 `evals/results/2026-11-08-first-real-run.md`
- `.env`의 `LLM_PROVIDER=fake`여도 `run_eval.py --provider anthropic`은 실제 호출한다 (인자가 .env를 덮어쓴다). 서버(`make dev`)로 실제 호출을 보려면 `.env`를 `anthropic`으로
- graphiti-core 0.31.2 `AnthropicClient`는 **강제 tool_choice**를 쓰고 5.5 계열이 이를 400으로 거부한다 → 그래프 추출 기본은 `claude-haiku-4-5`. 리랭커는 `PassthroughReranker`(기본 리랭커는 OpenAI 호출). 모델별 max_tokens 표도 구버전. anthropic SDK 2.x에 없는 `temperature`를 넘기므로 `GraphAnthropicClient`가 떼어낸다
- Aura(`neo4j+s://`) 접속 시 `self-signed certificate` 오류는 Windows 루트 저장소에 SSL.com 루트가 없어서다 → `load_settings()`가 certifi 번들을 `SSL_CERT_FILE`로 지정한다. `--limit 5` 실측(2026-11-08)은 `graph/graph-spike-report.md`
- 원격: `github.com/SkyWith628/persona-ize`(비공개). `main` 직접 푸시는 팀장만, 나머지는 PR. 11/04 이전 원격 뼈대(`spikes/`)는 `graph/graphiti_p0/`로 대체됨
- 평가셋 열 이름은 `docs/dataset-guide.md`가 기준. `evals/run_eval.py`·`scripts/validate_data_files.py`가 그 열을 읽으므로 열을 바꾸면 셋을 같이 고친다
- 이 Windows 머신: Docker Desktop 4.98 + WSL 3.0.3 동작 확인(2026-11-08). 새 PC에서는 BIOS 가상화(SVM/VT-x) → `VirtualMachinePlatform`·WSL 기능 → `wsl --install --no-distribution` → 재부팅 순서가 필요했다(`docker/README.md`). node·gh는 없다
- 개발은 `make up`(DB 두 개) + `make dev`(호스트 venv). 전체 컨테이너는 `make up-all` — backend 이미지는 다단계(`prod` 기본 3.0GB, 첫 빌드 3.5~5.5분, 코드만 바뀌면 몇 초), CPU torch, e5 모델 내장, 시작 때 alembic upgrade (`docker/README.md`)
- API 비용은 구독과 별개. Console 사용량 한도 → `--limit 5` → 전체 순서
- 2026-11-11부터 이 Windows PC는 Smart App Control(강제)이 서명 없는 `.pyd`를 막는다 — 호스트 venv에서 `import sqlalchemy`·`import torch`가 "애플리케이션 제어 정책에서 이 파일을 차단했습니다"로 실패(alembic·pytest·uvicorn·run_eval 모두). DB를 쓰는 명령은 컨테이너로: `MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -v "C:/Users/gkfkd/dev/persona-ize:/repo" -w /repo backend python evals/run_eval.py ...` (Git Bash, DATABASE_URL·NEO4J_URI는 compose가 서비스 이름으로 바꿔 준다). 서버는 `make up-all`, 테스트·lint는 `make test-docker`·`make lint-docker`. **`.env`가 들어간 컨테이너에서 pytest를 돌리지 않는다** — `DATABASE_URL`·`NEO4J_URI`·API 키가 환경변수로 새어 테스트가 실제 개발 DB·그래프에 붙고 가져오기 테스트가 haiku를 부른다
