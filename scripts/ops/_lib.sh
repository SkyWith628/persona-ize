# scripts/ops 공통 함수 — 다른 스크립트가 `. "$(dirname "$0")/_lib.sh"`로 불러 쓴다 (직접 실행하지 않는다).
#
# 환경변수로 바꿀 수 있는 것
#   PZ_ENV_FILE   운영 env 파일 (기본: 저장소 루트의 .env.production)
#   PZ_PROJECT    compose 프로젝트 이름 (기본: docker-compose.prod.yml의 name = persona-ize-prod)
# shellcheck shell=bash

set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$OPS_DIR/../.." && pwd)"
ENV_FILE="${PZ_ENV_FILE:-$REPO_ROOT/.env.production}"
case "$ENV_FILE" in
/* | [A-Za-z]:*) ;;
*) ENV_FILE="$PWD/$ENV_FILE" ;; # 상대 경로는 지금 폴더 기준
esac

# Windows Git Bash에서 로컬 검증할 때: docker.exe가 알아듣는 경로(C:/...)로 바꾸고, 컨테이너 안 경로
# (/opt/backup/...)가 Windows 경로로 바뀌지 않게 MSYS 경로 변환을 끈다. 리눅스에서는 아무 영향 없다.
if command -v cygpath >/dev/null 2>&1; then
	REPO_ROOT="$(cygpath -m "$REPO_ROOT")"
	ENV_FILE="$(cygpath -m "$ENV_FILE")"
fi
export MSYS_NO_PATHCONV=1

COMPOSE_PROD="$REPO_ROOT/docker-compose.prod.yml"

info() { echo "[$(date +%H:%M:%S)] $*"; }
warn() { echo "[$(date +%H:%M:%S)] 주의: $*" >&2; }
die() {
	echo "오류: $*" >&2
	exit 1
}

require_env_file() {
	[ -f "$ENV_FILE" ] || die "env 파일이 없습니다: $ENV_FILE
  cp .env.production.example .env.production 뒤 값을 채우세요 (docs/deploy.md 3단계)."
}

# env_get KEY [기본값] — env 파일에서 값 하나를 읽는다 (마지막 정의, 양끝 따옴표·CR 제거).
env_get() {
	local key="$1" default="${2:-}" line value
	line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
	if [ -z "$line" ]; then
		printf '%s' "$default"
		return 0
	fi
	value="${line#*=}"
	value="${value%$'\r'}"
	case "$value" in
	\"*\") value="${value#\"}" value="${value%\"}" ;;
	\'*\') value="${value#\'}" value="${value%\'}" ;;
	esac
	if [ -z "$value" ]; then value="$default"; fi
	printf '%s' "$value"
}

# 운영 compose 명령. 예) compose ps / compose logs -f backend
compose() {
	PZ_ENV_FILE="$ENV_FILE" docker compose ${PZ_PROJECT:+-p "$PZ_PROJECT"} \
		--env-file "$ENV_FILE" -f "$COMPOSE_PROD" "$@"
}

# 백업 폴더의 절대 경로 (env의 BACKUP_DIR, 기본 ./backups — compose 파일 기준 상대 경로)
backup_dir() {
	local dir
	dir="$(env_get BACKUP_DIR ./backups)"
	case "$dir" in
	/* | [A-Za-z]:*) printf '%s' "$dir" ;;
	*) printf '%s' "$REPO_ROOT/${dir#./}" ;;
	esac
}

# image_content <이미지> — 이미지 "내용"의 지문(레이어 + 실행 설정). 이미지가 없으면 빈 문자열.
# 같은 소스로 다시 빌드하면 이미지 ID는 달라져도(빌드 기록이 붙는다) 이 값은 같다 → 실제로 바뀌었는지 볼 때 쓴다.
image_content() {
	local desc
	desc="$(docker image inspect -f '{{json .RootFS.Layers}} {{json .Config}}' "$1" 2>/dev/null)" || return 0
	printf '%s' "$desc" | sha256sum | cut -c1-16
}

# confirm "<질문>" "<입력해야 하는 글자>" — 그 글자를 그대로 쳐야 통과한다.
confirm() {
	local question="$1" expected="$2" answer
	printf '%s\n계속하려면 %s 를 입력하세요: ' "$question" "$expected"
	read -r answer || answer=""
	[ "$answer" = "$expected" ] || die "취소했습니다."
}
