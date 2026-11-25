#!/usr/bin/env bash
# Neo4j(그래프) 덤프·복원. 커뮤니티판은 켜진 채로 덤프할 수 없어서 **neo4j를 잠깐 내렸다가** 다시 올린다.
#
#   scripts/ops/neo4j-dump.sh                  # 덤프 → <BACKUP_DIR>/neo4j/pz-neo4j-<UTC 시각>.dump
#   scripts/ops/neo4j-dump.sh --load <파일>    # 그 덤프로 현재 그래프를 덮어쓴다 (확인 질문, --yes로 생략)
#
# 내려가 있는 동안(보통 1분 안쪽) 백엔드는 떠 있지만 D 조건 첨삭과 그래프 수집은 실패할 수 있다 → 한가한 시간에.
# 그래프는 PostgreSQL에 남은 원문에서 scripts/graph_reset.py로 다시 만들 수도 있다. 다만 메시지마다 haiku를
# 다시 부르므로 비용과 시간이 든다(docs/guides/deploy.md "백업·복원"). 그래서 이 덤프를 가끔 떠 두는 편이 싸다.
# 보존 정리는 하지 않는다 — 오래된 파일은 직접 지운다.
. "$(dirname "$0")/_lib.sh"
require_env_file

mode="dump" load_file="" yes="no"
while [ $# -gt 0 ]; do
	case "$1" in
	--load)
		mode="load"
		load_file="${2:?--load 뒤에 덤프 파일}"
		shift 2
		;;
	--yes)
		yes="yes"
		shift
		;;
	*) die "모르는 인자: $1" ;;
	esac
done

dir="$(backup_dir)/neo4j"
mkdir -p "$dir"

# neo4j-admin을 이미지의 시작 스크립트 없이 neo4j 사용자로 직접 돌린다(시작 스크립트는 표준 출력에 안내를 섞는다).
admin() {
	compose run --rm --no-deps -T --user neo4j --entrypoint neo4j-admin neo4j "$@"
}

restart_neo4j() {
	info "neo4j를 다시 올립니다"
	compose up -d --no-build --wait --wait-timeout 300 neo4j
}

if [ "$mode" = "dump" ]; then
	out="$dir/pz-neo4j-$(date -u +%Y%m%dT%H%M%SZ).dump"
	info "neo4j를 내립니다 (덤프하는 동안 그래프 기능이 멈춥니다)"
	compose stop neo4j
	trap restart_neo4j EXIT
	info "덤프 중 → $out"
	if ! (umask 077 && admin database dump neo4j --to-stdout >"$out.partial"); then
		rm -f "$out.partial"
		die "neo4j-admin dump 실패 (neo4j는 다시 올립니다)"
	fi
	[ -s "$out.partial" ] || {
		rm -f "$out.partial"
		die "덤프가 비어 있습니다 (neo4j는 다시 올립니다)"
	}
	mv "$out.partial" "$out"
	info "완료: $out ($(wc -c <"$out") bytes)"
	exit 0
fi

# ---- 복원 ----
file="$(basename "$load_file")"
[ -f "$dir/$file" ] || die "덤프 파일이 없습니다: $dir/$file"
if [ "$yes" != "yes" ]; then
	confirm "현재 그래프를 모두 지우고 '$file'로 되돌립니다. 되돌릴 수 없습니다." "neo4j"
fi
info "neo4j를 내립니다"
compose stop neo4j
trap restart_neo4j EXIT
info "복원 중 ← $file"
admin database load neo4j --from-stdin --overwrite-destination=true <"$dir/$file" ||
	die "neo4j-admin load 실패 (neo4j는 다시 올립니다)"
info "복원 끝"
