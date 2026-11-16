#!/bin/sh
# PostgreSQL 백업 1회 — backup 컨테이너(docker-compose.prod.yml) 안에서 돈다.
#   pg_dump(커스텀 포맷) → /backups/pz-<DB>-<UTC 시각>.dump → 읽히는지 확인 → 보존 기간이 지난 파일 삭제
# 접속 정보는 환경변수 PGHOST·PGUSER·PGPASSWORD·PGDATABASE. 호스트에서 한 번 돌리려면 scripts/ops/backup.sh.
set -eu
umask 077 # 덤프에는 대화 원문이 들어 있다 — 소유자만 읽게

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

log() { echo "[backup $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

name="pz-${PGDATABASE}-$(date -u +%Y%m%dT%H%M%SZ).dump"
partial="$BACKUP_DIR/.$name.partial"
final="$BACKUP_DIR/$name"
mkdir -p "$BACKUP_DIR"

log "시작: ${PGDATABASE}@${PGHOST} → $name"
if ! pg_dump --format=custom --compress=6 --no-password --file="$partial"; then
	rm -f "$partial"
	log "실패: pg_dump"
	exit 1
fi
# 읽을 수 있는 아카이브일 때만 정식 이름을 붙인다 (중간에 끊긴 파일이 백업처럼 보이지 않게)
if ! pg_restore --list "$partial" >/dev/null; then
	rm -f "$partial"
	log "실패: 덤프를 읽을 수 없습니다"
	exit 1
fi
mv "$partial" "$final"
log "완료: $name ($(wc -c <"$final") bytes)"

# 보존 정리는 새 덤프가 성공한 뒤에만 한다 — 백업이 계속 실패하는 동안 옛 파일까지 사라지면 안 된다.
if [ "$RETENTION_DAYS" -gt 0 ]; then
	find "$BACKUP_DIR" -maxdepth 1 -type f -name 'pz-*.dump' -mmin "+$((RETENTION_DAYS * 1440))" -print -delete |
		while read -r old; do log "보존 기간(${RETENTION_DAYS}일)이 지나 삭제: $(basename "$old")"; done
fi
# 하루 넘게 남아 있는 임시 파일(중단된 백업) 정리
find "$BACKUP_DIR" -maxdepth 1 -type f -name '.pz-*.partial' -mmin +1440 -delete
