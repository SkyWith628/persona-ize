# 배포 안내 — 리눅스 VM 한 대 + Docker Compose + Caddy 자동 HTTPS

처음 보는 사람이 위에서부터 따라 하면 서비스가 `https://<도메인>`으로 뜨도록 쓴 문서다.
끝의 [검증한 것과 못 한 것](#15-검증한-것과-못-한-것-2026-11-11)을 먼저 읽어 두면 어디서 막힐 수 있는지 안다.
옛 브랜치(`design/iphone-mobile`)로 올려 둔 서버를 main으로 옮길 때는 [16장](#16-옛-브랜치로-올린-서버를-main으로-다시-올리기-db-새로-만들기).

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
| `POSTGRES_PASSWORD`, `NEO4J_PASSWORD` | `openssl rand -base64 32 \| tr -d '/+='` 로 각각 만든다. Neo4j는 8자 이상. 영문·숫자·`-`·`_`만 쓴다(접속 주소에 그대로 들어가서, 다른 문자가 있으면 `deploy.sh`가 멈춘다) |
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
관리자는 두 가지로 정해진다: 아이디가 `ADMIN_USERNAMES`에 있거나(대소문자까지 같아야 한다), 권한(`users.role`)이
`admin`이다. 먼저 이름으로 지정하는 쪽 — 운영에서는 그 아이디를
**화면 가입으로 만들 수 없게 막아 두었다**(다른 사람이 먼저 차지하는 것을 막으려고 — 시도하면 "이미 사용 중인 이름"으로 보인다).
관리자 계정은 서버에서 만든다. 비밀번호는 물어본다(8자 이상):
```bash
scripts/ops/run.sh scripts/reset_password.py --username <관리자 아이디> --create
```
만든 뒤 `https://<도메인>`에서 그 아이디로 로그인하고 `https://<도메인>/admin`을 연다. 계정이 아직 없으면 backend가
시작할 때 로그에 "관리자 계정이 아직 없습니다" 경고를 남긴다.

권한으로 관리자를 만들 때(`ADMIN_USERNAMES`를 비워 두었거나 관리자를 더 둘 때)는 `app.tools.create_user`를 쓴다.
앱에 들어 있는 모듈이라 `run.sh`(저장소의 `scripts/*.py`용)가 아니라 compose로 바로 돌린다:
```bash
scripts/ops/compose.sh run --rm --no-deps backend python -m app.tools.create_user <아이디> --admin
```
- 없는 아이디면 관리자(role=admin, 승인제여도 바로 active)로 만들고 비밀번호를 화면에 보이지 않게 두 번 묻는다.
  이미 있는 아이디면 비밀번호는 건드리지 않고 권한만 올린다. `--reset-password`를 붙이면 비밀번호도 새로 정한다.
- 비밀번호는 명령 인자·환경변수로 받지 않는다. 자동화할 때는 표준입력 첫 줄로 넘긴다(`… -T backend python -m
  app.tools.create_user <아이디> --admin < 비밀번호파일`). 입력을 가릴 수 없는 터미널 창(Windows의 Git Bash)에서는
  묻지 않고 끝내므로 `winpty`를 앞에 붙이거나 PowerShell에서 실행한다.
- 그 뒤로는 `/admin`의 회원 관리에서 다른 회원을 관리자로 올리거나 내릴 수 있다. **활성 관리자가 0명이 되는 변경은
  거절된다**(409 `last_admin` — 마지막 관리자의 권한 내리기·정지·삭제·본인 탈퇴). 활성 관리자는 상태가 active이고
  role이 admin이거나 `ADMIN_USERNAMES`에 있는, 실제로 있는 계정이다.

### 7-1단계 — 소셜 로그인 켜기 (선택)
기본은 꺼져 있다. 켜려면 `.env.production`에 아래를 채우고 `scripts/ops/compose.sh up -d`로 backend를 다시 만든다.

| 설정 | 값 |
|---|---|
| `PUBLIC_BASE_URL` | 밖에서 보이는 주소, 끝에 `/` 없이. 예) `https://persona.example.com`. 소셜 로그인을 켰는데 이 값이 `https://`로 시작하지 않으면 운영 모드는 시작하지 않는다 |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google Cloud Console → API 및 서비스 → 사용자 인증 정보 → OAuth 클라이언트 ID(웹 애플리케이션) |
| `KAKAO_CLIENT_ID`, `KAKAO_CLIENT_SECRET` | Kakao Developers → 내 애플리케이션. ID는 **REST API 키**, 시크릿은 보안 → Client Secret("사용함"으로 켠다) |

제공자마다 ID와 시크릿이 **둘 다** 있어야 그 제공자가 켜진다(하나만 있으면 꺼진 채로 시작 로그에 경고가 남는다).
각 제공자 콘솔에 **redirect URI**를 글자 그대로 등록한다:
```
<PUBLIC_BASE_URL>/v1/auth/oauth/google/callback      # Google: "승인된 리디렉션 URI"
<PUBLIC_BASE_URL>/v1/auth/oauth/kakao/callback       # Kakao: 카카오 로그인 → Redirect URI
```
- Kakao는 카카오 로그인을 "활성화"로 두고, 이메일을 받으려면 동의 항목에서 카카오계정(이메일)을 켠다(없어도 로그인은
  된다 — 이메일 없는 계정이 만들어진다).
- 켜졌는지는 `curl -s https://<도메인>/v1/auth/providers`의 `google`·`kakao` 값으로 본다.
- 새 계정은 가입 정책을 그대로 따른다: 초대 코드·동의가 필요한 서버에서는 화면이 그 값을 함께 보내고, 승인제
  (`AUTH_SIGNUP_REQUIRES_APPROVAL=true`)면 첫 소셜 로그인은 승인 대기로 끝난다(관리자가 `/admin`에서 승인).
- Caddy 접근 로그에는 콜백 주소의 `code`·`state` 값이 `REDACTED`로 남는다(`docker/caddy/Caddyfile`).
- 실제 Google·Kakao 왕복은 앱 키가 없어 확인하지 못했다(가짜 제공자로만 검증) — 처음 켤 때 직접 로그인해 본다.

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
- 메시지 하나가 거듭 실패하면(`GRAPH_EPISODE_MAX_ATTEMPTS`, 기본 3번 — 연결·요청 한도·5xx 같은 일시 장애는 세지 않는다) 그 메시지만 보류하고 나머지를 계속 넣는다. 로그에 `graph ingest gave up episode=<방:메시지>`가 남고, 건수는 관리자 화면(`/admin`) 수집 표의 "거듭 실패해 보류된 메시지"에 보인다. 다시 시도하는 방법은 둘이다: ① 그 줄의 **다시 시도** 버튼(`POST /v1/admin/graph/retry-held`, 전체) 또는 아래 "보류된 메시지 (대화방별)" 표의 줄마다 있는 버튼(그 대화방만) — 보류만 풀어 다시 넣는다. 그래프는 그대로이고, 보류 건수만큼 모델 호출 비용이 들며 수집 상한을 따른다. 또 실패하면 다시 보류된다. ② `scripts/ops/run.sh scripts/graph_reset.py --user <id> --conversation <방>` — 그 방(또는 사용자)의 그래프를 지우고 처음부터 다시 만든다(상한 없음, 전체 메시지만큼 비용). 스키마·프롬프트를 바꿨을 때만 쓴다.
- 서버를 다시 띄우면 수집하다 만 메시지를 이어서 넣는다(`GRAPH_RESUME_ON_START=true`).
- 서버를 내릴 때(배포 포함) 뒤에서 도는 묶음 생성·그래프 수집은 `SHUTDOWN_GRACE_SECONDS`(기본 15초)까지만 기다린다. 수집은 넣던 메시지까지만 끝내고 멈추며, 남은 것은 다시 뜬 뒤 이어받는다. 로그: `graph ingest stopped for shutdown`, 시간을 넘겨 끊으면 `shutdown: cancelled N background task(s)`. 큰 가져오기 도중에 재시작하면
  남은 분량만큼 비용이 이어진다.
- 관리자 화면의 비용 환산(`COST_*`)은 0이면 표시하지 않는다. 예시 파일의 값은 2026-11-11 기준 `claude-opus-5-5`(입력 4 / 출력 20)와 그래프 실측 평균(0.035/건)이다 — `LLM_MODEL`을 바꾸거나 요금이 바뀌면 함께 고친다.
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
- [ ] LangSmith 추적 변수(`LANGSMITH_TRACING`·`LANGSMITH_TRACING_V2`·`LANGCHAIN_TRACING_V2`·`LANGCHAIN_TRACING`·
      `LANGCHAIN_HANDLER`)가 `.env.production`에도 서버 셸에도 없다. 첨삭 워크플로(LangGraph)와 함께 `langsmith`가
      깔려 있어, 켜지면 프롬프트에 든 초안·대화 원문이 안내문에 없는 곳(LangSmith)으로 나간다. 있으면 backend가
      시작을 거부한다(`false`·`0`·빈 값은 통과). `LANGSMITH_API_KEY`만 있으면 경고만 한다
- [ ] `ADMIN_USERNAMES`의 계정을 서버에서 만들었다(7단계) — backend 시작 로그에 "관리자 계정이 아직 없습니다" 경고가 없다
- [ ] Console 월 한도, 전체 차단기(`USAGE_GLOBAL_DAILY_*`), 그래프 상한(`GRAPH_INGEST_*`)이 0이 아니다
- [ ] 방화벽: 들어오는 포트는 80·443(+SSH는 내 IP). `scripts/ops/compose.sh ps`에서 포트가 보이는 것은 caddy뿐
- [ ] `curl -sI https://<도메인>/`에 `strict-transport-security`가 있고 `server`·`via`가 없다.
      https가 며칠 안정된 뒤 `HSTS_MAX_AGE`를 300 → 31536000으로 올렸다
- [ ] `https://<도메인>/docs`가 열리지 않는다(운영 모드)
- [ ] 백업이 생기고(`scripts/ops/backup.sh --list`) 서버 밖 사본이 있다. 복원 연습을 한 번 했다
- [ ] 개인정보 처리방침(`/privacy`)·이용약관(`/terms`) 문구가 실제 보존 기간(`REWRITE_RETENTION_DAYS`)·삭제 방법과 맞다
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
| `compose ps`에서 backend가 `Restarting` | 시작 명령이 실패한다. `logs --tail 50 backend`: ① 마이그레이션 실패 ② 운영 모드 검사가 설정을 거부(메시지에 어떤 설정인지 나온다 — `LangSmith 추적 변수가 설정돼 있습니다`면 괄호 안의 변수를 `.env.production`과 셸에서 지운다) ③ DB 비밀번호 불일치(`password authentication failed` → 11장) |
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

## 16. 옛 브랜치로 올린 서버를 main으로 다시 올리기 (DB 새로 만들기)

대상은 `https://43-203-31-7.sslip.io`(AWS 서울 EC2 한 대, 4GB, 고정 IP 43.203.31.7)다. 2026-11-17 기준으로 이 서버에는
main이 아니라 옛 브랜치 `design/iphone-mobile`의 11-16 빌드가 떠 있다. 위에서부터 따라 하면 main이 뜨도록 썼다.
이 절에서 **main은 PR #8(`port/members-mobile` — 회원 상태·승인제·소셜 로그인, 마이그레이션 0012)이 병합된 뒤의 main**이다
(같은 기능의 PR #7은 닫았다 — `docs/decisions.md` 2026-11-17). 계정·관리자·소셜 로그인의 일반 설명은 3장 7단계와 7-1단계에
있고, 여기에는 이 서버에서 달라지는 것만 적는다.
**이 절은 저장소의 파일만 읽고 썼다 — 서버에 접속해 보지 않았고, 어느 명령도 그 서버에서 돌려 보지 못했다**
([16.11](#1611-확인한-것과-못-한-것-2026-11-17)).

### 16.1 왜 DB를 새로 만드나
- 서버 DB는 옛 브랜치의 마이그레이션 `0008_members_social_login`(revision id `"0008"`: `users.email`·`role`·`status`,
  소셜 로그인 표)으로 만들어졌다. main의 `0008_deploy_ready`도 revision id가 `"0008"`인데 내용이 다르다
  (`users.consented_at`·`consent_version`, `feedback.reason_code`, `admin_access_log`).
- alembic은 번호만 본다. 그 DB에 main을 올리면 0008을 "적용됨"으로 건너뛰어 `users.consented_at`이 없는 채로 뜨고
  (로그인 500), `0012_members_social_login`까지 가면 이미 있는 `users.email`을 다시 더하려다 실패해 backend가 재시작을
  반복한다(0012의 첫 줄이 `users.email` 추가다. 옛 DB로 실제로 올려 본 것은 아니다).
- 그래서 **PostgreSQL 볼륨을 새로 만든다.** 사용자 id가 전부 새로 생기므로, 그 id(`group_id`)로 묶인
  **Neo4j 그래프도 함께 비운다.** 기존 계정·대화·첨삭 이력·그래프는 새 서버로 넘어오지 않는다. 사용자는 다시 가입한다.

옛 스택과 main의 차이(옛 것은 `git show design/iphone-mobile:<파일>`로 읽은 것 — 서버에 실제로 그렇게 떠 있는지는 0단계에서 본다):

| | 옛 스택 (`design/iphone-mobile`) | main |
|---|---|---|
| env 파일 | `.env.prod` | `.env.production` |
| compose 프로젝트 이름 | 지정 없음 → **서버 폴더 이름**(예: 폴더가 `persona-ize`면 `persona-ize`) | `persona-ize-prod` 고정 |
| 서비스 | `postgres` · `neo4j` · `backend` · `proxy`(caddy:2) | `postgres` · `neo4j` · `backend` · `caddy` · `backup` |
| 볼륨 | `postgres-data` · `neo4j-data` · `hf-cache` · `caddy-data` · `caddy-config` | `postgres-data` · `neo4j-data` · `neo4j-logs` · `caddy-data` · `caddy-config` |
| 코드 올린 방법 | rsync (서버 폴더가 git 저장소가 아닐 수 있다) | git |
| 계정 만들기 | `python -m app.tools.create_user` | 둘 다 있다(3장 7단계): `scripts/ops/run.sh scripts/reset_password.py --username <이름> --create`(이름으로 지정한 관리자), `scripts/ops/compose.sh run --rm --no-deps backend python -m app.tools.create_user <아이디> --admin`(권한 관리자) |
| 승인제에서 가입 응답 | (옛 서버에서 확인하지 않았다) | 202 `{"status":"pending",…}`, 세션 없음. 승인 전 로그인은 403 `account_pending` |

볼륨의 실제 이름은 `<프로젝트>_<볼륨>`이다. 프로젝트 이름이 다르면(예상) **main을 올리는 것만으로 빈 DB·빈 그래프가
새 볼륨에 만들어지고, 옛 볼륨은 손대지 않은 채 남는다** — 확인이 끝날 때까지 되돌아갈 곳으로 둔다.
옛 폴더 이름이 `persona-ize-prod`였다면 이름이 겹친다 → 3단계의 "이름이 겹칠 때".

### 16.2 0단계 — 시작 전 확인

**(내 PC에서) 올릴 코드**
- [ ] PR #8(`port/members-mobile` — 회원 상태·소셜 로그인, 마이그레이션 0012)이 main에 병합됐고 CI가 통과했다.
      **병합 전의 main을 올리지 않는다** — `AUTH_SIGNUP_REQUIRES_APPROVAL`을 모르는 코드라 승인제가 조용히 꺼진 채 가입이 열린다.
      닫힌 PR #7의 브랜치(`feat/members-social-login`)도 올리지 않는다 — 0012가 번호만 같고 스키마가 다르다. 서버에서
      받은 코드가 PR #8의 것인지는 16.6의 확인 줄로 본다.
- [ ] 올릴 커밋을 정했다. 태그를 붙이면(지금 있는 태그는 `v0.1.0`뿐. 예: `git tag v0.2.0 && git push origin v0.2.0`)
      `release.yml`이 GHCR에 이미지도 올린다. 태그 없이 main의 커밋 해시로 올려도 된다.
- [ ] 아이폰 앱·웹 사용자에게 "계정과 데이터가 초기화되고 다시 가입해야 한다"를 알렸다.

**(서버에서) 지금 무엇이 떠 있나** — 옛 스택의 폴더·프로젝트·컨테이너·볼륨 이름은 **모른다.** 아래로 찾아 적어 둔다:
```bash
docker version --format '{{.Server.Version}}' && docker compose version   # Engine 25 이상, Compose 2.24 이상이어야 한다(3장 1단계)
docker compose ls -a          # 프로젝트 이름(NAME)과 compose 파일 경로(CONFIG FILES) → <옛 프로젝트>, <옛 폴더>
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'      # postgres 컨테이너 이름 → <옛 postgres>
docker volume ls              # <옛 프로젝트>_postgres-data, _neo4j-data, _hf-cache, _caddy-data, _caddy-config 가 보일 것이다
df -h / && docker system df   # 새 이미지(약 3GB)와 빌드 캐시가 들어갈 자리. 여유가 8GB 아래면 4단계 전에 정리한다
free -h                       # 스왑 2GB가 있는지 (3장 1단계)
```
옛 스택은 Ubuntu 패키지(`docker.io`·`docker-compose-v2`)로 Docker를 깔았다(옛 `ec2_bootstrap.sh`). 버전이 모자라면
`deploy.sh`의 `up --wait`·`start_interval`이 안 된다 — 3장 1단계 방식으로 올리되, 그 전에 아래 백업을 서버 밖에 둔다
(패키지를 바꾸는 절차는 확인하지 못했다).

**지킬 데이터가 있는지 본다** (읽기만 한다):
```bash
docker exec <옛 postgres> sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select version_num from alembic_version"'
#   → 0008 이면 이 절의 전제(옛 브랜치 스키마)가 맞다. 아래 열 목록에 email·role·status가 있고 consented_at이 없으면 확실하다
docker exec <옛 postgres> sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select column_name from information_schema.columns where table_name = '"'"'users'"'"' order by 1"'
docker exec <옛 postgres> sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select relname, n_live_tup from pg_stat_user_tables order by 2 desc"'
#   → 표별 행 수(어림값). users·messages·rewrite_requests 가 시험 계정뿐인지, 남의 실제 대화가 있는지 본다
docker exec <옛 postgres> sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select username, created_at from users order by created_at"'
```
`alembic_version`이 0008이 아니거나 `users`에 `consented_at`이 있으면 **멈춘다** — 전제가 다르다(누가 이미 다른 코드를
올렸다). 실제 사용자의 대화가 있으면 그 사용자에게 먼저 알리고, 필요하면 옛 화면에서 내보내게 한 뒤 진행한다.

### 16.3 1단계 — 백업 (지킬 것이 없어 보여도 뜬다)
새 스택의 `scripts/ops/backup.sh`는 옛 스택을 모른다. 옛 컨테이너에서 직접 뜬다:
```bash
mkdir -p ~/pz-old && chmod 700 ~/pz-old
docker exec <옛 postgres> sh -c 'pg_dump -U "$POSTGRES_USER" --format=custom "$POSTGRES_DB"' > ~/pz-old/pz-old-0008-$(date -u +%Y%m%dT%H%M%SZ).dump
ls -lh ~/pz-old/ && docker exec -i <옛 postgres> pg_restore --list < ~/pz-old/pz-old-0008-*.dump | head -5    # 읽히는 덤프인지
cp <옛 폴더>/.env.prod ~/pz-old/env.prod.bak && chmod 600 ~/pz-old/*      # 옛 설정(API 키·옛 DB 비밀번호). 되돌릴 때 쓴다
```
- 덤프에는 대화 원문이, `env.prod.bak`에는 API 키가 들어 있다. 서버 밖에 둘 때는 암호화한다(6장).
- **이 덤프는 옛 브랜치 코드로만 읽힌다.** main 스택에 `scripts/ops/restore.sh`로 넣으면 16.1과 같은 이유로 backend가 뜨지 않는다.
- Neo4j는 덤프하지 않는다. 옛 볼륨을 남겨 두는 동안은 그 볼륨이 백업이고, 지운 뒤에는 PostgreSQL 원문에서 다시 만든다(비용 — 6장).

### 16.4 2단계 — 인증서를 어떻게 할지 정한다
새 스택의 인증서 볼륨은 `persona-ize-prod_caddy-data`로, 옛 것(`<옛 프로젝트>_caddy-data`)과 다른 볼륨이다. 아무것도 하지
않으면 Caddy가 같은 이름(`43-203-31-7.sslip.io`)의 인증서를 **새로 받는다.**

Let's Encrypt 발급 한도(https://letsencrypt.org/docs/rate-limits/ — 수치는 바뀔 수 있으니 그 문서가 기준이다): 같은 이름
묶음의 인증서는 7일에 5장, 등록 도메인 하나에 7일 50장. `sslip.io`는 여러 사람이 함께 쓰는 도메인이라 뒤쪽 한도가
남의 발급에 영향을 받는지는 **확인하지 못했다.**

- **(권장) 옛 인증서를 새 볼륨으로 옮긴다** — 발급 자체를 하지 않는다. 3단계에서 옛 스택을 내린 직후에 한다:
  ```bash
  docker volume create persona-ize-prod_caddy-data
  docker run --rm --entrypoint sh -v <옛 프로젝트>_caddy-data:/from:ro -v persona-ize-prod_caddy-data:/to \
    caddy:2-alpine -c 'cp -a /from/. /to/ && ls /to/caddy/certificates/*/'
  ```
  마지막 `ls`에 `43-203-31-7.sslip.io` 폴더가 보이면 됐다. 나중에 compose가 "이 볼륨은 Compose가 만든 것이 아니다"라고
  경고할 수 있다(그대로 쓴다). **이 복사는 해 보지 못했다** — 안 되면 아래로 간다.
- **새로 받아도 되는 조건**: 최근 7일 동안 이 이름으로 발급된 인증서가 4장 이하다. 브라우저에서
  `https://crt.sh/?q=43-203-31-7.sslip.io`를 열어 날짜를 센다(11-16에 처음 올렸으니 보통 1~2장이다). 조건이 맞으면 옮기지
  않고 그냥 올려도 된다. 발급이 거절되면 `scripts/ops/compose.sh logs caddy | grep -i -E "acme|obtain|rate"`에 사유가
  남고, 한도가 풀릴 때까지 **반복해서 재시작하지 않는다.**
- 어느 쪽이든 **옛 `caddy-data` 볼륨은 새 서버가 https로 열리는 것을 본 뒤에 지운다.**
- 고정 IP와 `SITE_ADDRESS`는 그대로다. DNS·보안 그룹(80·443, SSH는 내 IP)은 바꿀 것이 없다.

### 16.5 3단계 — 옛 스택 내리기
여기서부터 서비스가 끊긴다(새 스택이 뜰 때까지 — 서버에서 빌드하면 10분 안팎으로 본다. 4GB 서버의 빌드 시간은 재지 못했다).
옛 스택이 80·443을 잡고 있어서 내리지 않으면 새 Caddy가 뜨지 못한다.
```bash
cd <옛 폴더>
docker compose --env-file .env.prod -f docker-compose.prod.yml down        # -v 를 붙이지 않는다: 볼륨(데이터)은 남는다
docker ps        # 옛 컨테이너가 없어야 한다
```
옛 폴더의 compose 파일이 없거나 깨졌으면 `docker compose -p <옛 프로젝트> down`(파일 없이 프로젝트 이름으로 내린다 — 해 보지 못했다).
인증서를 옮기기로 했으면 지금 16.4의 복사를 한다.

**볼륨 — 무엇을 남기고 무엇을 지우나**

| 볼륨 | 처리 |
|---|---|
| `<옛 프로젝트>_postgres-data` · `_neo4j-data` | **확인이 끝날 때까지(16.9) 남긴다** — 되돌아갈 곳이다. 새 스택은 이 볼륨을 쓰지 않는다 |
| `<옛 프로젝트>_caddy-data` | 새 서버가 https로 열린 뒤에 지운다 |
| `<옛 프로젝트>_hf-cache` · `_caddy-config` | 지금 지워도 된다(모델 캐시 — main 이미지는 모델을 안에 담는다 / Caddy 자동 저장 설정). 디스크가 모자랄 때 먼저 지운다 |

```bash
docker volume rm <옛 프로젝트>_hf-cache <옛 프로젝트>_caddy-config        # 되돌릴 때 옛 backend가 모델을 다시 받는다(수 분)
```
옛 이미지(`<옛 프로젝트>-backend`)도 확인이 끝날 때까지 지우지 않는다(되돌릴 때 다시 빌드하지 않으려고).

**이름이 겹칠 때**(`docker volume ls`에 이미 `persona-ize-prod_postgres-data`가 있다 = 옛 폴더 이름이 `persona-ize-prod`):
새 스택이 옛 DB를 그대로 물고 떠서 16.1의 문제가 난다. 이때만 올리기 전에 지운다.
```bash
# ⚠ 되돌릴 수 없다. 1단계의 덤프가 읽히는 것을 확인한 뒤에만. 지우면 되돌아갈 곳은 그 덤프뿐이고 그래프는 사라진다.
docker volume rm persona-ize-prod_postgres-data persona-ize-prod_neo4j-data
```
`persona-ize-prod_caddy-data`는 **지우지 않는다** — 새 스택이 그대로 이어 쓴다(인증서 복사가 필요 없다).

### 16.6 4단계 — 코드 올리기 (rsync가 아니라 git)
옛 폴더는 지우지 않고 이름만 바꿔 둔다(되돌릴 때 쓴다. 폴더 이름을 바꿔도 볼륨 이름은 그대로다 — 그래서 되돌릴 때
`-p <옛 프로젝트>`를 준다). 새 폴더는 git으로 받는다. 저장소는 비공개다.
```bash
cd ~ && mv <옛 폴더> ~/pz-old/code          # 옛 폴더가 ~/persona-ize 가 아니면 생략해도 된다
```
**서버에 저장소 접근 권한이 없을 때 — 배포 키(읽기 전용)**:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/pz_deploy -N '' -C pz-ec2-deploy && cat ~/.ssh/pz_deploy.pub
#   → GitHub 저장소 Settings → Deploy keys → Add deploy key (Allow write access 는 끈다)
GIT_SSH_COMMAND='ssh -i ~/.ssh/pz_deploy -o IdentitiesOnly=yes' git clone git@github.com:SkyWith628/persona-ize.git ~/persona-ize
cd ~/persona-ize && git config core.sshCommand 'ssh -i ~/.ssh/pz_deploy -o IdentitiesOnly=yes'      # 이후의 git pull·fetch 용
```
(https로 받을 수 있는 계정이 서버에 있으면 3장 2단계의 `git clone https://…` 그대로.)

올릴 커밋으로 맞춘다:
```bash
cd ~/persona-ize
git checkout v0.2.0                 # 0단계에서 정한 태그. 태그 없이 main 이면 이 줄을 건너뛴다(clone 직후가 main이다)
git log --oneline -1 && ls backend/migrations/versions | tail -1        # 0012_members_social_login.py 여야 한다
grep -c email_verified backend/migrations/versions/0012_members_social_login.py     # 1 이상이어야 한다
ls backend/app/tools/create_user.py                                     # 있어야 한다
grep -c AUTH_SIGNUP_REQUIRES_APPROVAL .env.production.example           # 1 이상이어야 한다
```
**넷 중 하나라도 어긋나면 멈춘다.** 0012 파일이 없거나 마지막 줄이 0이면 PR #8 이전의 main이다. 0012는 있는데
`email_verified`가 0이거나 `create_user.py`가 없으면 PR #8이 아닌 다른 0012다(`users.email_verified`와 `create_user`는
PR #8에만 있다 — `docs/decisions.md` 2026-11-17).
- 태그를 체크아웃하면 브랜치 위가 아니라서 `deploy.sh`의 `git pull --ff-only`가 실패한다 → 이 절은 전부 `--no-pull`로 돈다.
- 서버가 GitHub에 닿지 못할 때만: 내 PC에서 `git bundle create pz-main.bundle main` → `scp`로 서버에 → 서버에서
  `git clone -b main pz-main.bundle ~/persona-ize`. 이력과 커밋 해시가 그대로 온다(해 보지 못했다). rsync로 파일만 덮는
  방식은 쓰지 않는다 — 서버에 어느 커밋이 떠 있는지 알 수 없게 된다.

### 16.7 5단계 — 환경 파일
옛 `.env.prod`를 복사해 쓰지 않는다(이름·항목이 다르고, main의 `.env.production.example`에 운영 권장값이 들어 있다).
```bash
cd ~/persona-ize && cp .env.production.example .env.production && chmod 600 .env.production && nano .env.production
```
예시 파일에서 **이 서버에 맞게 바꿀 값**(나머지는 예시 그대로):

| 설정 | 이 서버의 값 | 이유 |
|---|---|---|
| `SITE_ADDRESS` | `43-203-31-7.sslip.io` | `https://` 없이 |
| `TRUSTED_HOSTS` | 비워 둔다 | compose가 `SITE_ADDRESS`를 넣는다 |
| `HSTS_MAX_AGE` | `300` 그대로 | 옛 Caddyfile은 1년(31536000)을 보냈다. 새 서버가 며칠 안정된 뒤 올린다 |
| `POSTGRES_PASSWORD` · `NEO4J_PASSWORD` | 각각 `openssl rand -base64 32 \| tr -d '/+='` 로 **새로** | 새 볼륨이라 이 값으로 초기화된다. 옛 값을 다시 쓰지 않는다. 개발용 기본값·8자 미만(Neo4j)·영문·숫자·`-`·`_` 밖의 문자는 거부된다 |
| `ANTHROPIC_API_KEY` | Console의 키 | `~/pz-old/env.prod.bak`의 키를 옮겨 적거나 새로 발급. Console 월 한도가 걸려 있는지 본다 |
| `ADMIN_USERNAMES` | 관리자 아이디 **하나** | 대소문자까지 같아야 한다. 이 이름은 화면 가입으로 만들 수 없다(7단계에서 서버에서 만든다). 이 계정은 회원 관리에서 정지·삭제되지 않는다(409 `protected_admin`). 비워 두고 권한(role) 관리자만 두는 길은 3장 7단계 |
| `AUTH_ALLOW_REGISTER` | `true` | 가입 열림 |
| `REGISTER_INVITE_CODE` | **6단계: 임시 코드(`openssl rand -hex 6`) → 8단계: 빈 값** | 아이폰 앱(`apps/ios/App/API.swift`)은 `POST /v1/auth/register`에 초대 코드를 보내지 않는다. 처음 올릴 때만 스모크용으로 잠근다 |
| `AUTH_SIGNUP_REQUIRES_APPROVAL` | **6단계: `false` → 8단계: `true`** | 목표는 승인제. `true`면 화면·앱·소셜 가입이 전부 승인 대기(`pending`)로 만들어지고, 관리자가 승인하기 전에는 로그인할 수 없다. 서버에서 만든 계정(7단계)은 바로 `active`다. 6단계에서 끄는 이유는 아래 |
| `REQUIRE_CONSENT` | `false` | 가입(`auth_service.signup`)은 `REQUIRE_CONSENT=true`인데 요청에 `consent: true`가 없으면 422 `consent_required`다. 앱은 `consent`를 보내지 않으므로 `true`면 **앱 가입이 전부 막힌다** |
| `GOOGLE_CLIENT_ID` · `KAKAO_CLIENT_ID` (+`_SECRET`) | 비워 둔다 | 소셜 로그인을 켤 때만(3장 7-1단계). 켜면 `PUBLIC_BASE_URL`이 `https://43-203-31-7.sslip.io`(경로 없이)가 아니면 시작을 거부한다 |
| `PUBLIC_BASE_URL` | 비워 둔다(소셜을 켜면 위 값) | |
| `USAGE_GLOBAL_DAILY_REWRITE_LIMIT` · `_TOKEN_LIMIT` | **0이 아닌 값**(예시 2000 / 4000000을 예산에 맞게 낮춘다) | 초대 코드 없이 가입이 열리면 둘 다 0일 때 시작을 거부한다 |
| `GRAPH_INGEST_DAILY_GLOBAL_CAP` · `GRAPH_INGEST_USER_MESSAGE_CAP` | **0이 아닌 값**(예시 1000 / 300, 하루 최악 약 US$30~40 — 8장) | 초대 코드 없이 가입이 열리면 하루 전체 상한이 0일 때 시작을 거부한다 |
| `USAGE_DAILY_REWRITE_LIMIT` · `_TOKEN_LIMIT` | 예시 200 / 400000 (옛 서버는 50 / 150000이었다) | 사용자 한 명의 하루 한도 |
| `ALLOW_UNCAPPED_COSTS` | `false` 그대로 | |
| `LLM_PROVIDER` · `LLM_MODEL` · `GRAPH_LLM_MODEL` | 예시 그대로(`anthropic` · `claude-opus-5-5` · `claude-haiku-4-5`) | 모델을 바꾸면 `COST_*`도 함께 |

운영 모드(`backend/app/core/guard.py`)가 **시작을 거부**하는 것 — 위 표대로면 걸리지 않는다:
`AUTH_ALLOW_HEADER_STUB=true` · `SESSION_COOKIE_SECURE=false` · 개발용 DB/Neo4j 비밀번호 · LangSmith 추적 변수(10장) ·
그래프 상한이 둘 다 0, 또는 (초대 코드 없는 열린 가입 + 그래프 하루 상한 0)인데 API 키가 있음 ·
(실제 모델 + 초대 코드 없는 열린 가입 + 전체 차단기 둘 다 0) · 소셜 로그인을 켰는데 `PUBLIC_BASE_URL`이 `https://도메인` 꼴이 아님.
**승인제는 이 검사에서 "가입자 수를 묶는 장치"로 쳐 주지 않는다**(검사는 가입 열림과 초대 코드만 본다) — 승인제를 켜도
차단기와 그래프 상한은 0이 아니어야 한다. `REQUIRE_CONSENT`는 이 검사에 없다(`false`여도 뜬다).

목표 설정에서 **남는 것이 정상인 경고**: backend 시작 로그와 `deploy.sh`의 "가입이 열려 있는데 REGISTER_INVITE_CODE가 비어
있습니다 …"(둘 다 승인제가 켜져 있는지는 보지 않고 낸다). 7단계 전에는 "관리자 계정이 아직 없습니다"도 나온다.

**왜 두 번에 나눠 올리나**: `scripts/smoke_test.py`는 임의 계정(`smoke-<16진수 8자>`)으로 가입한 뒤 상대 생성·첨삭까지 간다.
가입 단계는 201과 세션 쿠키를 기대하는데, 승인제가 켜진 서버는 202(세션 없음)를 준다 → "가입"에서 실패하고 `deploy.sh`가
"실패: 스모크"로 끝난다. PR #8은 스모크 테스트와 `deploy.sh`의 스모크 부분을 고치지 않았다(가입이 닫힌 서버만
`--health-only`로 바꿔 준다 — 승인제는 보지 않는다). 게다가 그 계정은 `pending`으로 **남는다**(스모크는 가입에 성공했을 때만
계정을 지운다). 코드로 읽은 것이다 — 돌려 보지 못했다. 그래서 처음에는 **초대 코드로 잠그고 승인제를 끈 채** 전체 흐름(실제
모델 첨삭 1회 포함)을 확인하고, 그다음 목표 설정으로 바꾼다. 스모크는 가입할 때 `consent: true`와 초대 코드(컨테이너의
`REGISTER_INVITE_CODE`)를 스스로 보내므로 `REQUIRE_CONSENT`는 처음부터 `false`여도 된다.

### 16.8 6~8단계 — 올리기, 관리자 계정, 목표 설정으로 바꾸기

**6단계 — 올리기** (`REGISTER_INVITE_CODE=<임시 코드>`, `AUTH_SIGNUP_REQUIRES_APPROVAL=false`인 상태로)
```bash
cd ~/persona-ize
grep -n CHANGE_ME .env.production          # 아무것도 나오지 않아야 한다
scripts/ops/deploy.sh --no-pull            # 설정 점검 → (새 DB라 백업 건너뜀) → 빌드 → 기동 → healthy 대기 → 스모크(--no-import: 첨삭 1회 비용)
```
- 빌드가 메모리 부족으로 죽으면(`Killed`, 종료 코드 137): 스왑을 확인하고(3장 1단계), 그래도 안 되면 태그로 올라간
  GHCR 이미지를 쓴다 — `.env.production`의 `BACKEND_IMAGE=ghcr.io/skywith628/persona-ize/backend:<태그>`,
  `docker login ghcr.io`, `scripts/ops/deploy.sh --no-pull --no-build`(4장. 서버에서 받아 띄워 보는 것은 아직 해 보지 못했다).
- backend가 `Restarting`이면 `scripts/ops/compose.sh logs --tail 50 backend`. `CRITICAL … 서버를 시작하지 않습니다` 아래에
  고칠 설정 이름이 나온다(12장).

마이그레이션이 0012까지 올라갔는지:
```bash
scripts/ops/compose.sh exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select version_num from alembic_version"'
#   → 0012
scripts/ops/compose.sh exec -T backend alembic current        # 0012 (head)  — 같은 것을 alembic으로
scripts/ops/compose.sh exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select count(*) from information_schema.columns where table_name = '"'"'users'"'"' and column_name in ('"'"'consented_at'"'"','"'"'consent_version'"'"','"'"'email'"'"','"'"'email_verified'"'"','"'"'role'"'"','"'"'status'"'"')" -c "select to_regclass('"'"'admin_access_log'"'"'), to_regclass('"'"'user_identities'"'"')"'
#   → 6, 그리고 두 표 이름이 그대로 나온다(빈 칸이면 그 표가 없다). main의 0008과 PR #8의 0012가 둘 다 실제로 적용됐다는 뜻이다
#     (5면 email_verified가 없다 = 다른 0012다)
```
이 세 줄이 "DB가 코드와 맞는가"의 손 검사다(저장소에 이것을 대신하는 스크립트는 없다). 0단계의 같은 조회와 짝이다.

**7단계 — 관리자 계정** — 3장 7단계와 같은 명령이다. `ADMIN_USERNAMES`에 적은 그 아이디로 만든다. 비밀번호는 물어본다
(8자 이상). 서버에서 만든 계정은 승인제와 상관없이 바로 `active`다(`auth_service.create_account`).
```bash
scripts/ops/run.sh scripts/reset_password.py --username <관리자 아이디> --create
scripts/ops/compose.sh logs --since 10m backend | grep "관리자 계정"      # 다음 재시작부터 이 경고가 사라진다
```
- 이 계정은 `ADMIN_USERNAMES`에 이름이 있어서 관리자다(`users.role`은 `user`로 남는다 — 그래도 `is_admin`은 true다).
  `python -m app.tools.create_user <아이디> --admin`은 **권한**(role=admin)으로 관리자를 만드는 다른 길이다(3장 7단계).
  이 서버는 이름 쪽 하나만 쓴다 — 시작 로그의 "관리자 계정이 아직 없습니다" 경고가 안내하는 명령이 이것이다.
  관리자를 더 둘 때는 `/admin`의 회원 관리에서 권한을 올린다.
- 둘 다 스택이 떠 있어야 한다(PostgreSQL에 붙는다). `run.sh`가 운영 컨테이너에서 이 스크립트를 돌리는 것은 11-11에 로컬
  운영 구성에서 확인했다(15장). 이 서버에서는 아직이다.

브라우저로 `https://43-203-31-7.sslip.io`에 로그인해 `/admin`이 열리고 "회원 관리" 구획이 보이는지 본다.

**8단계 — 목표 설정으로 바꾼다** (`.env.production`에서 두 줄)
```
REGISTER_INVITE_CODE=
AUTH_SIGNUP_REQUIRES_APPROVAL=true
```
```bash
scripts/ops/compose.sh up -d --no-build --wait        # 값이 바뀐 backend만 다시 만든다(10~20초 끊김)
scripts/ops/compose.sh exec -T backend python - --base-url https://43-203-31-7.sslip.io --connect-to caddy:443 --health-only < scripts/smoke_test.py
curl -s https://43-203-31-7.sslip.io/v1/auth/providers
#   → {"signup_open":true,"requires_approval":true,"invite_required":false,"require_consent":false,"google":false,"kakao":false}
#   (승인제 여부는 /v1/auth/providers 에만 있다. /v1/auth/config 에는 requires_approval 이 없다)
```

**승인 흐름 확인** (가입 → 202 `pending` → 승인 전 로그인 403 → 관리자 승인 → 로그인 200). 승인제에서는 가입이 세션을
주지 않는다 — "가입한 세션으로 API를 불러 본다"는 확인은 성립하지 않고, **로그인이 되는가**로 본다.

화면으로 하는 것이 기본이다: 다른 브라우저(또는 시크릿 창)에서 시험 계정으로 가입 → "승인 대기" 안내가 나오고 로그인되지
않는다 → 관리자로 `/admin` → 회원 관리 → 그 계정 "승인" → 시험 계정으로 로그인된다 → 회원 관리에서 시험 계정 삭제.
(회원 관리 화면은 PR #8에 들어 있다. 이 서버에서 눌러 본 것은 아니다.)

같은 것을 API로 볼 때(어디서 돌려도 된다). 요청·응답 모양은 `backend/app/api/routes/auth.py`·`routes/admin.py`·
`docs/api.md`를 읽고 적었고 **실제로 돌려 보지 못했다.**
```bash
BASE=https://43-203-31-7.sslip.io
# ① 시험 계정으로 가입 — 앱과 똑같이 초대 코드·동의 없이. 202 와 {"status":"pending","username":"approval-check","email":null}
curl -s -w '\n%{http_code}\n' -X POST "$BASE/v1/auth/register" -H 'Content-Type: application/json' \
  -d '{"username":"approval-check","password":"<시험용 8자 이상>"}'
# ② 승인 전 로그인은 403, 본문의 code 가 account_pending (비밀번호가 틀리면 401 이다 — 같은 비밀번호로)
curl -s -w '\n%{http_code}\n' -X POST "$BASE/v1/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"approval-check","password":"<시험용 8자 이상>"}'
# ③ 관리자 토큰 (비밀번호에 " 나 \ 가 있으면 이 줄이 깨진다 — 그때는 위의 화면으로 한다)
read -rsp '관리자 비밀번호: ' PW; echo
TOKEN=$(curl -s -X POST "$BASE/v1/auth/token" -H 'Content-Type: application/json' \
  -d "{\"username\":\"<관리자 아이디>\",\"password\":\"$PW\",\"client\":\"api\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'); unset PW
# ④ 대기 목록(items[].user_id)에서 user_id 를 찾아 승인한다. 응답의 "status" 가 "active"
curl -s "$BASE/v1/admin/members?status=pending" -H "Authorization: Bearer $TOKEN"
curl -s -X PATCH "$BASE/v1/admin/members/<user_id>" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"status":"active","expected_status":"pending"}'
# ⑤ 승인 뒤에는 ②와 같은 로그인이 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/v1/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"approval-check","password":"<시험용 8자 이상>"}'
# ⑥ 정리: 시험 계정 삭제(세션도 함께 지워진다), 관리자 토큰 폐기(204)
curl -s -X DELETE "$BASE/v1/admin/members/<user_id>" -H "Authorization: Bearer $TOKEN"
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/v1/auth/logout" -H "Authorization: Bearer $TOKEN"; unset TOKEN
```
- Bearer 토큰으로 부르는 쓰기에는 `X-CSRF-Token`이 필요 없다(쿠키 인증일 때만 검사한다).
- ④의 PATCH 본문에 모르는 필드가 있거나 `status`가 `pending`이면 422다. `expected_status`가 지금 상태와 다르면 409 `member_changed`.
- 로그인 시도는 제한이 있다(`RATE_LIMIT_AUTH_PER_MINUTE`, 같은 아이디를 거듭 틀리면 429) — 위 순서대로 한 번씩이면 걸리지 않는다.

**아이폰 앱** — 코드로 읽은 것이다(`apps/ios/App/API.swift`의 `SignInOutcome`·`AccountState`, `App/PersonaApp.swift`의
`LoginView.sign`). PR #12(2026-11-17)부터 앱은 승인제를 안다:
- 가입 응답이 202면 세션을 열지 않고 **"가입됐어요. 관리자가 승인하면 로그인할 수 있어요."**를 보이며, 그 아이디에
  대해서는 가입 버튼을 감춘다(앱을 다시 켜면 버튼은 다시 보인다 — 그때 같은 아이디로 가입하면 409다).
- 승인 전에 로그인하면(403 `account_pending`) "아직 승인 전이에요. 관리자가 승인하면 로그인할 수 있어요.", 정지된 계정은
  (403 `account_suspended`) "정지된 계정이에요. 관리자에게 문의해 주세요."를 보인다.
- 서버 주소는 앱에 기본값으로 들어 있다(`apps/ios/project.yml`의 `PERSONA_DEFAULT_SERVER_HOST` — 이 서버의 주소). 주소가
  바뀌면 그 값을 고쳐 다시 빌드하거나, 로그인 화면의 '서버 설정'에 직접 적는다.

**PR #12 이전에 설치한 앱은 그대로다** — 202 본문에서 `csrf_token`을 찾다가 가입 화면이 오류로 끝난다(계정은 승인 대기로
만들어진다). 승인제를 켜기 전에 새 빌드를 기기에 올린다. 앱에서 한 번 해 본다: 새 아이디로 가입(안내 문구) → `/admin`에서
승인 → 앱에서 로그인 → 첨삭이 되는지. **이 흐름을 실제 서버와 기기로 해 본 것은 아니다**(단위 테스트와 코드로만 확인했다).
앱은 여전히 가입 때 초대 코드와 `consent`를 보내지 않는다(16.7의 `REGISTER_INVITE_CODE`·`REQUIRE_CONSENT` 줄).

### 16.9 올린 뒤 확인표
`BASE=https://43-203-31-7.sslip.io`. 서버 밖(내 PC)에서 본다 — 닫힌 포트는 밖에서 봐야 뜻이 있다.

| 확인 | 명령 | 기대 |
|---|---|---|
| 서비스 상태 | (서버) `scripts/ops/compose.sh ps` | 다섯 서비스 모두 `(healthy)`, 포트가 보이는 것은 caddy뿐 |
| 깊은 헬스 | `curl -s "$BASE/healthz?deep=true"` | `"status":"ok"`, postgres·neo4j ok (503 `degraded`면 실패) |
| 가입 설정 | `curl -s "$BASE/v1/auth/providers"` | `signup_open: true`, `requires_approval: true`, `invite_required: false`, `require_consent: false`, `google`·`kakao: false` |
| http → https | `curl -sI http://43-203-31-7.sslip.io/ \| head -3` | 308, `Location: https://…` |
| 인증서 | `curl -sI "$BASE/" \| grep -i strict-transport` + 브라우저 자물쇠 | HSTS 있음, 인증서 경고 없음 |
| 닫힌 포트 | `for p in 5432 7474 7687 8000; do nc -z -w 3 43.203.31.7 $p && echo "$p 열림(문제)" \|\| echo "$p 닫힘"; done` | 넷 다 닫힘 |
| 문서 화면 꺼짐 | `curl -s -o /dev/null -w '%{http_code}\n' "$BASE/docs"` (`/redoc`, `/openapi.json`도) | 404 |
| 헤더 인증 거부 | `curl -s -o /dev/null -w '%{http_code}\n' -H 'X-User-Id: demo' "$BASE/v1/contacts"` | 401 |
| 마이그레이션 | 16.8의 세 줄 | `0012`, 6, 두 표 있음 |
| 스모크(전체 흐름) | 6단계의 `deploy.sh` 출력 | "스모크 통과 — …" — 승인제를 켜기 **전**에 통과한 것 |
| 스모크(지금 상태) | `python3 scripts/smoke_test.py --base-url "$BASE" --health-only` | 통과. 승인제가 켜진 뒤에는 `--health-only`만 통과한다(전체를 돌리면 "가입"에서 실패하고 `smoke-…` 계정이 승인 대기로 남는다) |
| 승인 흐름 | 16.8의 화면 또는 ①~⑥ | 가입 202 `pending` → 로그인 403 `account_pending` → 승인 → 로그인 200 |
| 관리자 | 브라우저 `$BASE/admin` → 회원 관리 | 관리자 한 명(`ADMIN_USERNAMES`의 그 아이디), 승인 대기 0, 시험 계정(`approval-check`·`smoke-…`) 없음 |
| 백업 | (서버) `scripts/ops/backup.sh && scripts/ops/backup.sh --list` | 새 덤프 하나 |
| 배포 기록 | (서버) `tail -2 .deploy/history.log` | 올린 커밋, "성공" |

전부 맞으면 10장의 점검표를 본다. 이 서버에서 **일부러 어긋나게 둔 두 줄**: "`REGISTER_INVITE_CODE`가 있거나 가입이 닫혀
있다"(→ 승인제로 대신한다), "동의를 받는다(`REQUIRE_CONSENT=true`)"(→ 앱이 동의 값을 보내게 고친 뒤 켠다).

**정리 — 확인이 끝난 뒤, 며칠 써 보고**
```bash
# ⚠ 되돌릴 수 없다. 이 줄 뒤에는 옛 서버로 돌아갈 방법이 1단계의 덤프뿐이고, 옛 그래프는 영영 없다.
docker volume rm <옛 프로젝트>_postgres-data <옛 프로젝트>_neo4j-data <옛 프로젝트>_caddy-data
docker image ls | grep <옛 프로젝트>      # 옛 backend 이미지 이름을 보고 → docker image rm <이름>
```
`~/pz-old`(덤프·옛 env·옛 코드)는 서버 밖으로 암호화해 옮긴 뒤 지운다. 옛 env의 API 키를 새로 발급한 키로 바꿨다면 옛 키는 Console에서 폐기한다.

### 16.10 되돌리기
새 스택이 뜨지 않거나 확인표에서 막혔을 때, **돌아갈 수 있는 곳은 옛 브랜치 빌드뿐이다**(옛 DB는 옛 코드로만 읽힌다).
`scripts/ops/rollback.sh`는 여기서 쓸 수 없다 — 직전 이미지(`:previous`)가 없는 첫 배포다.

**옛 볼륨을 아직 지우지 않았을 때** (16.5에서 남겨 둔 경우 — 데이터가 내린 순간 그대로 돌아온다):
```bash
cd ~/persona-ize && scripts/ops/compose.sh down            # 새 스택만 내린다. -v 를 붙이지 않는다
cd ~/pz-old/code                                           # 옛 폴더 (옮기지 않았으면 <옛 폴더>)
docker compose -p <옛 프로젝트> --env-file .env.prod -f docker-compose.prod.yml up -d
curl -s "https://43-203-31-7.sslip.io/healthz?deep=true"
```
`-p <옛 프로젝트>`를 빼면 폴더 이름(`code`)이 프로젝트 이름이 되어 **빈 볼륨이 새로 생긴다.** `hf-cache`를 지웠으면 옛
backend가 e5 모델을 다시 받느라 첫 요청이 늦다. 그 사이 새 스택에 쌓인 가입·데이터는 옛 서버에 없다.
(이 되돌리기는 해 보지 못했다.)

**옛 볼륨을 지웠을 때** (이름이 겹쳤거나 정리 뒤): 1단계의 덤프를 옛 스택에 넣는다. 그래프는 돌아오지 않는다.
```bash
cd ~/persona-ize && scripts/ops/compose.sh down
cd ~/pz-old/code && P="docker compose -p <옛 프로젝트> --env-file .env.prod -f docker-compose.prod.yml"
$P up -d postgres            # 빈 DB가 옛 .env.prod 의 비밀번호로 만들어진다
$P exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' < ~/pz-old/pz-old-0008-<시각>.dump
$P up -d                     # 옛 backend 가 뜨면서 alembic 이 이미 0008 임을 보고 넘어간다
```
이름이 겹친 경우에는 그 전에 새 스택의 볼륨을 비워야 옛 비밀번호로 초기화된다(`docker volume rm persona-ize-prod_postgres-data
persona-ize-prod_neo4j-data` — ⚠ 새 스택의 데이터가 사라진다). 이 복원도 해 보지 못했다.

**옛 덤프의 내용을 main으로 옮기는 길은 없다.** 필요하면 옛 스택을 잠깐 띄워 사용자가 화면에서 내보내고, 새 서버에서 다시 가져온다.

### 16.11 확인한 것과 못 한 것 (2026-11-17)
- **파일을 읽어 확인한 것** — PR #8(`port/members-mobile`, `39eda7b`)의 코드:
  - `docker-compose.prod.yml`·`docker/caddy/Caddyfile`·`.env.production.example`·`scripts/ops/*.sh`·`scripts/reset_password.py`.
  - 승인제: 가입은 202 `{"status":"pending",…}`에 세션·쿠키가 없고, 승인 전 로그인·토큰 발급은 비밀번호가 맞은 뒤 403
    `account_pending`이다(`backend/app/api/routes/auth.py`·`backend/app/services/auth_service.py`). 서버에서 만든 계정
    (`reset_password.py --create`·`create_user`)은 승인제여도 `active`다.
  - 관리자: `backend/app/tools/create_user.py`의 인자는 `<아이디>`·`--admin`·`--reset-password`뿐이고 비밀번호는 물어보거나
    표준입력으로 받는다. 승인은 `PATCH /v1/admin/members/{user_id}` `{"status":"active"}`(`routes/admin.py`).
  - 운영 검사(`backend/app/core/guard.py`)가 거부하는 조합은 16.7에 적은 그대로이고, 승인제·`REQUIRE_CONSENT`는 거부 조건에
    들어 있지 않다. 소셜 로그인을 켜면 `PUBLIC_BASE_URL`이 경로 없는 `https://도메인`이어야 한다.
  - 스모크: `scripts/smoke_test.py`의 가입 단계는 201만 받는다. `scripts/ops/deploy.sh`는 승인제를 보지 않는다. → 승인제가
    켜진 서버에서 전체 스모크는 실패한다(PR #8이 고치지 않았다).
  - 0012(`backend/migrations/versions/0012_members_social_login.py`): `users.email`·`email_verified`·`display_name`·`role`·
    `status`·`last_login_at`, `user_identities`. `/v1/auth/providers`의 응답 필드.
  - 아이폰 앱이 가입에 `username`·`password`만 보내고(초대 코드·`consent` 없음), 202는 승인 대기로, 201은 세션으로 읽는다는 것
    (`apps/ios/App/API.swift` — PR #12. 단위 테스트 `apps/ios/Tests/LoginLogicTests.swift`가 이 판정을 고정한다).
  - 옛 브랜치(`design/iphone-mobile`, `c001cbb`)의 compose·Caddyfile·`.env.prod.example`·`0008`·배포 문서는 이 절을 처음 쓸 때
    읽은 것이다(이번에 다시 읽지 않았다).
- **못 한 것 — 서버에서 해 봐야 아는 것**: 서버 접속(옛 스택의 실제 폴더·프로젝트·볼륨 이름, Docker 버전, 디스크, 데이터 양을
  모른다). 이 절의 명령을 어디서도 실행하지 않았다 — 인증서 볼륨 복사, 프로젝트 이름으로 내리기, 승인 흐름의 화면과 curl,
  두 가지 되돌리기, 4GB 서버에서의 빌드, GHCR 이미지로 띄우기. 옛 DB에 main을 올렸을 때의 실제 실패 모습(16.1).
  `sslip.io`의 Let's Encrypt 한도 적용 방식. 승인제 서버에서 아이폰 앱의 가입·로그인 화면이 실제로 보여 주는 것.
  PR #8의 pytest·CI 결과는 여기서 다시 돌리지 않았다.
- **이 절이 바뀌어야 하는 때**: 마이그레이션 번호·환경변수 이름이 바뀌면 16.6의 확인 줄과 16.7의 표를 고친다(PR #8은
  2026-11-17에 그대로 병합됐다). 스모크 테스트가 승인제를 알게 되면 16.7의 "두 번에 나눠 올리기"와 16.12의 마지막 줄을
  지운다. 앱이 가입 때 `consent`를 보내게 되면 16.7의 `REQUIRE_CONSENT` 줄과 16.8의 아이폰 앱 문단, 16.9 아래의 "일부러
  어긋나게 둔 두 줄"을 고친다.

### 16.12 앞으로
- **이미 어딘가에 적용된 마이그레이션은 고치지 않는다.** 바꿀 것이 있으면 새 번호로 더한다.
- **같은 번호(revision id)를 두 브랜치에서 쓰지 않는다.** 브랜치에서 마이그레이션을 만들었으면 병합 직전에 main의 마지막
  번호 다음으로 다시 매긴다(`down_revision`도). 번호가 겹친 브랜치는 운영 DB에 닿기 전에 고친다.
- **배포는 main(태그)에서만 한다.** 브랜치 빌드를 운영 서버에 올리지 않는다 — 시험은 14장의 로컬 구성이나 따로 띄운 서버에서.
- **코드는 git으로만 올린다**(rsync 금지). 서버에서 `git status`가 깨끗하고 `.deploy/history.log`에 커밋이 남아야 한다.
- 이 서버의 다음 업데이트: 승인제가 켜져 있는 동안 `scripts/ops/deploy.sh`의 스모크는 실패로 끝나고, 돌릴 때마다
  `smoke-…` 계정이 승인 대기로 하나씩 남는다(16.7). 스모크 테스트가 승인제를 알기 전까지는 `scripts/ops/deploy.sh --no-smoke`
  (태그 배포면 `git fetch --tags && git checkout <태그>` 뒤 `--no-pull --no-smoke`) 다음에 16.8의 `--health-only` 스모크와
  16.9의 표를 본다. 실수로 돌렸으면 `/admin`의 회원 관리에서 `smoke-…` 계정을 지운다.
- 남은 일(이 절의 범위 밖): 스모크 테스트가 승인제를 알게 하기, 아이폰 앱이 가입 때 `consent`를 보내게 하기(그 뒤
  `REQUIRE_CONSENT=true`). 앱이 가입 응답 202(승인 대기)를 안내로 보여 주는 것은 PR #12에서 끝났다.
