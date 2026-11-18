#!/usr/bin/env bash
# 배포 한 번: 코드 받기 → 설정 점검 → 배포 전 백업 → 이미지 빌드 → 기동 → 헬스 대기 → 스모크 테스트
#
#   scripts/ops/deploy.sh             # 전부
#   옵션
#     --no-pull      git pull을 건너뛴다 (이미 원하는 커밋에 있을 때)
#     --no-backup    배포 전 PostgreSQL 백업을 건너뛴다
#     --no-build     이미지를 다시 만들지 않는다 (BACKEND_IMAGE로 받은 이미지를 쓸 때)
#     --pull-base    빌드할 때 기반 이미지(python:3.12-slim)를 새로 받는다 (보안 패치 반영, 전체 재빌드라 느리다)
#     --no-smoke     스모크 테스트를 건너뛴다
#     --smoke-full   실제 모델(LLM_PROVIDER=anthropic)에서도 가져오기·C 첨삭까지 돈다 (그래프 수집 3건 비용 발생)
#     --prune        끝난 뒤 태그 없는 옛 이미지를 지운다 (`docker image prune -f` — 이 서버의 모든 프로젝트에 적용)
#
# 실패하면 0이 아닌 코드로 끝나고, 되돌리는 방법을 알려 준다 (scripts/ops/rollback.sh).
# 직전 이미지는 <이미지 저장소>:previous 태그로 남겨 둔다. 배포 기록은 .deploy/history.log.
. "$(dirname "$0")/_lib.sh"

main() {
	local do_pull=yes do_backup=yes do_build=yes do_smoke=yes smoke_full=no pull_base=no prune=no
	local -a original_args=("$@")
	while [ $# -gt 0 ]; do
		case "$1" in
		--no-pull) do_pull=no ;;
		--no-backup) do_backup=no ;;
		--no-build) do_build=no ;;
		--pull-base) pull_base=yes ;;
		--no-smoke) do_smoke=no ;;
		--smoke-full) smoke_full=yes ;;
		--prune) prune=yes ;;
		*) die "모르는 옵션: $1 (사용법은 파일 머리말)" ;;
		esac
		shift
	done

	command -v docker >/dev/null 2>&1 || die "docker가 없습니다 (docs/deploy.md 1단계)."
	docker compose version >/dev/null 2>&1 || die "docker compose 플러그인이 없습니다."
	require_env_file

	# ---- 1. 코드 받기 ----
	local before after
	# PZ_DEPLOY_FROM: 아래에서 자신을 다시 실행할 때 pull 이전 커밋을 넘겨받는다(배포 기록용).
	before="${PZ_DEPLOY_FROM:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "-")}"
	if [ "$do_pull" = yes ]; then
		info "코드를 받습니다 (git pull --ff-only)"
		git -C "$REPO_ROOT" pull --ff-only
		after="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
		if [ "$after" != "$before" ]; then
			# 이 스크립트 자신도 바뀌었을 수 있다 → 새 버전으로 다시 시작한다.
			info "새 커밋 $before → $after. 새 스크립트로 이어갑니다."
			PZ_DEPLOY_FROM="$before" exec bash "$0" --no-pull "${original_args[@]}"
		fi
	fi
	after="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "-")"

	# ---- 2. 설정 점검 ----
	preflight
	compose config --quiet || die "docker-compose.prod.yml 설정이 올바르지 않습니다 (위 메시지 참고)."
	# 백업 폴더는 미리 만들어 둔다 — 없으면 Docker가 root 소유로 만들어 나중에 덤프를 다루기 불편하다.
	mkdir -p "$(backup_dir)"

	# ---- 3. 배포 전 백업 (PostgreSQL이 이미 떠 있을 때만) ----
	if [ "$do_backup" = yes ]; then
		if [ -n "$(compose ps -q --status running postgres 2>/dev/null)" ]; then
			bash "$OPS_DIR/backup.sh"
		else
			info "PostgreSQL이 아직 없어 배포 전 백업은 건너뜁니다 (첫 배포)."
		fi
	fi

	# ---- 4. 이미지 ----
	local image repo last prev_id="" new_id
	image="$(env_get BACKEND_IMAGE persona-ize/backend:prod)"
	last="${image##*/}"
	if [[ "$last" == *:* ]]; then repo="${image%:*}"; else repo="$image"; fi
	prev_id="$(image_content "$image")"
	if [ "$do_build" = yes ]; then
		# 빌드가 태그를 새 이미지로 옮기면 옛 이미지는 이름이 없어져 다시 가리킬 수 없다(containerd 이미지 저장소).
		# 그래서 빌드 전에 임시 태그를 붙여 두고, 이미지 내용이 실제로 바뀌었을 때만 :previous 로 옮긴다
		# (코드가 그대로인 재배포가 되돌리기용 이미지를 덮어쓰지 않게).
		[ -z "$prev_id" ] || docker tag "$image" "$repo:deploy-candidate"
		info "백엔드 이미지를 만듭니다 ($image)"
		if [ "$pull_base" = yes ]; then compose build --pull backend; else compose build backend; fi
	fi
	new_id="$(image_content "$image")"
	if [ "$do_build" = yes ] && [ -n "$prev_id" ]; then
		if [ "$prev_id" != "$new_id" ]; then
			docker tag "$repo:deploy-candidate" "$repo:previous"
			info "직전 이미지를 $repo:previous 로 남겼습니다 (되돌리기용)."
		else
			info "이미지 내용은 그대로입니다."
		fi
		docker rmi "$repo:deploy-candidate" >/dev/null 2>&1 || true # 임시 태그만 지운다
	fi

	# ---- 5. 기동 + 헬스 대기 ----
	info "서비스를 올리고 전부 healthy가 될 때까지 기다립니다 (최대 ${PZ_WAIT_SECONDS:-300}초)"
	if ! compose up -d --no-build --remove-orphans --wait --wait-timeout "${PZ_WAIT_SECONDS:-300}"; then
		compose ps || true
		compose logs --tail 40 backend || true
		record "$before" "$after" "$prev_id" "$new_id" "실패: 기동"
		die "서비스가 정상 상태가 되지 못했습니다. 위 로그를 확인하세요. 되돌리기: scripts/ops/rollback.sh"
	fi
	compose ps

	# ---- 6. 스모크 테스트 ----
	if [ "$do_smoke" = yes ]; then
		if ! smoke "$smoke_full"; then
			record "$before" "$after" "$prev_id" "$new_id" "실패: 스모크"
			die "스모크 테스트가 실패했습니다. 서비스는 떠 있습니다 — 로그를 보고 고치거나 되돌리세요: scripts/ops/rollback.sh"
		fi
	fi

	record "$before" "$after" "$prev_id" "$new_id" "성공"
	if [ "$prune" = yes ]; then docker image prune -f >/dev/null && info "태그 없는 옛 이미지를 지웠습니다."; fi
	info "배포 끝: 커밋 $after, 주소 https://$(site_host)"
}

# 서비스 주소의 첫 이름 (SITE_ADDRESS가 "a.com, b.com"이면 a.com)
site_host() {
	local site
	site="$(env_get SITE_ADDRESS)"
	site="${site%%,*}"
	site="${site#https://}"
	printf '%s' "${site// /}"
}

preflight() {
	local key value problems=0
	# 예시 파일의 자리표시자가 그대로 남아 있으면 멈춘다
	if grep -nE '^[A-Z0-9_]+=.*CHANGE_ME' "$ENV_FILE" >/dev/null; then
		echo "오류: 자리표시자(CHANGE_ME)가 남아 있습니다:" >&2
		grep -nE '^[A-Z0-9_]+=.*CHANGE_ME' "$ENV_FILE" | cut -d= -f1 >&2
		problems=1
	fi
	for key in SITE_ADDRESS POSTGRES_PASSWORD NEO4J_PASSWORD; do
		[ -n "$(env_get "$key")" ] || {
			echo "오류: $key 가 비어 있습니다." >&2
			problems=1
		}
	done
	[ "$(env_get POSTGRES_PASSWORD)" != "persona-dev-only" ] || {
		echo "오류: POSTGRES_PASSWORD가 개발용 기본값입니다." >&2
		problems=1
	}
	value="$(env_get NEO4J_PASSWORD)"
	if [ "$value" = "neo4j-dev-only" ] || [ "${#value}" -lt 8 ]; then
		echo "오류: NEO4J_PASSWORD는 개발용 기본값이 아닌 8자 이상이어야 합니다." >&2
		problems=1
	fi
	# 두 비밀번호는 compose가 접속 주소(postgresql+asyncpg://사용자:비밀번호@...)와 NEO4J_AUTH(neo4j/비밀번호)에
	# 그대로 끼워 넣는다. @ / # : ? % 같은 문자가 있으면 주소가 깨져 backend가 DB에 붙지 못한다.
	for key in POSTGRES_PASSWORD NEO4J_PASSWORD; do
		value="$(env_get "$key")"
		case "$value" in
		*[!A-Za-z0-9_-]*)
			echo "오류: $key 에는 영문·숫자·-·_ 만 쓰세요 (접속 주소에 그대로 들어갑니다. openssl rand -base64 32 | tr -d '/+=' 로 만드세요)." >&2
			problems=1
			;;
		esac
	done
	[ "$problems" -eq 0 ] || die "$ENV_FILE 을 고친 뒤 다시 실행하세요."

	# 여기부터는 경고만 — 의도한 설정일 수 있다
	case "$(env_get LLM_PROVIDER fake)" in
	anthropic) [ -n "$(env_get ANTHROPIC_API_KEY)" ] || warn "LLM_PROVIDER=anthropic인데 ANTHROPIC_API_KEY가 비어 있습니다 — 첨삭이 실패합니다." ;;
	*) warn "LLM_PROVIDER가 anthropic이 아닙니다 — 가짜 모델(원문을 그대로 돌려줌)로 뜹니다." ;;
	esac
	if [ "$(env_get AUTH_ALLOW_REGISTER true)" != "false" ] && [ -z "$(env_get REGISTER_INVITE_CODE)" ]; then
		warn "가입이 열려 있는데 REGISTER_INVITE_CODE가 비어 있습니다 — 주소를 아는 누구나 가입해 API 비용을 쓸 수 있습니다."
	fi
	[ -n "$(env_get ADMIN_USERNAMES)" ] || warn "ADMIN_USERNAMES가 비어 있습니다 — 권한(role)이 admin인 계정도 없으면 /admin 화면을 볼 수 있는 사람이 없습니다 (docs/deploy.md 7단계)."
	if [ "$(env_get USAGE_GLOBAL_DAILY_TOKEN_LIMIT 0)" = "0" ] && [ "$(env_get USAGE_GLOBAL_DAILY_REWRITE_LIMIT 0)" = "0" ]; then
		warn "전체 사용량 차단기(USAGE_GLOBAL_DAILY_*)가 꺼져 있습니다 — 하루 비용 상한이 사용자별 한도 × 사용자 수가 됩니다."
	fi
	if [ "$(env_get GRAPH_INGEST_USER_MESSAGE_CAP 0)" = "0" ] && [ "$(env_get GRAPH_INGEST_DAILY_GLOBAL_CAP 0)" = "0" ]; then
		warn "그래프 수집 상한(GRAPH_INGEST_*_CAP)이 둘 다 0(무제한)입니다 — 메시지 1건마다 모델 비용이 듭니다. API 키가 있으면 backend가 시작을 거부합니다(ALLOW_UNCAPPED_COSTS=true로만 통과)."
	fi
	if [ "$(uname -s)" = "Linux" ] && [ "$(stat -c %a "$ENV_FILE" 2>/dev/null || echo 600)" != "600" ]; then
		warn "$ENV_FILE 권한이 600이 아닙니다 (chmod 600 권장 — 비밀 값이 들어 있습니다)."
	fi
}

# 스모크 테스트를 backend 컨테이너 안에서 돌린다: 호스트에 파이썬이 없어도 되고, Caddy(https)를 거쳐 들어온다.
smoke() {
	local full="$1" host
	host="$(site_host)"
	local -a args=(--base-url "https://$host" --connect-to caddy:443)
	# localhost·IP 주소는 Caddy 내부 CA(자체 서명)라 인증서 검증을 끈다
	if [ "$host" = "localhost" ] || [[ "$host" =~ ^[0-9.]+$ ]] || [ "${SMOKE_INSECURE:-0}" = "1" ]; then
		args+=(--insecure)
	fi
	if [ "$(env_get AUTH_ALLOW_REGISTER true)" = "false" ]; then
		warn "가입이 닫혀 있어(AUTH_ALLOW_REGISTER=false) 스모크는 헬스·화면 확인만 합니다."
		args+=(--health-only)
	elif [ "$(env_get LLM_PROVIDER fake)" = "anthropic" ] && [ "$full" != "yes" ]; then
		# 실제 모델: B 첨삭 한 번으로 API 키·크레딧만 확인하고, 비용이 큰 그래프 수집(가져오기)은 건너뛴다.
		args+=(--no-import)
	fi
	info "스모크 테스트: ${args[*]}"
	compose exec -T backend python - "${args[@]}" <"$REPO_ROOT/scripts/smoke_test.py"
}

# 배포 기록 한 줄: 시각, 커밋 전→후, 이미지 내용 지문 전→후, 결과
record() {
	mkdir -p "$REPO_ROOT/.deploy"
	printf '%s commit=%s→%s image=%s→%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" \
		"${3:--}" "${4:--}" "$5" >>"$REPO_ROOT/.deploy/history.log"
}

main "$@"
