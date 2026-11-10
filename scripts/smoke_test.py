"""배포 스모크 테스트 — 떠 있는 서버에 사용자 한 명의 전체 흐름을 한 번 태워 본다.

표준 라이브러리만 쓴다(Python 3.10 이상). 서버 밖 어디서든, 또는 backend 컨테이너 안에서 돌릴 수 있다.

    python3 scripts/smoke_test.py --base-url https://persona.example.com
    python3 scripts/smoke_test.py --base-url https://localhost:8143 --insecure          # 자체 서명 인증서
    python3 scripts/smoke_test.py --base-url https://persona.example.com --invite-code <초대 코드>
    docker compose ... exec -T backend python - --base-url https://<도메인> --connect-to caddy:443 \
        < scripts/smoke_test.py                                                      # deploy.sh가 쓰는 방식

흐름: /healthz?deep=true → 화면(/) → 가입(임의 사용자) → 내 정보 → CSRF 없는 쓰기 거부 → 상대 생성
→ 페르소나 저장 → 대화방 생성 → 메시지 3건 가져오기 → 첨삭 B(+같은 키 재요청) → 피드백 → 첨삭 C → 사용량
→ 그래프 수집 상태 → 계정 삭제 → 삭제 뒤 세션 무효. 단계마다 OK/실패를 찍고, 하나라도 실패하면 종료 코드 1.

만든 계정은 끝에서 지운다(중간에 실패해도 지우려고 시도한다). 내용은 전부 합성 문장이다.

비용: 서버가 실제 모델(LLM_PROVIDER=anthropic)이면 첨삭 2회와 그래프 수집 3건(메시지당 haiku 호출)이 실제로
나간다. 줄이려면 --no-import(B 첨삭 한 번만), --skip-model(모델 호출 없음), --health-only(헬스·화면만).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from http.cookies import SimpleCookie
from typing import Any

SESSION_COOKIE = "pi_session"

MESSAGES = [
    {
        "id": "s01",
        "speaker": "contact",
        "sent_at": "2026-11-02T09:30:00+09:00",
        "text": "수요일 회의 자료는 준비되고 있나요?",
    },
    {
        "id": "s02",
        "speaker": "me",
        "sent_at": "2026-11-02T09:32:00+09:00",
        "text": "네, 초안은 오늘 오후 3시까지 공유드리겠습니다.",
    },
    {
        "id": "s03",
        "speaker": "me",
        "sent_at": "2026-11-02T09:33:00+09:00",
        "text": "표 두 개만 더 넣으면 됩니다. 확인 부탁드립니다.",
    },
]
PERSONA = {
    "formality": 4,
    "friendliness": 3,
    "directness": 3,
    "length": "same",
    "emoji": "none",
    "preferred_phrases": [],
    "forbidden_phrases": [],
    "notes": "배포 확인용 스모크 테스트",
}
DRAFT = "회의 자료는 내일 오전 10시까지 보내 드리겠습니다."


class StepError(Exception):
    """단계 실패. 메시지는 그대로 화면에 찍힌다."""


class Response:
    def __init__(self, status: int, headers: Any, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def error_text(self) -> str:
        """오류 응답을 한 줄로: `HTTP 403 forbidden: 메시지`."""
        data = self.json()
        if isinstance(data, dict):
            detail = data.get("detail") if isinstance(data.get("detail"), dict) else data
            code, message = detail.get("code"), detail.get("message")
            if code or message:
                return f"HTTP {self.status} {code or ''}: {message or ''}".strip()
        text = self.body[:200].decode("utf-8", "replace").strip()
        return f"HTTP {self.status} {text}".strip()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TCP는 지정한 주소로 붙고, TLS 이름(SNI)과 Host 헤더는 URL의 호스트를 그대로 쓴다.

    `curl --connect-to`와 같은 동작이다. 서버 안에서 프록시 컨테이너로 바로 붙어
    실제 도메인의 인증서를 확인할 때 쓴다.
    """

    pin: tuple[str, int] | None = None

    def connect(self) -> None:
        if self.pin is None:
            super().connect()
            return
        sock = socket.create_connection(self.pin, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    pin: tuple[str, int] | None = None

    def connect(self) -> None:
        if self.pin is None:
            super().connect()
            return
        self.sock = socket.create_connection(self.pin, self.timeout)


class _PinnedHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext, pin: tuple[str, int]) -> None:
        super().__init__(context=context)
        self._pin_context = context
        self._pin = pin

    def _factory(self, cls: type) -> Callable[..., http.client.HTTPConnection]:
        pin = self._pin

        def make(host: str, **kwargs: Any) -> http.client.HTTPConnection:
            conn = cls(host, **kwargs)
            conn.pin = pin
            return conn

        return make

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(self._factory(_PinnedHTTPSConnection), req, context=self._pin_context)

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(self._factory(_PinnedHTTPConnection), req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가지 않는다 — 받은 상태 코드를 그대로 본다."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Client:
    """세션 쿠키와 CSRF 토큰을 들고 다니는 얇은 HTTP 클라이언트."""

    def __init__(self, base_url: str, *, insecure: bool, connect_to: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.is_https = self.base_url.startswith("https://")
        self.timeout = timeout
        self.cookies: dict[str, str] = {}
        self.csrf: str | None = None
        self.last_set_cookie: list[str] = []

        context = ssl.create_default_context()
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        handlers: list[Any] = [_NoRedirect()]
        if connect_to:
            host, _, port = connect_to.rpartition(":")
            if not host or not port.isdigit():
                raise SystemExit("--connect-to 는 host:port 형식입니다 (예: caddy:443)")
            # 다른 주소로 직접 붙을 때는 프록시 환경변수를 무시한다
            handlers += [urllib.request.ProxyHandler({}), _PinnedHandler(context, (host, int(port)))]
        else:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        csrf: bool = True,
        headers: dict[str, str] | None = None,
    ) -> Response:
        data = None
        send: dict[str, str] = {"Accept": "application/json", "User-Agent": "persona-ize-smoke/1"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            send["Content-Type"] = "application/json"
        if self.cookies:
            send["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if csrf and self.csrf and method not in ("GET", "HEAD"):
            send["X-CSRF-Token"] = self.csrf
        send.update(headers or {})
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=send)
        try:
            with self._opener.open(req, timeout=self.timeout) as res:
                response = Response(res.status, res.headers, res.read())
        except urllib.error.HTTPError as exc:
            response = Response(exc.code, exc.headers, exc.read())
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            raise StepError(f"연결 실패: {reason}") from exc
        self._remember_cookies(response)
        return response

    def _remember_cookies(self, response: Response) -> None:
        self.last_set_cookie = response.headers.get_all("Set-Cookie") or []
        for raw in self.last_set_cookie:
            jar = SimpleCookie()
            try:
                jar.load(raw)
            except Exception:  # noqa: BLE001 - 못 읽는 쿠키는 무시
                continue
            for name, morsel in jar.items():
                if morsel.value == "" or morsel["max-age"] == "0":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value


class Smoke:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = Client(
            args.base_url, insecure=args.insecure, connect_to=args.connect_to, timeout=args.timeout
        )
        self.username = f"smoke-{secrets.token_hex(4)}"
        self.password = secrets.token_urlsafe(18)
        self.registered = False
        self.deleted = False
        self.failures = 0
        self.warnings = 0
        self.passed = 0
        self.contact_id: str | None = None
        self.conversation_id: str | None = None
        self.import_job: dict[str, Any] | None = None
        self.rewrite_ids: list[str] = []
        self.model_calls = 0

    # ---- 출력 ----
    def _line(self, mark: str, name: str, detail: str, started: float | None = None) -> None:
        took = f"  ({int((time.perf_counter() - started) * 1000)}ms)" if started is not None else ""
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else "") + took, flush=True)

    def warn(self, name: str, detail: str) -> None:
        self.warnings += 1
        self._line("주의", name, detail)

    def run(self, name: str, fn: Callable[[], str | None]) -> bool:
        """단계 하나를 돌린다. 성공하면 True."""
        started = time.perf_counter()
        try:
            detail = fn() or ""
        except StepError as exc:
            self.failures += 1
            self._line("실패", name, str(exc), started)
            return False
        except Exception as exc:  # noqa: BLE001 - 예상 못 한 응답 모양도 실패로 보고하고 계속
            self.failures += 1
            self._line("실패", name, f"{type(exc).__name__}: {exc}", started)
            return False
        self.passed += 1
        self._line(" OK ", name, detail, started)
        return True

    def expect(self, response: Response, *statuses: int) -> Any:
        if response.status not in statuses:
            raise StepError(response.error_text())
        return response.json()

    # ---- 단계 ----
    def health(self) -> str:
        data = self.expect(self.client.request("GET", "/healthz?deep=true"), 200)
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise StepError(f"status가 ok가 아닙니다: {data}")
        shown = {k: data[k] for k in ("storage", "graph", "llm_provider") if k in data}
        shown.update(data.get("checks") or {})
        return " ".join(f"{k}={v}" for k, v in shown.items())

    def page(self) -> str:
        res = self.client.request("GET", "/", headers={"Accept": "text/html"})
        if res.status != 200:
            raise StepError(f"HTTP {res.status}")
        content_type = res.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            raise StepError(f"HTML이 아닙니다: {content_type}")
        if self.client.is_https:
            if not res.headers.get("Strict-Transport-Security"):
                self.warn("보안 헤더", "https인데 Strict-Transport-Security가 없습니다")
            if res.headers.get("Server"):
                self.warn("보안 헤더", f"Server 헤더가 보입니다: {res.headers.get('Server')}")
        return f"{len(res.body)} bytes"

    def auth_config(self) -> str:
        res = self.client.request("GET", "/v1/auth/config")
        if res.status == 404:
            return "이 서버 버전에는 없는 주소 (건너뜀)"
        data = self.expect(res, 200)
        if data.get("allow_register") is False:
            raise StepError("가입이 닫혀 있습니다(AUTH_ALLOW_REGISTER=false). --health-only로 돌리세요.")
        if data.get("invite_required") and not self.args.invite_code:
            raise StepError("초대 코드가 필요한 서버입니다. --invite-code 를 주세요.")
        keys = ("allow_register", "invite_required", "require_consent", "app_env")
        return " ".join(f"{k}={data[k]}" for k in keys if k in data)

    def register(self) -> str:
        body: dict[str, Any] = {"username": self.username, "password": self.password, "consent": True}
        if self.args.invite_code:
            body["invite_code"] = self.args.invite_code
        res = self.client.request("POST", "/v1/auth/register", body)
        data = self.expect(res, 201)
        self.registered = True
        self.client.csrf = data["csrf_token"]
        if SESSION_COOKIE not in self.client.cookies:
            raise StepError(f"세션 쿠키({SESSION_COOKIE})가 내려오지 않았습니다.")
        flags = " ".join(self.client.last_set_cookie).lower()
        if "httponly" not in flags:
            raise StepError("세션 쿠키에 HttpOnly가 없습니다.")
        if self.client.is_https and "secure" not in flags:
            raise StepError("https인데 세션 쿠키에 Secure가 없습니다 (SESSION_COOKIE_SECURE=true 필요).")
        return f"사용자 {self.username}"

    def me(self) -> str:
        data = self.expect(self.client.request("GET", "/v1/auth/me"), 200)
        if data.get("username") != self.username:
            raise StepError(f"다른 사용자가 돌아왔습니다: {data.get('username')}")
        return "세션 쿠키로 인증됨"

    def csrf_enforced(self) -> str:
        res = self.client.request(
            "POST", "/v1/contacts", {"alias": "막혀야 함", "relationship_type": "colleague"}, csrf=False
        )
        if res.status != 403:
            raise StepError(f"CSRF 토큰 없는 쓰기가 거부되지 않았습니다: HTTP {res.status}")
        return "토큰 없는 쓰기 요청은 403"

    def create_contact(self) -> str:
        body = {"alias": "스모크 상대", "relationship_type": "colleague"}
        data = self.expect(self.client.request("POST", "/v1/contacts", body), 201)
        self.contact_id = data["id"]
        return ""

    def save_persona(self) -> str:
        body = {"settings": PERSONA, "expected_version": 0}
        data = self.expect(self.client.request("PUT", f"/v1/contacts/{self.contact_id}/persona", body), 200)
        return f"버전 {data['version']}"

    def create_conversation(self) -> str:
        body = {"contact_id": self.contact_id, "title": "스모크 대화", "source_key": "smoke", "me_name": "나"}
        data = self.expect(self.client.request("POST", "/v1/conversations", body), 201)
        self.conversation_id = data["id"]
        return ""

    def import_messages(self) -> str:
        path = f"/v1/conversations/{self.conversation_id}/imports"
        data = self.expect(self.client.request("POST", path, {"messages": MESSAGES}), 202)
        self.import_job = data
        if data.get("imported") != len(MESSAGES):
            raise StepError(f"가져온 수가 다릅니다: {data.get('imported')} (기대 {len(MESSAGES)})")
        if not data.get("episodes"):
            raise StepError("문체 묶음이 만들어지지 않았습니다 (임베딩 확인).")
        return f"메시지 {data['imported']}건, 묶음 {data['episodes']}개, 그래프 {data.get('graph_status')}"

    def rewrite(self, condition: str, *, replay: bool = False) -> str:
        body = {
            "contact_id": self.contact_id,
            "draft_text": DRAFT,
            "situation": "배포 확인용 스모크 테스트",
            "condition": condition,
        }
        key = f"smoke-{uuid.uuid4()}"
        res = self.client.request("POST", "/v1/rewrites", body, headers={"Idempotency-Key": key})
        data = self.expect(res, 201)
        self.model_calls += 1
        result = data["result"]
        self.rewrite_ids.append(data["id"])
        if not result.get("result_text"):
            raise StepError("결과 문장이 비어 있습니다.")
        if result["status"] != "recommended":
            reason = "; ".join(result.get("warnings") or [])[:200]
            if result.get("model_version") in (None, "", "unknown"):
                # 모델이 한 번도 답하지 못했다 — API 키·크레딧·네트워크 문제
                raise StepError(f"모델 호출 실패로 원문을 유지했습니다: {reason}")
            self.warn(f"첨삭 {condition}", f"검증을 통과하지 못해 원문을 유지했습니다: {reason}")
        if replay:
            again = self.client.request("POST", "/v1/rewrites", body, headers={"Idempotency-Key": key})
            if self.expect(again, 201)["id"] != data["id"]:
                raise StepError("같은 Idempotency-Key 재요청이 다른 결과를 만들었습니다.")
        tokens = result.get("token_usage") or {}
        return (
            f"{result['status']} 모델={result.get('model_version')} 검색={result.get('retrieval_version')} "
            f"토큰={tokens.get('input_tokens', 0)}+{tokens.get('output_tokens', 0)} "
            f"{result.get('latency_ms')}ms"
        )

    def feedback(self) -> str:
        path = f"/v1/rewrites/{self.rewrite_ids[0]}/feedback"
        self.expect(self.client.request("POST", path, {"accepted": True, "consent_to_reuse": False}), 204)
        return ""

    def usage(self) -> str:
        data = self.expect(self.client.request("GET", "/v1/usage"), 200)
        if data["rewrites"] < self.model_calls:
            raise StepError(f"사용량이 세어지지 않았습니다: {data['rewrites']} (요청 {self.model_calls}회)")
        tokens = f"{data['input_tokens']}+{data['output_tokens']}"
        return f"오늘 {data['rewrites']}/{data['rewrite_limit']}회, 토큰 {tokens}"

    def graph_status(self) -> str:
        """가져오기 뒤 백그라운드 그래프 수집이 끝날 때까지 기다린다(끝나기 전에 계정을 지우지 않으려고)."""
        assert self.import_job is not None
        job = self.import_job
        deadline = time.monotonic() + self.args.graph_wait
        while job.get("graph_status") in ("pending", "running") and time.monotonic() < deadline:
            time.sleep(2)
            job = self.expect(self.client.request("GET", f"/v1/imports/{job['id']}"), 200)
        status = job.get("graph_status")
        detail = f"{status}, 에피소드 {job.get('graph_episodes')}개"
        if status == "completed" or status == "skipped":
            return detail
        problem = f"{detail} {job.get('graph_error') or ''}".strip()
        if self.args.require_graph:
            raise StepError(problem)
        self.warn("그래프 수집", problem + " (--require-graph 를 주면 실패로 칩니다)")
        return "끝까지 가지 못함(주의 참고)"

    def delete_account(self) -> str:
        res = self.client.request("DELETE", "/v1/auth/me", {"password": self.password})
        data = self.expect(res, 200)
        self.deleted = True
        note = f" 그래프 오류: {data['graph_error']}" if data.get("graph_error") else ""
        return f"상대 {data.get('contacts')}명, 메시지 {data.get('messages')}건 삭제{note}"

    def session_gone(self) -> str:
        res = self.client.request("GET", "/v1/auth/me")
        if res.status != 401:
            raise StepError(f"계정 삭제 뒤에도 세션이 살아 있습니다: HTTP {res.status}")
        return "삭제 뒤 401"

    # ---- 전체 ----
    def main(self) -> int:
        args = self.args
        print(f"스모크 테스트: {args.base_url}" + (f" (연결 {args.connect_to})" if args.connect_to else ""))
        ok = self.run("헬스체크 /healthz?deep=true", self.health)
        ok = self.run("화면 /", self.page) and ok
        if args.health_only or not ok:
            return self.finish()
        try:
            self.flow()
        finally:
            if self.registered and not self.deleted and not args.keep_account:
                if not self.run("정리: 계정 삭제", self.delete_account):
                    print(f"       남은 테스트 계정: {self.username} — 직접 지워야 합니다.")
        return self.finish()

    def flow(self) -> None:
        args = self.args
        if not self.run("가입 설정 /v1/auth/config", self.auth_config):
            return
        if not self.run("가입", self.register):
            return
        self.run("내 정보", self.me)
        self.run("CSRF 검사", self.csrf_enforced)
        if not self.run("상대 생성", self.create_contact):
            return
        if not self.run("페르소나 저장", self.save_persona):
            return
        imported = False
        if not args.no_import and not args.skip_model:
            if self.run("대화방 생성", self.create_conversation):
                imported = self.run("메시지 3건 가져오기", self.import_messages)
        if not args.skip_model:
            if self.run("첨삭 B (+같은 키 재요청)", lambda: self.rewrite("B", replay=True)):
                self.run("피드백 저장", self.feedback)
            if imported:
                self.run("첨삭 C (과거 표현 검색)", lambda: self.rewrite("C"))
            self.run("사용량 조회", self.usage)
        if imported:
            self.run("그래프 수집 상태", self.graph_status)
        if args.keep_account:
            print(f"       --keep-account: 계정 {self.username} 을 남겨 둡니다.")
            return
        if self.run("계정 삭제", self.delete_account):
            self.run("삭제 뒤 세션 무효", self.session_gone)

    def finish(self) -> int:
        summary = f"통과 {self.passed}, 실패 {self.failures}, 주의 {self.warnings}"
        print(("스모크 실패 — " if self.failures else "스모크 통과 — ") + summary)
        return 1 if self.failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="배포 스모크 테스트 (표준 라이브러리만)")
    p.add_argument("--base-url", required=True, help="서버 주소. 예) https://persona.example.com")
    p.add_argument("--insecure", action="store_true", help="인증서 검증을 끈다 (자체 서명·localhost)")
    p.add_argument(
        "--invite-code",
        default=os.environ.get("SMOKE_INVITE_CODE") or os.environ.get("REGISTER_INVITE_CODE") or "",
        help="가입 초대 코드 (기본: 환경변수 SMOKE_INVITE_CODE 또는 REGISTER_INVITE_CODE)",
    )
    p.add_argument(
        "--connect-to",
        metavar="HOST:PORT",
        help="TCP 연결만 이 주소로 보낸다. 인증서 이름과 Host 헤더는 --base-url 그대로 (예: caddy:443)",
    )
    p.add_argument("--timeout", type=float, default=90.0, help="요청 하나의 제한 시간(초). 기본 90")
    p.add_argument("--health-only", action="store_true", help="헬스체크와 화면만 본다 (계정을 만들지 않는다)")
    p.add_argument("--skip-model", action="store_true", help="가져오기·첨삭을 건너뛴다 (모델 호출 없음)")
    p.add_argument("--no-import", action="store_true", help="가져오기와 C 첨삭을 건너뛴다 (그래프 수집 없음)")
    p.add_argument("--graph-wait", type=float, default=90.0, help="그래프 수집이 끝나길 기다리는 시간(초)")
    p.add_argument("--require-graph", action="store_true", help="그래프 수집이 completed가 아니면 실패")
    p.add_argument("--keep-account", action="store_true", help="끝나도 계정을 지우지 않는다 (디버깅용)")
    return p.parse_args(argv)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔에서도 한글이 깨지지 않게
    raise SystemExit(Smoke(parse_args()).main())
