#!/usr/bin/env bash
# 아이폰 앱 화면 흐름 검사(XCUITest)를 시뮬레이터에서 돌린다 — 맥 전용, 모델·DB·Docker 없이.
#
#   scripts/ios_ui_check.sh [시뮬레이터 이름 또는 UDID]      (기본: 부팅된 것, 없으면 첫 iPhone)
#
# 하는 일: 임시 인증서(localhost, 30일) 생성 → 시뮬레이터에 그 루트 인증서 등록 → 가짜 모델·메모리 저장소로
# 서버를 https://localhost:<PORT>(중계 뒤)에 띄움 → `PersonaIzeUITests` 스킴 실행 → 서버 종료.
# 앱은 HTTPS 주소만 받으므로 인증서가 필요하다. 운영 서버로는 요청이 가지 않는다(검사가 로컬 주소만 받는다).
# 서명을 끄고(CODE_SIGNING_ALLOWED=NO) 빌드하면 키체인 저장이 실패해 '다시 열기' 단계가 깨진다 — 끄지 않는다.
# 램 8GB 맥에서는 시뮬레이터 첫 부팅 뒤 몇 분 동안 검사 실행기가 멈춰 죽을 수 있다. 부하가 가라앉은 뒤 다시 돌린다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8443}"
BACKEND_PORT="${BACKEND_PORT:-$((PORT + 1))}"  # 서버. 앱은 PORT의 중계(scripts/ios_ui_relay.py)로 닿는다
WORK="${WORK:-$(mktemp -d -t persona-ios-ui)}"
PY="$ROOT/.venv/bin/python"

if ! xcrun simctl help >/dev/null 2>&1; then
  export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi

device="${1:-}"
if [ -z "$device" ]; then
  device="$(xcrun simctl list devices booted -j | python3 -c '
import json, sys
print(next((d["udid"] for ds in json.load(sys.stdin)["devices"].values() for d in ds), ""))')"
fi
if [ -z "$device" ]; then
  device="$(xcrun simctl list devices available -j | python3 -c '
import json, sys
devices = json.load(sys.stdin)["devices"]
print(next(d["udid"] for runtime, ds in devices.items() if ".iOS-" in runtime
           for d in ds if d["name"].startswith("iPhone")))')"
fi

# 1. 인증서: 루트 하나와 그 루트가 서명한 localhost 인증서.
mkdir -p "$WORK/tls"
cd "$WORK/tls"
if [ ! -f srv.pem ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.pem -days 30 \
    -subj "/CN=persona-ize sim test CA" -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -keyout srv.key -out srv.csr -subj "/CN=localhost" 2>/dev/null
  printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' > ext.cnf
  openssl x509 -req -in srv.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out srv.pem -days 30 \
    -extfile ext.cnf 2>/dev/null
fi

# 2. 시뮬레이터: 부팅하고 루트 인증서를 믿게 한다.
xcrun simctl boot "$device" 2>/dev/null || true
xcrun simctl bootstatus "$device" >/dev/null
xcrun simctl keychain "$device" add-root-cert "$WORK/tls/ca.pem"

# 3. 서버: .env의 값보다 여기 환경변수가 먼저다 — 실제 모델·DB·그래프를 쓰지 않는다.
cd "$ROOT"
LLM_PROVIDER=fake EMBEDDING_PROVIDER=fake DATABASE_URL= NEO4J_URI= GRAPH_RESUME_ON_START=false \
  "$PY" -m uvicorn app.main:app --app-dir backend --port "$BACKEND_PORT" \
  --ssl-keyfile "$WORK/tls/srv.key" --ssl-certfile "$WORK/tls/srv.pem" > "$WORK/server.log" 2>&1 &
server=$!
# 중계: 검사가 https://localhost:<PORT>/__relay/down·drop·up으로 서버를 끊었다 잇는다(오류 뒤 재시도 검사).
"$PY" scripts/ios_ui_relay.py --port "$PORT" --backend "localhost:$BACKEND_PORT" --ca "$WORK/tls/ca.pem" \
  --cert "$WORK/tls/srv.pem" --key "$WORK/tls/srv.key" > "$WORK/relay.log" 2>&1 &
relay=$!
trap 'kill "$server" "$relay" 2>/dev/null || true' EXIT
curl -s --retry 30 --retry-delay 2 --retry-connrefused --cacert "$WORK/tls/ca.pem" \
  -o /dev/null "https://localhost:$PORT/healthz"

# 4. 검사.
cd "$ROOT/apps/ios"
TEST_RUNNER_PERSONA_UITEST_SERVER="https://localhost:$PORT" \
  xcodebuild -project PersonaIze.xcodeproj -scheme PersonaIzeUITests \
  -destination "platform=iOS Simulator,id=$device" -derivedDataPath "$WORK/dd" test \
  2>&1 | tee "$WORK/xcodebuild.log" | grep -E "Test Case|error:|Executed|\*\* TEST"
echo "로그: $WORK/xcodebuild.log · $WORK/server.log · $WORK/relay.log"
