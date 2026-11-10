#!/bin/sh
# 덤프 하나를 대상 DB로 복원한다 — backup 컨테이너 이미지 안에서 돈다.
#   pg-restore.sh <덤프 파일 이름(/backups 안)> <대상 DB>
# 대상 DB를 지우고 새로 만든 뒤 pg_restore 하고, 표별 행 수를 찍는다.
# 확인 질문과 백엔드 중지·재시작은 부르는 쪽(scripts/ops/restore.sh)이 한다. 직접 부르지 않는다.
set -eu

file="${1:?덤프 파일 이름}"
target="${2:?대상 DB 이름}"
path="${BACKUP_DIR:-/backups}/$file"

[ -f "$path" ] || {
	echo "[restore] 덤프 파일이 없습니다: $path" >&2
	exit 2
}
pg_restore --list "$path" >/dev/null || {
	echo "[restore] 읽을 수 없는 덤프입니다: $file" >&2
	exit 2
}

echo "[restore] 대상 DB '$target'을 지우고 새로 만듭니다"
dropdb --if-exists --force --maintenance-db=postgres "$target"
createdb --maintenance-db=postgres "$target"
# 덤프에 public 스키마 정의가 들어 있으면(스키마를 다시 만든 적이 있는 DB) 새 DB의 public과 부딪힌다
if pg_restore --list "$path" | grep -q ' SCHEMA - public '; then
	psql -q -d "$target" -c 'DROP SCHEMA IF EXISTS public'
fi

echo "[restore] 복원 중: $file → $target"
pg_restore --dbname="$target" --no-owner --exit-on-error "$path"

echo "[restore] 표별 행 수 ($target)"
psql -d "$target" -At -F '  ' -c "
  SELECT c.relname,
         (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I', c.relname), false, true, '')))[1]::text
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relkind = 'r'
  ORDER BY c.relname"
echo "[restore] 끝"
