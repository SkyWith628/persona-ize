#!/usr/bin/env bash
# 지금 PostgreSQL 백업을 한 번 뜬다 (주기 백업은 backup 컨테이너가 알아서 한다 — 이건 배포 직전·수동용).
#   scripts/ops/backup.sh           # pg_dump 커스텀 포맷 → <BACKUP_DIR>/pz-<DB>-<UTC 시각>.dump, 보존 정리
#   scripts/ops/backup.sh --list    # 지금 있는 백업 목록
# 보존 일수는 .env.production의 BACKUP_RETENTION_DAYS(기본 14). 파일에는 대화 원문이 들어 있다 —
# 서버 밖으로 옮길 때는 암호화한다(docs/guides/deploy.md "백업·복원"). Neo4j 그래프는 scripts/ops/neo4j-dump.sh.
. "$(dirname "$0")/_lib.sh"
require_env_file

dir="$(backup_dir)"
list_backups() {
	if compgen -G "$dir/pz-*.dump" >/dev/null; then
		ls -lh "$dir"/pz-*.dump
	else
		echo "(백업 없음: $dir)"
	fi
}

case "${1:-}" in
--list)
	list_backups
	exit 0
	;;
"") ;;
*) die "모르는 인자: $1 (사용법은 파일 머리말)" ;;
esac

mkdir -p "$dir"
info "PostgreSQL 백업을 시작합니다 → $dir"
# backup 서비스 정의(같은 이미지·네트워크·볼륨)로 일회용 컨테이너를 띄워 한 번만 돌린다.
compose run --rm --no-deps -T --entrypoint sh backup /opt/backup/pg-backup.sh
info "끝. 최근 백업:"
list_backups | tail -n 5
