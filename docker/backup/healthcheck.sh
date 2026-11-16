#!/bin/sh
# backup 컨테이너 헬스체크: 최근 덤프가 (백업 주기 + 2시간) 안에 있으면 정상.
# `docker compose ps`에 unhealthy로 보이면 백업이 밀린 것이다 → `docker compose logs backup`.
set -eu
INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
[ "$INTERVAL_HOURS" -le 0 ] && exit 0
find "${BACKUP_DIR:-/backups}" -maxdepth 1 -type f -name 'pz-*.dump' -mmin "-$(((INTERVAL_HOURS + 2) * 60))" | grep -q .
