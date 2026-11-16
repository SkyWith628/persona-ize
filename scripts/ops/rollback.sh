#!/usr/bin/env bash
# 직전 배포의 백엔드 이미지로 되돌린다 (deploy.sh가 남긴 <이미지 저장소>:previous).
#
#   scripts/ops/rollback.sh          # 확인 질문 뒤 이미지 태그를 맞바꾸고 backend만 다시 띄운다
#   scripts/ops/rollback.sh --yes
#
# 되돌리는 것은 **백엔드 코드(이미지)뿐**이다.
# - DB 스키마는 되돌리지 않는다. 새 버전이 마이그레이션을 올렸다면 옛 코드가 새 스키마에서 돌게 된다. 열 추가
#   정도는 대개 괜찮지만, 문제가 되면 배포 전 백업으로 복원한다(scripts/ops/restore.sh, 그 사이 데이터는 잃는다).
# - Caddyfile·compose 파일도 그대로다. 그것까지 되돌리려면 `git checkout <이전 커밋>` 뒤 `deploy.sh --no-pull`.
#   이전 커밋은 .deploy/history.log 에 있다.
# 한 번 더 돌리면 다시 새 버전으로 돌아간다(태그를 맞바꾸기 때문).
. "$(dirname "$0")/_lib.sh"
require_env_file

yes="no"
[ "${1:-}" != "--yes" ] || yes="yes"

image="$(env_get BACKEND_IMAGE persona-ize/backend:prod)"
last="${image##*/}"
if [[ "$last" == *:* ]]; then repo="${image%:*}"; else repo="$image"; fi

current="$(image_content "$image")"
previous="$(image_content "$repo:previous")"
[ -n "$previous" ] || die "되돌릴 이미지($repo:previous)가 없습니다. deploy.sh로 코드가 바뀐 배포를 한 번 이상 한 뒤에만 생깁니다."
[ "$previous" != "$current" ] || die "현재 이미지와 직전 이미지의 내용이 같습니다."

if [ "$yes" != "yes" ]; then
	confirm "백엔드를 직전 이미지($repo:previous)로 되돌립니다. 현재 이미지는 :previous 로 남습니다." "rollback"
fi
# 태그를 맞바꾼다. 이름이 없어진 이미지는 다시 가리킬 수 없으므로(containerd 이미지 저장소) 임시 태그를 거친다.
if [ -n "$current" ]; then docker tag "$image" "$repo:rollback-swap"; fi
docker tag "$repo:previous" "$image"
if [ -n "$current" ]; then
	docker tag "$repo:rollback-swap" "$repo:previous"
	docker rmi "$repo:rollback-swap" >/dev/null
fi
info "backend를 직전 이미지로 다시 띄웁니다"
compose up -d --no-build --wait --wait-timeout "${PZ_WAIT_SECONDS:-300}" backend
compose ps
mkdir -p "$REPO_ROOT/.deploy"
printf '%s rollback image=%s→%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${current:--}" "$previous" \
	>>"$REPO_ROOT/.deploy/history.log"
info "되돌렸습니다. 확인: python3 scripts/smoke_test.py --base-url https://<도메인> --no-import"
