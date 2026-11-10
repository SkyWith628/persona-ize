#!/usr/bin/env bash
# PostgreSQL 덤프를 복원한다. 대상 DB를 지우고 새로 만든 뒤 넣는다 — 되돌릴 수 없으므로 한 번 묻는다.
#
#   scripts/ops/restore.sh <덤프 파일> --db <연습용 DB>   # 복원 연습: 운영 DB는 건드리지 않고 다른 이름의 DB에 넣는다
#   scripts/ops/restore.sh <덤프 파일> --db <연습용 DB> --drop-after   # 행 수만 확인하고 그 DB를 지운다
#   scripts/ops/restore.sh <덤프 파일>                    # 실제 복원: 백엔드를 멈추고 운영 DB를 덮어쓴 뒤 다시 띄운다
#   --yes  확인 질문을 건너뛴다(자동화용)
#   --no-backup  실제 복원 전에 지금 상태를 백업하지 않는다 (기본은 먼저 백업을 한 번 뜬다)
#
# <덤프 파일>은 백업 폴더(BACKUP_DIR, 기본 ./backups) 안의 파일 이름 또는 그 경로. 목록: scripts/ops/backup.sh --list
# 실제 복원 뒤 백엔드가 시작하면서 `alembic upgrade head`를 돌리므로, 옛 스키마의 덤프도 최신 스키마로 올라온다.
# 그래프(Neo4j)는 여기서 다루지 않는다: 덤프 시점 이후에 수집된 그래프와 어긋나면 docs/deploy.md "복원 뒤 그래프" 참고.
. "$(dirname "$0")/_lib.sh"
require_env_file

dump="" target="" yes="no" drop_after="no" pre_backup="yes"
while [ $# -gt 0 ]; do
	case "$1" in
	--db)
		target="${2:?--db 뒤에 DB 이름}"
		shift 2
		;;
	--yes)
		yes="yes"
		shift
		;;
	--drop-after)
		drop_after="yes"
		shift
		;;
	--no-backup)
		pre_backup="no"
		shift
		;;
	-*) die "모르는 옵션: $1" ;;
	*)
		[ -z "$dump" ] || die "덤프 파일은 하나만 지정합니다."
		dump="$1"
		shift
		;;
	esac
done
[ -n "$dump" ] || die "덤프 파일을 지정하세요. 목록: scripts/ops/backup.sh --list"

dir="$(backup_dir)"
file="$(basename "$dump")"
[ -f "$dir/$file" ] || die "백업 폴더에 그 파일이 없습니다: $dir/$file"

live_db="$(env_get POSTGRES_DB persona_ize)"
target="${target:-$live_db}"
case "$target" in
*[!A-Za-z0-9_]* | "") die "DB 이름은 영문·숫자·_ 만 됩니다: $target" ;;
postgres | template0 | template1) die "시스템 DB에는 복원하지 않습니다: $target" ;;
esac

restore_into() {
	compose run --rm --no-deps -T --entrypoint sh backup /opt/backup/pg-restore.sh "$file" "$1"
}

if [ "$target" != "$live_db" ]; then
	# ---- 복원 연습: 운영 DB와 백엔드는 그대로 둔다 ----
	if [ "$yes" != "yes" ]; then
		confirm "'$file'을 연습용 DB '$target'에 복원합니다(같은 이름의 DB가 있으면 지웁니다). 운영 DB '$live_db'는 건드리지 않습니다." "$target"
	fi
	restore_into "$target"
	if [ "$drop_after" = "yes" ]; then
		compose run --rm --no-deps -T --entrypoint dropdb backup --if-exists --force --maintenance-db=postgres "$target"
		info "연습용 DB '$target'을 지웠습니다."
	else
		info "연습용 DB '$target'이 남아 있습니다. 다 봤으면 --drop-after 로 다시 돌리거나 dropdb 하세요."
	fi
	exit 0
fi

# ---- 실제 복원: 운영 DB를 덮어쓴다 ----
[ "$drop_after" = "no" ] || die "--drop-after는 연습용 DB(--db)에만 씁니다."
if [ "$yes" != "yes" ]; then
	confirm "운영 DB '$live_db'의 현재 내용을 모두 지우고 '$file'로 되돌립니다." "$live_db"
fi
if [ "$pre_backup" = "yes" ]; then
	# 덤프를 잘못 골랐을 때 돌아올 곳을 남긴다. 실패하면 복원하지 않는다.
	info "복원 전에 지금 상태를 백업합니다 (건너뛰려면 --no-backup)"
	bash "$OPS_DIR/backup.sh" || die "복원 전 백업에 실패했습니다. 원인을 고치거나 --no-backup 으로 다시 실행하세요."
fi
info "백엔드를 멈춥니다 (DB 연결을 끊기 위해)"
compose stop backend
restore_into "$live_db"
info "백엔드를 다시 띄웁니다 (시작하면서 스키마를 최신으로 올립니다)"
compose up -d --no-build --wait --wait-timeout 300 backend
info "복원 끝. 확인: scripts/ops/compose.sh ps"
