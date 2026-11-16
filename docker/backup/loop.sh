#!/bin/sh
# 주기 백업 루프 — backup 컨테이너의 시작 명령.
# "가장 최근 덤프가 BACKUP_INTERVAL_HOURS보다 오래됐으면 백업"을 몇 분마다 확인한다. 시계에 맞춰 자는 방식이
# 아니라서 컨테이너가 재시작돼도 백업이 겹쳐 쌓이지 않고, 서버가 꺼져 있던 동안 밀린 백업은 켜지자마자 한다.
set -eu

INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
CHECK_SECONDS="${BACKUP_CHECK_SECONDS:-300}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
here="$(dirname "$0")"

# PID 1인 sh는 핸들러가 없으면 SIGTERM을 무시한다 → docker stop이 10초를 기다리지 않게 받아서 끝낸다.
trap 'echo "[backup] 종료"; exit 0' TERM INT

if [ "$INTERVAL_HOURS" -le 0 ]; then
	echo "[backup] BACKUP_INTERVAL_HOURS=0 — 주기 백업을 하지 않습니다 (수동: scripts/ops/backup.sh)"
else
	echo "[backup] ${INTERVAL_HOURS}시간마다 백업, 보존 ${BACKUP_RETENTION_DAYS:-14}일, 위치 $BACKUP_DIR"
fi

while :; do
	if [ "$INTERVAL_HOURS" -gt 0 ]; then
		recent="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'pz-*.dump' -mmin "-$((INTERVAL_HOURS * 60))" 2>/dev/null | head -n 1)"
		if [ -z "$recent" ]; then
			sh "$here/pg-backup.sh" || echo "[backup] 이번 백업은 실패했습니다 — ${CHECK_SECONDS}초 뒤 다시 시도합니다"
		fi
	fi
	sleep "$CHECK_SECONDS" &
	wait $!
done
