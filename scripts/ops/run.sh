#!/usr/bin/env bash
# 저장소의 운영 스크립트(scripts/*.py)를 운영 스택 안에서 돌린다 — 백엔드와 같은 이미지·설정·네트워크로.
# 운영 이미지에는 앱 코드만 들어 있어서, 저장소의 scripts/·data/·evals/를 읽기 전용으로 붙여 실행한다.
#
#   scripts/ops/run.sh scripts/usage_report.py --days 30            # 사용자·일·조건별 사용량
#   scripts/ops/run.sh scripts/housekeeping.py                      # 만료 세션·오래된 작업 정리
#   scripts/ops/run.sh scripts/reset_password.py --username <이름>  # 비밀번호 재설정(물어본다)
#   scripts/ops/run.sh scripts/graph_reset.py --user <이름> --dry-run
#
# 주의
# - graph_reset.py·import_synthetic.py --graph·prepare_demo.py --graph 는 메시지마다 haiku를 부른다(비용).
# - 컨테이너 파일시스템은 읽기 전용이다. 결과를 파일로 받으려면 표준 출력을 호스트에서 리다이렉트한다:
#     scripts/ops/run.sh scripts/usage_report.py --days 30 > usage.md
. "$(dirname "$0")/_lib.sh"
require_env_file

[ $# -gt 0 ] || die "실행할 스크립트를 지정하세요. 예) scripts/ops/run.sh scripts/usage_report.py --days 7"
[ -f "$REPO_ROOT/$1" ] || die "저장소에 그 파일이 없습니다: $1 (저장소 루트 기준 경로로 적습니다)"

mounts=()
for d in scripts data evals; do
	if [ -d "$REPO_ROOT/$d" ]; then mounts+=(-v "$REPO_ROOT/$d:/repo/$d:ro"); fi
done
tty=()
if [ ! -t 0 ] || [ ! -t 1 ]; then tty=(-T); fi

compose run --rm --no-deps "${tty[@]}" "${mounts[@]}" -w /repo backend python "$@"
