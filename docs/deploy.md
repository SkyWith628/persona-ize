# 배포 안내 — 리눅스 VM 한 대 + Docker Compose + Caddy 자동 HTTPS

처음 보는 사람이 위에서부터 따라 하면 서비스가 `https://<도메인>`으로 뜨도록 쓴 문서다.
끝의 [검증한 것과 못 한 것](#15-검증한-것과-못-한-것-2026-11-11)을 먼저 읽어 두면 어디서 막힐 수 있는지 안다.

관련 파일: `docker-compose.prod.yml` · `docker/caddy/Caddyfile` · `backend/Dockerfile` · `.env.production.example` ·
`scripts/ops/*.sh` · `scripts/smoke_test.py`

## 1. 구성

```
인터넷 ──80/443──▶ caddy ──(edge 네트워크)──▶ backend:8000 ──(data 네트워크, 바깥 길 없음)──▶ postgres:5432
                  │ HTTPS 종료, 인증서 자동 발급          │ FastAPI + 화면(/, /static)                    ├▶ neo4j:7687
                  │ 압축, 본문 상한, HSTS, 접근 로그      │ e5 임베딩 모델 내장(오프라인)                 └◀ backup (주기 pg_dump → ./backups)
                  │                                      └──(edge)──▶ api.anthropic.com (Claude)
```

| 서비스 | 이미지 | 호스트에 여는 포트 | 데이터 |
|---|---|---|---|
| caddy | `caddy:2-alpine` | **80, 443** (유일) | 볼륨 `caddy-data`(인증서) |
| backend | `persona-ize/backend:prod` (이 저장소에서 빌드) | 없음 | 없음(읽기 전용 파일시스템, `/tmp`만 쓰기) |
| postgres | `pgvector/pgvector:pg16` | 없음 | 볼륨 `postgres-data` |
| neo4j | `neo4j:5` (커뮤니티판) | 없음 | 볼륨 `neo4j-data`, `neo4j-logs` |
| backup | `pgvector/pgvector:pg16` | 없음 | 서버 폴더 `./backups` |

- compose 프로젝트 이름은 `persona-ize-prod`로 고정이다(컨테이너 `persona-ize-prod-backend-1` …). 개발 구성
  (`docker-compose.yml`, 프로젝트 `persona-ize`)과 같은 폴더에 있어도 컨테이너·볼륨이 섞이지 않는다.
- 설정은 저장소 루트의 `.env.production` 하나다. compose(`${…}` 치환)와 backend 컨테이너(환경변수)가 함께 읽는다.
- 명령이 길어서 래퍼를 둔다: `scripts/ops/compose.sh <compose 인자>` = `docker compose --env-file .env.production -f docker-compose.prod.yml …`

### 백엔드가 한 프로세스(워커 1개)인 이유
로그인 시도 제한, 요청 제한, 그래프 수집의 사용자별 잠금이 **프로세스 메모리**에 있다. 워커나 컨테이너를 늘리면
각자 따로 세어서 제한이 느슨해지고, 같은 사용자의 그래프 수집이 겹칠 수 있다. e5 모델도 프로세스마다 따로 올라간다
(약 0.9GB). 늘리려면 먼저 이 상태를 DB나 Redis로 옮겨야 한다.

## 2. 준비물

### 서버
| 항목 | 권장 | 근거(2026-11-11 실측, Windows + Docker Desktop, 유휴 상태) |
|---|---|---|
| 메모리 | **4GB + 스왑 2GB**, 여유 있게는 8GB | backend 0.84~0.96GiB(torch + e5 모델, 메시지 1,500건 가져오는 동안 최대 0.96) · neo4j 1.0GiB(힙 512MB + 페이지 캐시 256MB + JVM) · postgres 0.03~0.05GiB · caddy 0.02GiB → 합계 약 2GiB. 여기에 OS와, 서버에서 이미지를 빌드할 때의 여유가 더 든다 |
| CPU | 2 vCPU 이상 | 임베딩은 CPU로 돈다. 16코어 PC에서 1,500건 가져오기에 13초(8코어 사용). 2 vCPU에서는 재 보지 못했다 |
| 디스크 | 30GB 이상 | 이미지 약 4.7GB(backend 3.0 · neo4j 1.0 · postgres 0.6 · caddy 0.1) + 빌드 캐시 2~3GB + 빈 neo4j 볼륨 0.5GB + 로그 최대 0.25GB(서비스당 10MB×5) + 백업 |
| OS | Ubuntu 22.04/24.04 LTS (x86_64) | ARM은 검증하지 못했다 |

컨테이너 메모리 상한은 backend 2GB, neo4j 2GB로 걸어 두었다(`BACKEND_MEM_LIMIT`, `NEO4J_MEM_LIMIT`). 넘으면 그
컨테이너만 죽었다가 다시 뜬다.

### 그 밖에
- **도메인** 하나(예: `persona.example.com`)와 DNS를 고칠 권한. 없을 때는 [13장](#13-도메인-없이-띄우기).
- **Anthropic API 키**. Console에서 **월 사용량 한도를 먼저** 건다. API 비용은 구독과 별개다.
- 이 저장소를 받을 수 있는 계정(비공개 저장소다).

## 3. 처음 배포

### 1단계 — Docker 설치 (Ubuntu)
공식 안내(https://docs.docker.com/engine/install/ubuntu/)가 기준이다. 요약:

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"   # 다시 로그인해야 적용된다. docker 그룹은 사실상 root 권한이다
docker version && docker compose version
```
필요한 버전: Docker Engine 25 이상, Compose 2.24 이상(헬스체크 `start_interval`, `env_file`의 `path/required`,
`up --wait`를 쓴다). 새로 설치하면 충족한다.

메모리가 4GB면 스왑을 만든다:
```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 2단계 — 코드 받기
```bash
git clone https://github.com/SkyWith628/persona-ize.git && cd persona-ize
```

### 3단계 — 설정 파일
```bash
cp .env.production.example .env.production && chmod 600 .env.production
nano .env.production
```
`CHANGE_ME`가 남아 있으면 배포 스크립트가 멈춘다. 꼭 채울 것:

| 설정 | 값 |
|---|---|
| `SITE_ADDRESS` | 서비스 도메인(앞에 `https://`를 붙이지 않는다) |
| `POSTGRES_PASSWORD`, `NEO4J_PASSWORD` | `openssl rand -base64 32 \| tr -d '/+='` 로 각각 만든다. Neo4j는 8자 이상 |
| `ANTHROPIC_API_KEY` | Console에서 만든 키 |
| `REGISTER_INVITE_CODE` | 가입할 때 요구할 코드(`openssl rand -hex 6`). 비우면 주소를 아는 누구나 가입해 API 비용을 쓴다 |
| `ADMIN_USERNAMES` | 관리자로 쓸 아이디 |
| `USAGE_GLOBAL_DAILY_*`, `GRAPH_INGEST_*` | 예시 값을 예산에 맞게 고친다([8장](#8-비용-상한)) |

나머지는 예시 값이 운영 권장값이다. 설정 이름별 설명은 `.env.production.example`의 주석에 있다.
`DATABASE_URL`·`NEO4J_URI`는 compose가 서비스 이름으로 조립하므로 적지 않는다.

배포 연습만 해 보려면 `LLM_PROVIDER=fake`, `ANTHROPIC_API_KEY=`(빈 값)로 둔다. 가짜 모델은 원문을 그대로 돌려주고
비용이 0이다. 이때 그래프 수집은 키가 없어 실패로 남는다(정상).

### 4단계 — DNS와 방화벽
1. DNS에 A 레코드: `<도메인> → 서버의 공인 IP`. 퍼졌는지 확인: `dig +short <도메인>`
   **DNS가 서버를 가리키기 전에 띄우지 않는다.** 인증서 확인이 실패하고, 반복되면 발급 횟수 제한에 걸린다.
2. 클라우드 방화벽(보안 그룹)에서 들어오는 **TCP 80, 443**을 연다. SSH(22)는 내 IP만.
   80은 인증서 확인과 https 리다이렉트에 쓴다. HTTP/3은 꺼 두어서 UDP는 열 필요가 없다.
3. 5432·7474·7687·8000은 **열지 않는다.** compose가 호스트에 내보내지도 않는다.
   참고: Docker가 내보낸 포트는 `ufw` 규칙을 거치지 않고 열린다. `ufw`를 쓰더라도 compose에 `ports:`를 더하지 않는다.

### 5단계 — 기동
```bash
scripts/ops/deploy.sh --no-pull
```
하는 일: 설정 점검 → (있으면) 배포 전 백업 → 이미지 빌드 → 기동 → 전 서비스 healthy 대기 → 스모크 테스트.
첫 빌드는 torch와 e5 모델(약 470MB)을 받느라 몇 분 걸린다(빠른 회선의 PC에서 3분 33초). 기동은 30초쯤.

손으로 하려면: `make prod-config && make prod-up` (스모크·백업 없이 빌드 → 기동 → 대기).

### 6단계 — 확인
```bash
scripts/ops/compose.sh ps                           # 다섯 서비스가 모두 (healthy)
curl -s "https://<도메인>/healthz?deep=true"        # {"status":"ok", …}  — DB나 그래프에 못 붙으면 503 "degraded"
curl -sI "http://<도메인>/" | head -3               # 308 → https
```
브라우저로 `https://<도메인>`을 열어 자물쇠(인증서)와 화면을 본다.

### 7단계 — 관리자 계정
관리자 여부는 아이디로만 정한다(`ADMIN_USERNAMES`와 대소문자까지 같아야 한다). 그래서 운영에서는 그 아이디를
**화면 가입으로 만들 수 없게 막아 두었다**(다른 사람이 먼저 차지하는 것을 막으려고 — 시도하면 "이미 사용 중인 이름"으로 보인다).
관리자 계정은 서버에서 만든다. 비밀번호는 물어본다(8자 이상):
```bash
scripts/ops/run.sh scripts/reset_password.py --username <관리자 아이디> --create
```
만든 뒤 `https://<도메인>`에서 그 아이디로 로그인하고 `https://<도메인>/admin`을 연다. 계정이 아직 없으면 backend가
시작할 때 로그에 "관리자 계정이 아직 없습니다" 경고를 남긴다.

비밀번호를 잊은 사용자는 운영자가 서버에서 재설정한다(이메일이 없어 자가 재설정이 없다):
`scripts/ops/run.sh scripts/reset_password.py --username <이름>`

### 8단계 — 스모크 테스트
`deploy.sh`가 끝에 한 번 돌린다. 따로 돌리려면:
```bash
python3 scripts/smoke_test.py --base-url https://<도메인> --invite-code <초대 코드> --no-import
```
임의 사용자를 만들어 가입 → 상대 → 페르소나 → (가져오기) → 첨삭 → 피드백 → 사용량 → 계정 삭제까지 가고, 단계마다
OK/실패를 찍는다. 실패가 있으면 종료 코드 1.

**비용**: 실제 모델에서는 첨삭이 진짜로 나간다. 기본(전체)은 첨삭 2회 + 그래프 수집 3건, `--no-import`는 첨삭 1회,
`--skip-model`은 모델 호출 없음, `--health-only`는 헬스·화면만. `deploy.sh`는 실제 모델이면 `--no-import`로 돈다
(API 키·크레딧 확인에는 첨삭 한 번이면 된다). 전체로 돌리려면 `deploy.sh --smoke-full`.

## 4. 업데이트

```bash
cd persona-ize && scripts/ops/deploy.sh
```
`git pull --ff-only` → 설정 점검 → **배포 전 백업** → 빌드 → 기동(바뀐 컨테이너만 다시 만든다) → healthy 대기 → 스모크.
코드만 바뀌면 빌드는 몇 초다(의존성·모델 레이어는 캐시). 백엔드가 다시 뜨는 10~20초 동안은 Caddy가 대신 답한다:
화면 요청에는 "점검 중" 안내, API 요청에는 HTTP 502와 `{"code":"service_unavailable","message":…}`.

- DB 스키마는 backend가 시작할 때 `alembic upgrade head`로 올린다. 실패하면 backend가 뜨지 않고 재시작을 반복한다
  → `scripts/ops/compose.sh logs --tail 50 backend`.
- 옵션: `--no-pull` `--no-backup` `--no-build` `--pull-base`(기반 이미지 갱신, 전체 재빌드) `--no-smoke` `--smoke-full`
  `--prune`(태그 없는 옛 이미지 삭제).
- 배포 기록: `.deploy/history.log` (시각, 커밋 전→후, 이미지, 결과).
- Caddyfile만 고쳤을 때: `scripts/ops/compose.sh exec caddy caddy reload --config /etc/caddy/Caddyfile`
- `.env.production`만 고쳤을 때: `scripts/ops/compose.sh up -d` (값이 바뀐 컨테이너만 다시 만든다).

### 레지스트리 이미지로 배포 (서버에서 빌드하지 않기)
`v*` 태그를 푸시하면 `.github/workflows/release.yml`이 `ghcr.io/<소유자>/<저장소>/backend:<태그>`를 올린다
(**이 워크플로는 아직 실행해 보지 못했다**). 서버에서:
```bash
docker login ghcr.io                      # 비공개 저장소: read:packages 권한의 토큰
# .env.production: BACKEND_IMAGE=ghcr.io/skywith628/persona-ize/backend:v0.1.0
scripts/ops/deploy.sh --no-build
```
메모리가 작은 서버에서 빌드가 버거울 때 쓴다. 이미지 전송량은 약 0.76GB(압축), 풀면 약 3GB.

## 5. 되돌리기

```bash
scripts/ops/rollback.sh        # 직전 이미지(…/backend:previous)로 backend만 다시 띄운다. 한 번 더 돌리면 원래대로
```
- 되돌아가는 것은 **백엔드 코드뿐**이다. `deploy.sh`는 이미지 내용이 바뀐 배포에서만 직전 이미지를 `:previous`로 남긴다.
- **DB 스키마는 되돌아가지 않는다.** 새 버전이 마이그레이션을 올렸으면 옛 코드가 새 스키마 위에서 돈다. 열 추가는
  대개 괜찮다. 깨지면 배포 전 백업으로 복원한다(`scripts/ops/restore.sh`, 그 사이에 쌓인 데이터는 잃는다).
- compose 파일·Caddyfile까지 되돌리려면 `git checkout <이전 커밋>` 뒤 `scripts/ops/deploy.sh --no-pull`
  (이전 커밋은 `.deploy/history.log`).
- 의존성 버전을 고정한 파일이 아직 없어서, 같은 커밋을 다시 빌드해도 그때의 최신 패키지가 깔린다. 그래서
  "다시 빌드"보다 **남겨 둔 이미지로 되돌리는 쪽**이 안전하다. 이미지 안의 실제 버전은 `/opt/pz/pip-freeze.txt`.

## 6. 백업·복원

### PostgreSQL — 자동
`backup` 컨테이너가 `BACKUP_INTERVAL_HOURS`(기본 24)마다 `pg_dump`(커스텀 포맷)를 떠서 `./backups/pz-<DB>-<UTC 시각>.dump`
로 두고, `BACKUP_RETENTION_DAYS`(기본 14)가 지난 파일을 지운다. 지우는 것은 새 덤프가 성공한 뒤뿐이다.
백업이 밀리면 `scripts/ops/compose.sh ps`에 backup이 `unhealthy`로 보인다.

```bash
scripts/ops/backup.sh           # 지금 한 번 (deploy.sh가 배포 전에 부른다)
scripts/ops/backup.sh --list
```

**서버 밖에도 둔다.** `./backups`는 같은 디스크라 서버를 잃으면 함께 사라진다. 덤프에는 대화 원문이 들어 있으므로
암호화해서 옮긴다. 예: `gpg -c backups/pz-….dump` 뒤 다른 저장소로 복사(주기 작업은 직접 건다 — 이 저장소에는 없다).
덤프 파일은 컨테이너가 root 소유·소유자만 읽기(600)로 만든다. 서버에서 복사하거나 지울 때는 `sudo`가 필요하다.

### 복원 연습 (분기에 한 번은 해 본다)
운영 DB를 건드리지 않고 다른 이름의 DB에 넣어 보고 행 수를 본다:
```bash
scripts/ops/restore.sh pz-persona_ize-<시각>.dump --db pz_restore_drill --drop-after
```

### 실제 복원
```bash
scripts/ops/backup.sh                                  # 지금 상태도 남겨 둔다
scripts/ops/restore.sh pz-persona_ize-<시각>.dump       # DB 이름을 입력해 확인 → backend 중지 → 복원 → 다시 기동
```
backend가 다시 뜨면서 스키마를 최신으로 올리므로 옛 스키마의 덤프도 쓸 수 있다.

### Neo4j(그래프)
커뮤니티판은 켜진 채로 덤프할 수 없다. 스크립트가 neo4j만 잠깐 내렸다 올린다(빈 그래프에서 30초).
```bash
scripts/ops/neo4j-dump.sh                      # → ./backups/neo4j/pz-neo4j-<시각>.dump
scripts/ops/neo4j-dump.sh --load <파일>        # 그 덤프로 현재 그래프를 덮어쓴다
```
내려가 있는 동안 D 조건 첨삭과 그래프 수집은 실패할 수 있다. 자동 주기는 없다 — 한가한 시간에 가끔 돌린다.

**복원 뒤 그래프**: 그래프는 PostgreSQL의 원문에서 다시 만들 수 있다
(`scripts/ops/run.sh scripts/graph_reset.py --user <이름> --dry-run`으로 건수를 먼저 본다). 다만 **메시지마다 haiku를
다시 부른다** — 11월 실측으로 메시지당 약 US$0.03~0.04, 10~15초. 1만 건이면 수백 달러다. 그래서 그래프가 클수록
Neo4j 덤프를 떠 두는 편이 싸다. PostgreSQL과 Neo4j를 서로 다른 시점으로 되돌리면 "수집됨" 표시와 실제 그래프가
어긋난다 → 어긋난 사용자는 `graph_reset.py`로 맞춘다.

## 7. 로그·상태

```bash
scripts/ops/compose.sh ps
scripts/ops/compose.sh logs -f --tail 100 backend       # 또는 make prod-logs SERVICE=backend
scripts/ops/compose.sh logs --since 1h caddy | grep '"status":5'     # 프록시 접근 로그(JSON)에서 5xx만
scripts/ops/compose.sh logs backup                      # 백업 기록
docker stats --no-stream                                # 메모리·CPU
```
- 로그는 서비스당 10MB × 5개에서 순환한다(`docker logs`로 보이는 범위가 그만큼이다). 더 오래 두려면 따로 모은다.
- Caddy 접근 로그에는 IP·경로·User-Agent가 남는다. 쿠키·`Authorization`·CSRF 토큰 값은 남기지 않는다.
- `/healthz`는 backend 프로세스만 본다(컨테이너 헬스체크가 쓴다). `/healthz?deep=true`는 PostgreSQL·Neo4j에 실제로
  붙어 보고 하나라도 안 되면 503 `degraded`다 — 밖에서 감시할 때는 이쪽을 본다. Neo4j가 내려가 있어도 backend는
  뜨고(A~C 조건은 동작), Neo4j가 돌아오면 다시 붙는다.
- 기동 직후 backend 로그에 `embedder warmed up`이 찍히면 임베딩 모델이 올라온 것이다(시작 뒤 약 10초).
  그 전에 들어온 첫 가져오기·C/D 첨삭은 모델을 기다리느라 10초쯤 걸린다.
- `Could not reach the Hub … offline mode is enabled`는 정상이다(모델을 내려받지 않고 이미지 안의 것을 쓴다는 뜻).

## 8. 비용 상한

겹겹이 건다. 위에서부터 "마지막 방어선" 순서다.

| 층 | 설정 | 넘으면 |
|---|---|---|
| Anthropic Console | 월 사용량 한도(Console에서) | API가 거절 → 첨삭이 "원문 유지"로 떨어진다 |
| 전체 하루 차단기 | `USAGE_GLOBAL_DAILY_REWRITE_LIMIT`, `USAGE_GLOBAL_DAILY_TOKEN_LIMIT` | 모든 사용자 429 `budget_exceeded`, 다음 날(한국 시간) 풀림 |
| 사용자 하루 한도 | `USAGE_DAILY_REWRITE_LIMIT`(200), `USAGE_DAILY_TOKEN_LIMIT`(400000) | 그 사용자만 429 `usage_exceeded` |
| 그래프 수집 상한 | `GRAPH_INGEST_USER_MESSAGE_CAP`(예 300), `GRAPH_INGEST_DAILY_GLOBAL_CAP`(예 1000) | 수집을 멈춘다(작업 상태 `limited`). 말투 검색(C)은 그대로 |
| 가입 제한 | `REGISTER_INVITE_CODE`, `AUTH_ALLOW_REGISTER=false` | 가입 거부 |
| 요청 제한 | `RATE_LIMIT_PER_MINUTE`(120), `RATE_LIMIT_AUTH_PER_MINUTE`(20) | IP당 429 `rate_limited` |

- **그래프 수집이 비용의 대부분이다**(11월 실측에서 85%). 메시지 1건당 약 US$0.03~0.04이므로 하루 1,000건 상한이면
  하루 최악 약 US$30~40다. 상한을 0(무제한)으로 두지 않는다.
- **비용에 전체 상한이 없는 구성이면 backend가 시작을 거부한다**(`APP_ENV=production`): 그래프가 켜져 있는데 하루 전체
  상한이 0인 경우(사용자당 상한도 0이거나 가입이 초대 코드 없이 열려 있을 때), 실제 모델 + 초대 코드 없는 공개 가입 +
  전체 차단기 둘 다 0인 경우. 로그의 `CRITICAL … 서버를 시작하지 않습니다` 아래에 정할 설정 이름이 나온다. 알고도 그렇게
  띄울 때만 `ALLOW_UNCAPPED_COSTS=true`(거부 대신 경고).
- 하루 한도 429(`usage_exceeded`·`budget_exceeded`)에는 다음 날(한국 시간 자정)까지 남은 초가 `Retry-After`로 붙는다.
- 전체 차단기의 하루 최악 비용 = 토큰 한도 × Console 요금표의 단가. 예시 값(2,000회 / 400만 토큰)은 예산에 맞게 고친다.
- 서버를 다시 띄우면 수집하다 만 메시지를 이어서 넣는다(`GRAPH_RESUME_ON_START=true`). 큰 가져오기 도중에 재시작하면
  남은 분량만큼 비용이 이어진다.
- 관리자 화면의 비용 환산(`COST_*`)은 0이면 표시하지 않는다. 단가는 Console 요금표에서 지금 쓰는 모델 것을 확인해 채운다.
- 실제 청구액은 Console이 기준이다. 서버 쪽 집계: `scripts/ops/run.sh scripts/usage_report.py --days 30`

## 9. 운영 스크립트

운영 이미지에는 앱 코드만 들어 있다. 저장소의 `scripts/*.py`는 `scripts/ops/run.sh`가 운영 스택 안에서 돌려 준다
(같은 이미지·설정·네트워크, 저장소의 `scripts/`·`data/`·`evals/`를 읽기 전용으로 붙인다):

```bash
scripts/ops/run.sh scripts/usage_report.py --days 30 > usage.md     # 결과는 표준 출력을 리다이렉트
scripts/ops/run.sh scripts/housekeeping.py                          # 보통은 HOUSEKEEPING_INTERVAL_HOURS로 서버가 직접 돈다
scripts/ops/run.sh scripts/reset_password.py --username <이름>
scripts/ops/run.sh scripts/graph_reset.py --user <이름> --dry-run
```

| 스크립트 | 하는 일 |
|---|---|
| `scripts/ops/deploy.sh` | 배포([4장](#4-업데이트)) |
| `scripts/ops/rollback.sh` | 직전 이미지로 되돌리기 |
| `scripts/ops/backup.sh` / `restore.sh` | PostgreSQL 백업 1회 / 복원(연습·실제) |
| `scripts/ops/neo4j-dump.sh` | 그래프 덤프·복원 |
| `scripts/ops/compose.sh` | 운영 compose 명령 래퍼 |
| `scripts/ops/run.sh` | 저장소 스크립트를 운영 스택 안에서 실행 |
| `scripts/smoke_test.py` | 배포 스모크 테스트 |

다른 env 파일이나 프로젝트 이름으로 돌릴 때는 `PZ_ENV_FILE=<파일>`, `PZ_PROJECT=<이름>` 환경변수를 준다.

## 10. 운영 전 보안 점검표

- [ ] `.env.production` 권한 600, 저장소에 커밋되지 않음(`git status`에 보이지 않는다)
- [ ] `APP_ENV=production`, `AUTH_ALLOW_HEADER_STUB=false`, `SESSION_COOKIE_SECURE=true` (compose가 기본으로 이 값을 넣는다)
- [ ] DB·Neo4j 비밀번호가 새로 만든 값(개발용 `persona-dev-only`·`neo4j-dev-only` 아님)
- [ ] `REGISTER_INVITE_CODE`가 있거나 가입이 닫혀 있다
- [ ] `ADMIN_USERNAMES`의 계정을 서버에서 만들었다(7단계) — backend 시작 로그에 "관리자 계정이 아직 없습니다" 경고가 없다
- [ ] Console 월 한도, 전체 차단기(`USAGE_GLOBAL_DAILY_*`), 그래프 상한(`GRAPH_INGEST_*`)이 0이 아니다
- [ ] 방화벽: 들어오는 포트는 80·443(+SSH는 내 IP). `scripts/ops/compose.sh ps`에서 포트가 보이는 것은 caddy뿐
- [ ] `curl -sI https://<도메인>/`에 `strict-transport-security`가 있고 `server`·`via`가 없다.
      https가 며칠 안정된 뒤 `HSTS_MAX_AGE`를 300 → 31536000으로 올렸다
- [ ] `https://<도메인>/docs`가 열리지 않는다(운영 모드)
- [ ] 백업이 생기고(`scripts/ops/backup.sh --list`) 서버 밖 사본이 있다. 복원 연습을 한 번 했다
- [ ] 개인정보 안내(`/privacy`)·약관(`/terms`) 문구가 실제 보존 기간(`REWRITE_RETENTION_DAYS`)·삭제 방법과 맞다
- [ ] 실제 사용자의 대화가 들어오는 순간부터 그 내용은 Claude API와 Neo4j로 간다 — 안내문에 적혀 있고 동의를 받는다
      (`REQUIRE_CONSENT=true`)
- [ ] OS 보안 업데이트(`unattended-upgrades`), SSH는 키 로그인만

컨테이너 쪽에 이미 해 둔 것: backend는 비루트(uid 10001)·권한 전부 제거·읽기 전용 파일시스템, 모든 컨테이너
`no-new-privileges`, caddy는 80/443 바인딩 권한만, DB 컨테이너는 바깥으로 나가는 길이 없는 네트워크.

## 11. 설정·비밀번호 바꾸기

- 설정: `.env.production`을 고치고 `scripts/ops/compose.sh up -d`.
- **DB 비밀번호**는 데이터 볼륨이 처음 만들어질 때만 env 값이 적용된다. 나중에 바꿀 때는 DB 안에서 먼저 바꾸고,
  같은 값을 `.env.production`에 적은 뒤 컨테이너를 다시 만든다:
  ```bash
  # PostgreSQL — 새 비밀번호를 두 번 묻는다. 끝나면 .env.production의 POSTGRES_PASSWORD를 같은 값으로 고친다
  scripts/ops/compose.sh exec postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "\password"'
  # Neo4j — 끝나면 .env.production의 NEO4J_PASSWORD를 고친다 (값이 셸 기록에 남으니 기록을 지운다)
  scripts/ops/compose.sh exec neo4j cypher-shell -u neo4j -p '<옛 값>' -d system \
    "ALTER CURRENT USER SET PASSWORD FROM '<옛 값>' TO '<새 값>'"
  scripts/ops/compose.sh up -d      # 값이 바뀐 컨테이너(backend·backup 등)가 다시 만들어진다
  ```
- Anthropic 키 교체: Console에서 새 키 발급 → `.env.production` → `scripts/ops/compose.sh up -d` → 옛 키 폐기.

## 12. 문제 해결

| 증상 | 원인과 조치 |
|---|---|
| 첨삭이 전부 "원문 유지"로 나온다. 스모크가 `모델 호출 실패로 원문을 유지했습니다` | Claude 호출이 실패한 것이다. **크레딧이 0이면 API가 400을 돌려준다**(Console → Billing). 그 밖에 키 오타, Console 월 한도 도달. `scripts/ops/compose.sh logs backend \| grep -i "model unavailable"` |
| `compose ps`에서 backend가 `Restarting` | 시작 명령이 실패한다. `logs --tail 50 backend`: ① 마이그레이션 실패 ② 운영 모드 검사가 설정을 거부(메시지에 어떤 설정인지 나온다) ③ DB 비밀번호 불일치(`password authentication failed` → 11장) |
| neo4j가 `Restarting`·`unhealthy`, 로그에 `OutOfMemory` 또는 종료 코드 137 | 메모리 부족. `free -h`로 확인 → 스왑 추가, `NEO4J_HEAP_SIZE`/`NEO4J_PAGECACHE_SIZE`를 줄이거나 서버를 키운다. `NEO4J_MEM_LIMIT`는 힙 + 페이지 캐시 + 1GB쯤으로 둔다(JVM이 힙 밖에서 쓰는 메모리가 있다) |
| 브라우저가 인증서 오류, caddy 로그에 `could not get certificate` | DNS가 이 서버를 가리키는지(`dig +short`), 80·443이 밖에서 열려 있는지, 다른 프로그램이 80/443을 쓰지 않는지. 고친 뒤 `compose.sh restart caddy`. 반복 실패로 발급 횟수 제한에 걸리면 기다려야 한다. `logs caddy \| grep -i -E "acme\|obtain\|challenge"` |
| 화면에 "서버를 점검하고 있습니다", API가 502 | backend가 내려가 있거나 뜨는 중. 1분 넘게 계속되면 `ps`·`logs backend` |
| 로그인이 유지되지 않는다 | https가 아닌 주소로 열었다(세션 쿠키가 `Secure`). 13장 |
| 가져오기가 413 | 본문이 `MAX_BODY_BYTES`(기본 8MB)를 넘었다. 한글은 글자당 3바이트라 `MAX_IMPORT_CHARS` 200만 자가 약 6MB다. 늘리려면 이 값만 올린다(Caddy도 같은 값을 쓴다) |
| 모든 요청이 400(Host 관련 오류) | `TRUSTED_HOSTS`에 접속한 도메인이 없다. 비워 두면 compose가 `SITE_ADDRESS`를 넣는다 |
| backend 로그에 `Read-only file system` | 앱이 `/tmp` 밖에 쓰려 했다. 급하면 `BACKEND_READ_ONLY=false` 후 `up -d`, 그리고 어디에 쓰는지 고친다 |
| 디스크가 찬다 | `docker system df`. 옛 이미지: `docker image prune -f`, 빌드 캐시: `docker builder prune -f`(다음 빌드가 처음부터 다시 돈다). 백업 보존 일수 확인 |
| 그래프 작업이 `failed`, 사유가 `Could not resolve authentication method` | `ANTHROPIC_API_KEY`가 비어 있다(가짜 모델로 띄웠을 때는 정상) |
| `deploy.sh`가 `git pull`에서 멈춘다 | 서버에서 파일을 직접 고쳤다. `git status`로 확인하고 되돌리거나 `--no-pull` |

## 13. 도메인 없이 띄우기

세션 쿠키가 `Secure`라서 **https가 아니면 로그인이 유지되지 않는다.** 운영 모드(`APP_ENV=production`)는 이 설정을
끄는 것을 거부한다. 그래서 도메인이 없어도 https는 필요하다. 선택지:

| 방법 | 설정 | 제약 |
|---|---|---|
| SSH 터널로 혼자 쓰기 | `SITE_ADDRESS=localhost`. 내 PC에서 `ssh -L 8443:localhost:443 <서버>` 후 `https://localhost:8443` | Caddy 내부 CA의 자체 서명 인증서라 브라우저 경고가 뜬다. 다른 사람은 못 쓴다. 80/443을 밖에 열 필요가 없다 |
| IP 주소로 열기 | `SITE_ADDRESS=<공인 IP>` | 자체 서명 → 모든 사용자에게 브라우저 경고. 시연·사용자 테스트에는 맞지 않는다 |
| 무료 DNS 이름 받기 | DuckDNS 같은 서비스로 이름을 받아 `SITE_ADDRESS`에 | 정상 인증서. 사실상 "도메인 있음"과 같다 — 가장 권한다 |

자체 서명일 때 스모크 테스트는 `--insecure`(`deploy.sh`는 localhost·IP면 알아서 붙인다).
세 방법 모두 실제로 해 보지는 못했다(로컬 `localhost` 구성만 검증).

## 14. 로컬에서 운영 구성 연습하기

개발 PC(Docker Desktop)에서 80/443이 막혀 있으면 포트만 바꿔 띄운다. `LLM_PROVIDER=fake`로 비용 없이 된다.
```bash
cp .env.production.example /tmp/pz.env      # SITE_ADDRESS=localhost, HTTP_PORT=8180, HTTPS_PORT=8143,
                                            # LLM_PROVIDER=fake, ANTHROPIC_API_KEY=, 비밀번호·초대 코드·관리자 채움
export PZ_ENV_FILE=/tmp/pz.env PZ_PROJECT=pzprod
scripts/ops/deploy.sh --no-pull
curl -k "https://localhost:8143/healthz?deep=true"
scripts/ops/compose.sh down -v              # 연습이 끝나면 볼륨까지 지운다 (운영에서는 -v 금지)
```
포트를 바꾸면 http→https 리다이렉트는 여전히 443을 가리킨다(운영은 80/443이라 문제없다).

## 15. 검증한 것과 못 한 것 (2026-11-11)

### 직접 돌려 확인한 것 (Windows 11 + Docker Desktop, Engine 29.9 / Compose 5.6, 가짜 모델, `SITE_ADDRESS=localhost`)
- 이미지: 첫 빌드 3분 33초, `docker images` 표시 3.02GB(전송 0.76GB). 코드 한 줄 바꾼 뒤 재빌드 3.8초.
  네트워크를 끊은 컨테이너(`--network none`)에서 e5 모델 로딩, 비루트(uid 10001).
- 기동: 빈 볼륨에서 다섯 서비스 모두 healthy까지 27초. `/healthz?deep=true` 200(postgres·neo4j ok), `/` 200,
  http→https 308, HSTS 있음, `Server`·`Via` 없음, 응답 압축(zstd).
- 스모크 테스트 17단계 통과(Caddy 경유: 컨테이너 안에서, 그리고 호스트에 연 포트로). C 조건 첨삭이 내장 모델로 검색.
- 호스트에 열린 포트는 caddy의 두 개뿐. DB 컨테이너는 인터넷에 못 나가고 backend는 나간다. caddy에서 DB로는 못 간다.
- 프록시 뒤 클라이언트 IP: backend 로그에 Caddy 주소가 아니라 Caddy가 본 상대 주소가 찍힌다. 클라이언트가 보낸
  `X-Forwarded-For`는 무시된다.
- 본문 상한: 3.2MB 요청은 Caddy가 413 JSON으로 끊고, 2.9MB는 앱까지 간다.
- backend를 내렸을 때 502 JSON / "점검 중" 화면, 다시 올리면 8초 만에 정상.
- `down` → `up` 뒤 데이터 유지(계정·세션·메시지 1,500건).
- 백업 → 다른 이름 DB로 복원 연습(행 수 일치) → 운영 DB 실제 복원(삭제한 데이터가 돌아옴, 14초). 보존 정리(20일 지난 파일 삭제).
- Neo4j 덤프 → 노드 삭제 → 복원(노드가 돌아옴).
- DB·Neo4j 비밀번호 교체 절차, `run.sh`로 운영 스크립트 실행, Caddy 설정 다시 읽기.
- Neo4j를 내린 채 backend를 다시 띄워도 뜬다(`/healthz` 200, `?deep=true` 503 → Neo4j가 돌아오면 200).
  다섯 컨테이너를 한꺼번에 재시작하면 33초 뒤 모두 healthy.
- **서버 흐름 리허설**: 저장소를 임시 폴더에 새로 받아 이 문서 순서대로 — `.env.production` 작성 →
  `deploy.sh --no-pull`(처음 배포, 62초) → 원격에 새 커밋 → `deploy.sh`(pull → 백업 → 빌드 → backend만 교체 →
  스모크, 47초) → `rollback.sh` → 복원 연습. 자리표시자가 남은 설정 파일은 배포가 거부된다.

### 통합 뒤 다시 확인한 것 (2026-11-11, 다섯 갈래를 합치고 검토 지적을 고친 최종 코드, 가짜 모델)
- `scripts/ops/deploy.sh --no-pull`을 임시 env(운영 모드, 초대 코드·동의 필수, 요청 제한 120/20, 그래프 상한 300/1000)로:
  이미지 빌드 → 기동 → 스모크 17단계가 41~58초에 통과(의존성 레이어까지 다시 깔면 5분 32초). 마이그레이션 0008·0009 적용.
- 운영 모드 검사 통과, `/docs` 404, 보안 헤더(CSP·HSTS), http→https 308, Host 검사, 헤더 스텁 거부.
- 관리자 이름은 화면 가입 불가(409) → `scripts/ops/run.sh scripts/reset_password.py --username <이름> --create`로 만든 계정으로
  로그인 → `/v1/admin/*`와 `/admin` 화면. Bearer 토큰으로 CSRF 없이 쓰기 요청. `scripts/ops/backup.sh`.
- 브라우저(운영 모드 백엔드를 localhost 평문 포트로 열어서 — 내장 브라우저가 Caddy의 자체 서명 인증서를 받지 않는다):
  초대 코드+동의 가입 → 카카오톡 형식 붙여넣기 미리보기 → 가져오기(실제 e5 임베딩) → 자동 조건 첨삭 → 내 데이터·설정 →
  관리자 동의 화면 → 운영 대시보드. 콘솔 오류 없음.
- 개발 DB 복사본에서 0007 → 0008 → 0009 마이그레이션과 기존 데이터(상대 20, 메시지 112, 첨삭 21)로 새 API·화면.
- 검토에서 서버를 23초 멈추던 30KB 요청: 실제 서버에서 0.09초에 422, 그동안 헬스체크 지연 없음.
- `backend/constraints.txt`: 다시 빌드한 이미지의 `pip freeze`가 고정 파일과 79줄 모두 일치.
- GitHub Actions(`main` 푸시, 2026-11-11): `backend`·`data-files`·`deploy` 세 작업 통과. `deploy` 잡은 러너에서 처음 돌았고
  고친 것 없이 5분 14초 — 운영 이미지 빌드 3분 44초(빌드 캐시 없이), 이미지 자체 점검 10초, 운영 스택 기동 48초, Caddy 경유 스모크 10초.

### 하지 못한 것
- **실제 리눅스 VM, 실제 도메인, Let's Encrypt 발급·갱신.** 인증서 자동 발급은 Caddy의 기본 동작에 기대고 있고,
  여기서는 내부 CA(자체 서명)만 확인했다. 3장의 1단계(Docker 설치·스왑)와 4단계(DNS·방화벽)는 적어만 두었고
  돌려 보지 못했다.
- **운영 구성(Caddy 뒤)에서의 실제 Claude 호출.** 새 코드 자체는 실제 모델로 확인했다(2026-11-11: 첨삭 6회, 그래프 수집 3건 — `decisions.md` "배포 준비 ⑥"). 운영 스택에 실제 키를 넣은 스모크(`deploy.sh --smoke-full`)는 첫 배포 때 돌린다.
- **ARM 서버**(Graviton, Ampere). 이미지를 amd64에서만 빌드했다.
- **작은 서버에서의 성능.** 2 vCPU·4GB에서 빌드가 되는지, 가져오기가 얼마나 걸리는지 재지 못했다.
- **`release.yml`(태그 푸시 → GHCR).** YAML 문법만 확인했고 실행된 적이 없다. `ci.yml`의 deploy 잡은 `main` 푸시에서 통과했다(위).
- **IPv6로 들어오는 접속의 클라이언트 IP.** Docker가 IPv6를 프록시로 넘길 때 주소가 게이트웨이로 바뀔 수 있다.
  그러면 IP 기준 제한이 IPv6 사용자를 한 묶음으로 본다. DNS에 AAAA 레코드를 두지 않으면 피할 수 있다.
- **서버 재부팅 뒤 자동 복구.** `restart: unless-stopped`에 기대고 있다. 컨테이너를 한꺼번에 재시작하는 것까지만
  해 봤고, Docker 데몬이나 서버 자체를 재시작해 보지는 못했다(개발 스택이 같은 PC에 떠 있어서).
- **오래 돌렸을 때**의 로그 순환, 인증서 갱신, 디스크 증가.
- 스모크 테스트는 파이썬 3.12로만 돌렸다. Ubuntu 22.04의 3.10은 문법 호환만 확인했다.
- `make` 대상은 `make -n`으로 펼친 명령만 확인했다(이 PC에 make가 없다). 같은 명령은 직접 돌렸다.

### 알려진 한계
- 80번 포트의 https 리다이렉트 응답에는 `Server: Caddy`가 남는다(Caddy가 자동으로 만드는 응답, 버전 없음).
- 무중단 배포가 아니다. backend가 다시 뜨는 10~20초 동안 요청이 502를 받는다.
- 백업의 서버 밖 복사, 모니터링·알림은 이 저장소에 없다.
- 의존성 버전 고정 파일이 없어 빌드 시점마다 패키지 버전이 달라질 수 있다.
