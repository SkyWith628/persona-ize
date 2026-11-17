"""모바일 웹(`/m`): 서빙, 화면 규칙, 그리고 `mobile.js`가 기대는 서버 계약.

브라우저를 띄우지 않는다 — 화면 파일을 글자로 읽어 확인할 수 있는 것만 본다:
- `/m`이 열리고 `/`는 PC 웹 앱 그대로다. 화면이 가리키는 정적 파일과 `mobile.js`가 가져다 쓰는 PC 쪽
  순수 로직(`js/lib/*.js`)이 전부 열리고, 가져오는 이름이 그 파일에 실제로 있다.
- 화면 규칙: 같은 출처의 모듈 스크립트 하나, 인라인 핸들러·style 없음, 외부 자원 없음, 서버·사용자 글을
  HTML로 해석하게 하는 길(innerHTML 등) 없음, 브라우저 저장소는 한 곳(`store`)으로만.
- `mobile.js`가 부르는 모든 `/v1/…` 경로·메서드가 지금 앱의 라우트에 있다 (서버가 바뀌면 여기서 걸린다).
- 화면이 믿는 인증 계약: 승인제 가입은 202 + 쿠키 없음, 소셜 로그인은 `/m`으로 돌아오고
  실패 코드마다 문구가 있다.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import OAUTH_ERROR_CODES, safe_next, with_auth_error
from app.core.config import Settings
from app.core.middleware import PAGE_PATHS, cache_control_for
from app.main import WEB_DIR, create_app

MOBILE_FILES = ("mobile.html", "mobile.js", "mobile.css")
# mobile.js의 `api("METHOD", "/v1/…")` · `api("METHOD", `/v1/…${…}…`)` 호출
API_CALL = re.compile(r'\bapi\(\s*"(GET|POST|PUT|PATCH|DELETE)",\s*(["`])(/v1/[^"`]*)\2')
IMPORT_FROM = re.compile(r'import\s*\{([^}]*)\}\s*from\s*"(\./[^"]+)"', re.S)


def _read(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def test_mobile_page_is_served_at_m(client: TestClient) -> None:
    page = client.get("/m")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "/static/mobile.js" in page.text and "/static/mobile.css" in page.text
    assert 'lang="ko"' in page.text and "width=device-width" in page.text
    # 다른 화면과 같은 캐시·보안 헤더 — 라우트가 아니라 미들웨어가 붙인다 (화면 주소 목록에 /m이 있다)
    assert page.headers["cache-control"] == "no-cache"
    assert "/m" in PAGE_PATHS and cache_control_for("/m") == "no-cache"
    assert "script-src 'self'" in page.headers["content-security-policy"]
    # 끝에 /가 붙어도 같은 화면으로 간다
    assert client.get("/m/").status_code == 200


def test_pc_pages_are_unchanged(client: TestClient) -> None:
    """`/`는 PC 웹 앱 그대로이고 모바일 파일을 가리키지 않는다. 다른 화면 주소도 그대로 열린다."""
    root = client.get("/")
    assert root.status_code == 200 and "/static/js/main.js" in root.text
    assert "mobile.js" not in root.text and "mobile.css" not in root.text
    for path in ("/admin", "/privacy", "/terms"):
        assert client.get(path).status_code == 200, path
    # 정적 파일 보호(점 파일·테스트 폴더)도 그대로다
    assert client.get("/static/tests/errors.test.js").status_code == 404
    assert client.get("/static/.env").status_code == 404


def test_mobile_assets_resolve(client: TestClient) -> None:
    html = client.get("/m").text
    refs = sorted(set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html)))
    assert refs == ["/static/mobile.css", "/static/mobile.js"]
    assert "text/css" in client.get("/static/mobile.css").headers["content-type"]
    assert "javascript" in client.get("/static/mobile.js").headers["content-type"]

    # mobile.js가 가져다 쓰는 PC 쪽 모듈: 파일이 열리고, 가져오는 이름이 그 파일에서 실제로 내보내진다
    imports = IMPORT_FROM.findall(_read("mobile.js"))
    assert imports, "mobile.js의 import를 찾지 못했다"
    for names, spec in imports:
        assert spec.startswith("./js/lib/"), spec  # 화면 부품·상태(js/ui, js/views, store)는 쓰지 않는다
        served = client.get("/static/" + spec.removeprefix("./"))
        assert served.status_code == 200, spec
        for name in (part.strip() for part in names.split(",")):
            if name:
                assert re.search(
                    rf"export\s+(?:async\s+)?(?:function|const|class)\s+{re.escape(name)}\b", served.text
                ), f"{spec}에 {name}이 없다"


def test_mobile_follows_the_screen_rules() -> None:
    html, script, css = (_read(name) for name in MOBILE_FILES)

    assert re.findall(r"<script\b[^>]*>", html) == ['<script type="module" src="/static/mobile.js">']
    assert "<style" not in html
    assert not re.search(r"\son[a-z]+\s*=", html)  # onclick= 같은 인라인 핸들러
    assert not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"https?://", html)  # 외부 자원(CDN·폰트·이미지) 없음
    # 화면 밖으로 나가는 링크는 같은 출처의 안내 문서뿐
    assert set(re.findall(r'href="([^"]+)"', html)) <= {
        "/favicon.ico",
        "/static/mobile.css",
        "/privacy",
        "/terms",
    }

    # 서버·사용자가 준 문자열을 HTML·코드로 해석하게 만드는 길이 없다
    assert not re.search(
        r"\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|document\.write|\beval\(|new Function\(|srcdoc",
        script,
    )
    # 외부 주소는 SVG 이름공간 하나뿐
    assert set(re.findall(r"https?://[^\s\"'`)]+", script)) == {"http://www.w3.org/2000/svg"}
    assert not re.search(r"https?://|@import|url\(", css)

    # 브라우저 저장소: localStorage는 `store` 한 곳에서만 쓰고(읽기·쓰기·지우기 한 줄씩),
    # 다른 저장소는 쓰지 않는다
    assert len(re.findall(r"localStorage\.", script)) == 3  # noqa: PLR2004
    assert not re.search(r"sessionStorage|indexedDB|document\.cookie", script)
    # 저장하는 것은 탭 이름과 상대 id뿐 — 토큰·초안·대화 본문을 넣는 호출이 없다
    assert set(re.findall(r'store\.(?:set|get|remove)\("([^"]+)"', script)) == {
        "tab",
        "lastContact",
        "recentContacts",
    }
    # 주소(URL)에 값을 싣지 않는다: 화면 이동 기록은 주소 없이 쌓고, auth_error는 읽은 뒤 주소에서 지운다
    assert re.findall(r"pushState\(([^)]*)\)", script) == ['{ depth: nav.stacks[tab].length }, ""']
    assert 'searchParams.delete("auth_error")' in script and "replaceState(" in script


def test_mobile_calls_only_existing_api_routes(client: TestClient) -> None:
    """`mobile.js`가 부르는 경로·메서드가 지금 앱에 있다. 서버 경로를 바꾸면 여기서 먼저 걸린다."""
    calls = sorted(set((method, path) for method, _, path in API_CALL.findall(_read("mobile.js"))))
    assert len(calls) >= 25, calls  # noqa: PLR2004 - 호출을 못 찾으면(작성 방식이 바뀌면) 이 테스트는 뜻이 없다
    # 앱의 API 경로표(OpenAPI): "/v1/contacts/{contact_id}/persona" → 정규식, 그 경로가 받는 메서드
    routes = [
        (
            re.compile("^" + re.sub(r"\\{[^/]+?\\}", "[^/]+", re.escape(path)) + "$"),
            {m.upper() for m in methods},
        )
        for path, methods in client.app.openapi()["paths"].items()  # type: ignore[attr-defined]
    ]
    assert any(regex.match("/v1/contacts/x/persona") for regex, _ in routes)  # 경로표를 제대로 읽었는지
    for method, template in calls:
        path = re.sub(r"\$\{[^}]*\}", "x", template)
        assert "$" not in path and "?" not in path, template  # 쿼리는 주소에 직접 쓰지 않는다 (query 옵션)
        assert any(regex.match(path) and method in methods for regex, methods in routes), (
            f"{method} {template} — 서버에 없는 경로"
        )

    # 새 인증·비동기 가져오기 계약을 실제로 쓰고 있다
    wanted = {
        ("GET", "/v1/auth/providers"),
        ("POST", "/v1/auth/signup"),
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/consent"),
        ("POST", "/v1/auth/oauth/${seg(provider)}/start"),
        ("POST", "/v1/imports/parse"),
        ("POST", "/v1/conversations/${seg(target.id)}/imports/raw"),
        ("GET", "/v1/imports/${seg(jobId)}"),
        ("GET", "/v1/contacts/${seg(contact.id)}/persona/suggestion"),
    }
    assert wanted <= set(calls), wanted - set(calls)
    # 서버에 없는(예전 브랜치의) 것을 부르지 않는다: 아이디 가입, 링크로 여는 소셜 시작
    script = _read("mobile.js")
    assert "/v1/auth/register" not in script and "start?next=" not in script
    # 첨삭은 Idempotency-Key와 함께, 모바일 채널로
    assert '"Idempotency-Key"' in script and 'channel: "mobile"' in script


def test_mobile_has_a_message_for_every_oauth_error_code() -> None:
    """소셜 로그인 콜백이 돌려보내는 `auth_error` 코드마다 화면 문구가 있다 (새 코드는 여기서 걸린다)."""
    script = _read("mobile.js")
    body = script[script.index("export function authErrorMessage") :]
    body = body[: body.index("\n}\n")]
    handled = set(re.findall(r'case "([a-z_]+)":', body))
    assert handled == {code for _, code in OAUTH_ERROR_CODES} | {"oauth_denied"}
    # 소셜 로그인은 /m에서 시작해 /m으로 돌아온다
    assert 'HOME_PATH = "/m"' in script
    assert safe_next("/m") == "/m"
    assert with_auth_error("/m", "oauth_denied") == "/m?auth_error=oauth_denied"


@pytest.fixture
def approval_client() -> TestClient:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, llm_provider="fake", embedding_provider="fake", auth_signup_requires_approval=True
    )
    return TestClient(create_app(settings))


def test_signup_pending_has_no_session(approval_client: TestClient) -> None:
    """승인제: 202는 '로그인됨'이 아니다 — 쿠키도 CSRF 토큰도 없고, 로그인은 403 account_pending."""
    client = approval_client
    policy = client.get("/v1/auth/providers").json()
    assert policy["requires_approval"] is True and policy["signup_open"] is True
    assert set(policy) == {
        "signup_open",
        "requires_approval",
        "invite_required",
        "require_consent",
        "google",
        "kakao",
    }

    r = client.post("/v1/auth/signup", json={"email": "wait@example.com", "password": "long-enough-pw"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending" and body["email"] == "wait@example.com" and "csrf_token" not in body
    assert "set-cookie" not in r.headers
    assert client.get("/v1/auth/me").status_code == 401

    login = client.post("/v1/auth/login", json={"username": "wait@example.com", "password": "long-enough-pw"})
    assert login.status_code == 403 and login.json()["code"] == "account_pending"


def test_signup_then_email_login_gives_the_fields_the_screen_reads(client: TestClient) -> None:
    """승인제가 아니면 201 + 세션. 화면이 읽는 필드(csrf_token·providers·consent_current 등)가 응답에 있다."""
    r = client.post(
        "/v1/auth/signup",
        json={"email": "Mina@Example.com", "password": "long-enough-pw", "display_name": "미나"},
    )
    assert r.status_code == 201
    out = r.json()
    assert out["email"] == "mina@example.com" and out["display_name"] == "미나" and out["csrf_token"]
    me = client.get("/v1/auth/me").json()
    assert me["providers"] == ["password"] and me["csrf_token"] == out["csrf_token"]
    assert {"user_id", "username", "is_admin", "consent_current", "consent_version", "status", "role"} <= set(
        me
    )

    # 쓰기 요청은 X-CSRF-Token이 있어야 한다 (화면은 로그인 응답·me에서 받은 값을 싣는다)
    contact = {"alias": "박지현", "relationship_type": "colleague", "use_default_persona": True}
    assert client.post("/v1/contacts", json=contact).status_code == 403
    made = client.post("/v1/contacts", json=contact, headers={"X-CSRF-Token": me["csrf_token"]})
    assert made.status_code == 201 and made.json()["persona_version"] == 1

    # 로그아웃 뒤 이메일로 다시 로그인된다 (대소문자 무관)
    assert client.post("/v1/auth/logout").status_code == 204
    again = client.post("/v1/auth/login", json={"username": "MINA@example.com", "password": "long-enough-pw"})
    assert again.status_code == 200 and again.json()["username"] == out["username"]
