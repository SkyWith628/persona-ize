# 배포 (AWS EC2 한 대)

서버 한 대에 PostgreSQL · Neo4j · 백엔드 · HTTPS 프록시(Caddy)를 `docker-compose.prod.yml`로 함께 올린다.
웹 화면은 백엔드가 같이 서빙한다: `/` 모바일 웹, `/manage` 실험용 관리 화면, `/admin` 운영 대시보드.
결정 배경은 `docs/decisions.md` 2026-11-16 "웹 배포" 항목.

## 준비물

| 항목 | 값 |
|---|---|
| 인스턴스 | 메모리 4GB 이상 (예: t3.medium), 디스크 30GB, Ubuntu 24.04. 요금은 AWS 요금표에서 리전별로 확인한다 |
| 보안 그룹 | 인바운드 80·443은 전체, 22는 **내 IP만**. 5432·7474·7687·8000은 열지 않는다 |
| 고정 IP | Elastic IP를 붙인다 (재시작 때 IP가 바뀌면 인증서 주소가 달라진다) |
| 주소 | 도메인이 있으면 A 레코드를 고정 IP로. 없으면 `<IP의 점을 -로>.sslip.io` (예: `3-35-1-2.sslip.io`) |
| 키 | Anthropic API 키. Console에서 **월 사용량 한도를 먼저** 건다 |

## 처음 올리기

```bash
# 서버에서
git clone <저장소 주소> persona-ize && cd persona-ize
sudo bash scripts/deploy/ec2_bootstrap.sh      # Docker·스왑. 끝나면 다시 로그인
cp .env.prod.example .env.prod && chmod 600 .env.prod
# .env.prod를 채운다: SITE_ADDRESS, ANTHROPIC_API_KEY, ADMIN_USERNAMES,
# POSTGRES_PASSWORD·NEO4J_PASSWORD는 `openssl rand -hex 24`로 각각 새로 만든다
make prod-up                                    # 설정 점검 → 빌드 → 기동 (첫 빌드 5~10분)
```

`make prod-up`은 먼저 `scripts/check_prod_env.py`를 돌린다. 헤더 인증이 켜져 있거나 개발용 비밀번호가 남아 있으면
여기서 멈춘다. 비공개 저장소는 서버에 배포 키(읽기 전용)를 등록해 받는다.

### 계정 만들기

가입은 꺼 두었으므로 계정은 서버에서 만든다. 비밀번호는 물어볼 때 입력한다(명령줄에 쓰지 않는다).

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml exec backend python -m app.tools.create_user <아이디>
```

운영 대시보드는 `.env.prod`의 `ADMIN_USERNAMES`에 적은 아이디로 로그인한 뒤 `/admin`에서 본다.
시연용 합성 대화는 웹의 상대 → 대화 가져오기에서 붙여 넣는다. `scripts/prepare_demo.py`는 저장소의
`data/`·`evals/`를 읽는데 배포 이미지는 `backend/`만 담으므로 컨테이너에서는 돌지 않는다.

## 올린 뒤 확인

```bash
curl -s https://$SITE_ADDRESS/healthz?deep=true      # status ok, checks.postgres·neo4j ok
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-User-Id: demo' https://$SITE_ADDRESS/v1/contacts   # 401이어야 한다
curl -s -o /dev/null -w '%{http_code}\n' http://$SITE_ADDRESS/                                    # 308 (HTTPS로 넘김)
make prod-logs                                        # `embedder warmed up` 뒤에 첫 첨삭
```

브라우저에서 `https://<주소>/`에 로그인해 첨삭 한 건, `/admin`에서 수치가 오르는지 본다.
아이폰 앱은 로그인 화면의 서버 주소에 `https://<주소>`를 넣는다.

## 다시 올리기 · 되돌리기

```bash
git pull && make prod-up            # 새 코드. 마이그레이션은 백엔드가 시작할 때 올린다
git checkout <이전 커밋> && make prod-up    # 되돌리기
```

마이그레이션이 있던 배포를 되돌릴 때는 코드만 내리면 스키마가 앞서 있다. 그 릴리스의 마이그레이션이
`downgrade`를 지원하는지 확인하고 `alembic downgrade <리비전>`을 먼저 한다. 확신이 없으면 되돌리지 말고 고쳐서 다시 올린다.
배포 전에 아래 백업을 받아 둔다.

## 백업

```bash
P="docker compose --env-file .env.prod -f docker-compose.prod.yml"
$P exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > backup-$(date +%F).sql.gz
```

그래프(Neo4j)는 PostgreSQL의 메시지에서 다시 만들 수 있다(`scripts/graph_reset.py`, Haiku 비용 발생).
통째로 지키려면 EBS 스냅샷을 쓴다. `caddy-data` 볼륨(인증서)은 지우지 않는다.

## 비용과 한도

- 서버 요금과 별개로 Anthropic API 요금이 나간다. 첨삭은 Opus, 그래프 수집은 Haiku.
- 사용자당 하루 한도는 `.env.prod`의 `USAGE_DAILY_*`. 예시는 첨삭 50회·15만 토큰.
- 쓰지 않을 때는 인스턴스를 중지한다(디스크와 고정 IP 요금은 남는다).

## 알려진 한계

- 서버 한 대라 재시작·배포 동안 잠깐 끊긴다. 그래프 수집 잠금은 프로세스 안에서만 유효해 백엔드를 여러 개로 늘릴 수 없다.
- 로그인 시도 제한은 프로세스 메모리에 있어 재시작하면 초기화된다. 요청 전체에 대한 속도 제한은 없다(사용자별 일 한도만).
- `/healthz`는 로그인 없이 모델 이름과 프롬프트 버전을 보여 준다.
- 로그는 `docker compose logs`에만 남는다. 장기 보관·알림은 없다.

## 남은 일

- 시연 데이터(`prepare_demo.py`)를 배포 서버에 한 번에 넣는 방법.
- GitHub Actions로 이미지 빌드·배포 자동화, 실패 시 자동 되돌리기.
- `housekeeping.py` 정기 실행(만료 세션·오래된 작업 정리).
