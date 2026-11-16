#!/usr/bin/env bash
# 운영 구성에 대한 docker compose 명령을 짧게 쓴다 (env 파일·compose 파일 인자를 대신 붙인다).
#   scripts/ops/compose.sh ps
#   scripts/ops/compose.sh logs -f --tail 100 backend
#   scripts/ops/compose.sh exec caddy caddy reload --config /etc/caddy/Caddyfile
# 다른 env 파일: PZ_ENV_FILE=/path/to/env scripts/ops/compose.sh ...
. "$(dirname "$0")/_lib.sh"
require_env_file
compose "$@"
