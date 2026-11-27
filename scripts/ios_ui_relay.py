"""화면 흐름 검사(`scripts/ios_ui_check.sh`)용 HTTPS 중계 — 표준 라이브러리만.

앱 → 중계(https://localhost:<port>) → 서버(https://localhost:<backend>). 검사 실행기(시뮬레이터 안)는 서버를
끄고 켤 수 없으므로, 같은 주소의 `/__relay/*`로 중계의 상태를 바꾼다:

- `up`: 그대로 전달한다(기본).
- `down`: 요청을 읽고 응답 없이 연결을 끊는다 — 서버에 닿지 않는다(서버가 내려간 것과 같다).
- `drop`: 서버에 전달해 처리시킨 뒤 응답을 버리고 끊는다 — 서버는 처리했는데 앱은 응답을 못 받은 경우.
- `GET /__relay/log`: 지금까지의 요청 기록(방법·경로·`Idempotency-Key`·상태·결과). 본문은 남기지 않는다.
  `clear`는 기록을 비운다.

TLS를 그대로 넘기지 않고 여기서 풀었다 다시 거는 것은 요청 머리의 멱등 키를 보고, 응답만 버리는
`drop`을 하려면 HTTP 요청 경계를 알아야 해서다. 인증서는 스크립트가 만든 localhost 인증서를 함께 쓴다.
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTROL = "/__relay/"
MODES = ("up", "down", "drop")
# 다음 구간으로 넘기지 않는 머리(hop-by-hop). 응답의 길이는 여기서 다시 단다.
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class Relay:
    def __init__(self, backend: str, ca: str) -> None:
        self.host, _, port = backend.rpartition(":")
        self.port = int(port)
        self.context = ssl.create_default_context(cafile=ca)
        self.mode = "up"
        self.records: list[dict] = []
        self.lock = threading.Lock()

    def record(self, entry: dict) -> None:
        with self.lock:
            self.records.append(entry)
        print(json.dumps(entry, ensure_ascii=False), file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    relay: Relay

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 부모 서명
        pass  # 기록은 `Relay.record`가 한 줄씩 남긴다

    def _reply(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _control(self) -> None:
        name = self.path[len(CONTROL) :].split("?", 1)[0]
        relay = self.relay
        if name in MODES:
            relay.mode = name
            print(json.dumps({"control": name}), file=sys.stderr, flush=True)
            self._reply(200, {"mode": name})
        elif name == "clear":
            with relay.lock:
                relay.records.clear()
            self._reply(200, {"mode": relay.mode})
        elif name == "state":
            self._reply(200, {"mode": relay.mode})
        elif name == "log":
            with relay.lock:
                records = list(relay.records)
            self._reply(200, {"mode": relay.mode, "requests": records})
        else:
            self._reply(404, {"error": "unknown"})

    def _body(self) -> bytes | None:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return None
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _forward(self) -> None:
        if self.path.startswith(CONTROL):
            self._control()
            return
        relay = self.relay
        mode = relay.mode
        entry = {
            "method": self.command,
            "path": self.path.split("?", 1)[0],
            "key": self.headers.get("Idempotency-Key"),
            "mode": mode,
        }
        body = self._body()
        if body is None:
            entry["outcome"] = "chunked-refused"
            relay.record(entry)
            self._reply(411, {"error": "chunked body not supported by relay"})
            return
        if mode == "down":
            entry["outcome"] = "cut"
            relay.record(entry)
            self.close_connection = True
            return
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        conn = http.client.HTTPSConnection(relay.host, relay.port, context=relay.context, timeout=120)
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            response = conn.getresponse()
            data = response.read()
        except OSError as exc:
            entry["outcome"] = f"backend-error:{type(exc).__name__}"
            relay.record(entry)
            self._reply(502, {"error": "backend unreachable"})
            return
        finally:
            conn.close()
        entry["status"] = response.status
        if mode == "drop":
            entry["outcome"] = "dropped"
            relay.record(entry)
            self.close_connection = True
            return
        entry["outcome"] = "passed"
        relay.record(entry)
        self.send_response_only(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() not in HOP:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = _forward  # noqa: N815 - http.server가 부르는 이름
    do_POST = _forward  # noqa: N815 - http.server가 부르는 이름
    do_PUT = _forward  # noqa: N815 - http.server가 부르는 이름
    do_PATCH = _forward  # noqa: N815 - http.server가 부르는 이름
    do_DELETE = _forward  # noqa: N815 - http.server가 부르는 이름
    do_HEAD = _forward  # noqa: N815 - http.server가 부르는 이름
    do_OPTIONS = _forward  # noqa: N815 - http.server가 부르는 이름


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--backend", required=True, help="host:port (HTTPS)")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--ca", required=True, help="서버 인증서를 믿을 루트")
    args = parser.parse_args()

    Handler.relay = Relay(args.backend, args.ca)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    # 핸드셰이크는 요청 처리 스레드에서 한다(받는 고리를 막지 않게).
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    print(json.dumps({"listening": args.port, "backend": args.backend}), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
