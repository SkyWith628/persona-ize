"""회원가입·로그인·소셜 로그인·로그아웃·내 정보 (project-design §7 인증).

세션 토큰은 HttpOnly 쿠키 `pi_session`으로만 내려간다. 응답 본문의 `csrf_token`을 쓰기 요청의
`X-CSRF-Token` 헤더에 넣는다. `Secure`는 설정(`SESSION_COOKIE_SECURE`) — 로컬 http에서는 꺼 둔다.

소셜 로그인(인가 코드 방식): `start`가 무작위 `state`(+ Google은 PKCE verifier, 돌아갈 `next`)를 10분짜리
HttpOnly 쿠키 `pi_oauth`에 넣고 제공자로 보낸다. `callback`은 쿠키의 state와 쿼리의 state를 상수 시간으로
비교하고, 쿠키를 지운 뒤 세션을 연다. 실패는 저장해 둔 `next`에 `auth_error=<code>`를 붙여 돌려보낸다
(상태 쿠키가 없거나 깨졌으면 `/?auth_error=<code>`). 코드·토큰은 주소에 싣지 않는다.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import SESSION_COOKIE, AppState, State, current_session, require_csrf
from app.api.schemas import (
    AccountDeleteIn,
    AccountDeletionOut,
    AuthOut,
    Credentials,
    LoginIn,
    MeOut,
    PasswordChange,
    PasswordChangeOut,
    ProvidersOut,
    SignupIn,
)
from app.core.errors import AccountSuspendedError, DomainError, NotFoundError, SignupClosedError
from app.core.security import tokens_equal
from app.domain.auth import AuthResult
from app.services.oauth import OAuthError, OAuthProvider, new_pkce_verifier, pkce_challenge

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

OAUTH_COOKIE = "pi_oauth"
OAUTH_COOKIE_PATH = "/v1/auth/oauth"
OAUTH_COOKIE_SECONDS = 600
MAX_NEXT = 512


def _set_cookie(response: Response, result: AuthResult, state: State) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=result.session_token,
        httponly=True,
        secure=state.settings.session_cookie_secure,
        samesite="lax",
        max_age=state.settings.session_ttl_days * 24 * 3600,
        path="/",
    )


def _out(result: AuthResult, state: AppState) -> AuthOut:
    user = result.user
    return AuthOut(
        user_id=str(user.id),
        username=user.username,
        csrf_token=result.csrf_token,
        expires_at=result.expires_at,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        is_admin=state.auth_service.is_admin(user),
    )


@router.get("/providers", response_model=ProvidersOut, summary="가입 정책과 켜진 소셜 로그인")
async def providers(state: State) -> ProvidersOut:
    """로그인 없이 읽는다. google·kakao는 서버에 클라이언트 설정이 있을 때만 true."""
    return ProvidersOut(
        signup_open=state.settings.auth_allow_register,
        requires_approval=state.settings.auth_signup_requires_approval,
        google="google" in state.oauth_providers,
        kakao="kakao" in state.oauth_providers,
    )


@router.post("/signup", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def signup(body: SignupIn, response: Response, state: State) -> AuthOut:
    """이메일 가입. 닫혀 있으면 403 `signup_closed`, 이메일 중복 409. 승인제면 `status`가 pending."""
    result = await state.auth_service.signup(body.email, body.password, body.display_name)
    _set_cookie(response, result, state)
    return _out(result, state)


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def register(body: Credentials, response: Response, state: State) -> AuthOut:
    result = await state.auth_service.register(body.username, body.password)
    _set_cookie(response, result, state)
    return _out(result, state)


@router.post("/login", response_model=AuthOut)
async def login(body: LoginIn, request: Request, response: Response, state: State) -> AuthOut:
    """`username`에 아이디 또는 이메일. 정지 계정은 403 `account_suspended`."""
    client_ip = request.client.host if request.client else None
    result = await state.auth_service.login(body.username, body.password, client_ip)
    _set_cookie(response, result, state)
    return _out(result, state)


def safe_next(raw: str | None) -> str:
    """로그인 뒤 돌아갈 곳. `/`로 시작하는 같은 출처 경로만 — 아니면 `/` (오픈 리다이렉트 방지)."""
    if not raw or len(raw) > MAX_NEXT or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    if "\\" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):  # noqa: PLR2004
        return "/"
    return raw


def _redirect_uri(request: Request, state: AppState, provider: str) -> str:
    base = state.settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/v1/auth/oauth/{provider}/callback"


def _provider(state: AppState, name: str) -> OAuthProvider:
    provider = state.oauth_providers.get(name)
    if provider is None:
        raise NotFoundError("지원하지 않거나 설정되지 않은 로그인 방식입니다.")
    return provider


def _pack(data: dict[str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode("ascii")


def _unpack(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def _clear_oauth_cookie(response: Response) -> None:
    response.delete_cookie(OAUTH_COOKIE, path=OAUTH_COOKIE_PATH)


def with_auth_error(next_path: str, code: str) -> str:
    """돌아갈 경로에 `auth_error=<code>`를 붙인다 (기존 쿼리는 유지, #조각 앞에 넣는다)."""
    path, hash_mark, fragment = next_path.partition("#")
    joined = f"{path}{'&' if '?' in path else '?'}auth_error={code}"
    return f"{joined}{hash_mark}{fragment}"


def _fail(code: str, saved: dict[str, str] | None = None) -> RedirectResponse:
    """실패는 시작할 때 저장한 `next`로 돌려보낸다. 상태 쿠키가 없거나 깨졌으면 `/`."""
    target = with_auth_error(safe_next((saved or {}).get("n")), code)
    response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    _clear_oauth_cookie(response)
    return response


@router.get("/oauth/{provider}/start", summary="소셜 로그인 시작 (제공자 로그인으로 302)")
async def oauth_start(provider: str, request: Request, state: State, next: str | None = None) -> Response:  # noqa: A002
    """provider = google | kakao. 설정되지 않은 제공자는 404."""
    impl = _provider(state, provider)
    oauth_state = secrets.token_urlsafe(32)
    verifier = new_pkce_verifier() if impl.uses_pkce else ""
    url = impl.authorize_url(
        redirect_uri=_redirect_uri(request, state, provider),
        state=oauth_state,
        code_challenge=pkce_challenge(verifier) if verifier else None,
    )
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=OAUTH_COOKIE,
        value=_pack({"p": provider, "s": oauth_state, "n": safe_next(next), "v": verifier}),
        httponly=True,
        secure=state.settings.session_cookie_secure,
        samesite="lax",
        max_age=OAUTH_COOKIE_SECONDS,
        path=OAUTH_COOKIE_PATH,
    )
    return response


@router.get("/oauth/{provider}/callback", summary="소셜 로그인 콜백 (세션 쿠키 설정 뒤 302)")
async def oauth_callback(
    provider: str,
    request: Request,
    state: State,
    code: str | None = None,
    error: str | None = None,
) -> Response:
    """성공: 세션 쿠키 + 302 → next. 실패: 302 → `<next>?auth_error=<code>` (상태 쿠키가 없으면 `/`로)."""
    impl = _provider(state, provider)
    # `state`는 앱 상태 의존성 이름과 겹쳐 쿼리에서 직접 읽는다.
    returned_state = request.query_params.get("state")
    saved = _unpack(request.cookies.get(OAUTH_COOKIE))
    if saved.get("p") != provider:
        return _fail("oauth_failed")  # 쿠키 없음·만료·다른 브라우저·다른 제공자로 시작한 흐름
    if not tokens_equal(saved.get("s"), returned_state):
        return _fail("oauth_failed", saved)  # 위조된 요청이거나 state가 빠졌다
    if error:
        return _fail("oauth_denied", saved)
    if not code:
        return _fail("oauth_failed", saved)
    try:
        profile = await impl.fetch_profile(
            code=code,
            redirect_uri=_redirect_uri(request, state, provider),
            code_verifier=saved.get("v") or None,
        )
        result = await state.auth_service.login_with_identity(provider, profile)
    except OAuthError:
        return _fail("oauth_failed", saved)
    except SignupClosedError:
        return _fail("signup_closed", saved)
    except AccountSuspendedError:
        return _fail("account_suspended", saved)
    except DomainError as exc:
        log.warning("oauth login failed provider=%s code=%s", provider, exc.code)
        return _fail("oauth_failed", saved)
    response = RedirectResponse(safe_next(saved.get("n")), status_code=status.HTTP_302_FOUND)
    _clear_oauth_cookie(response)
    _set_cookie(response, result, state)
    return response


@router.post("/password", response_model=PasswordChangeOut)
async def change_password(body: PasswordChange, request: Request, state: State) -> PasswordChangeOut:
    """현재 비밀번호 확인 뒤 변경. 이 세션만 남기고 다른 세션은 모두 로그아웃된다. CSRF 필수."""
    session = await current_session(request, state)
    require_csrf(request, state, session)
    revoked = await state.auth_service.change_password(session, body.current_password, body.new_password)
    return PasswordChangeOut(revoked_sessions=revoked)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, state: State) -> None:
    """CSRF 검사 없이 받는다 — 로그아웃 강제는 피해가 없고, 쿠키가 없으면 아무 일도 없다."""
    await state.auth_service.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=MeOut)
async def me(request: Request, state: State) -> MeOut:
    """승인 대기(pending) 계정도 읽을 수 있다 — 화면이 `status`로 안내한다."""
    session = await current_session(request, state)
    user = await state.auth_service.me(session)
    return MeOut(
        user_id=str(user.id),
        username=user.username,
        csrf_token=session.csrf_token,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        is_admin=state.auth_service.is_admin(user),
        providers=await state.auth_service.providers_of(user),  # type: ignore[arg-type]
    )


@router.delete("/me", response_model=AccountDeletionOut)
async def delete_me(
    body: AccountDeleteIn, request: Request, response: Response, state: State
) -> AccountDeletionOut:
    """계정 삭제 (비밀번호 확인 + CSRF). 상대·대화·첨삭 이력·그래프·사용량·세션을 지운다. 되돌릴 수 없다.

    비밀번호가 없는 소셜 전용 계정은 `{"password": ""}`로 보낸다.
    """
    session = await current_session(request, state)
    require_csrf(request, state, session)
    d = await state.deletion_service.delete_account(session, body.password)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return AccountDeletionOut(
        contacts=d.contacts,
        conversations=d.conversations,
        messages=d.messages,
        graph_nodes_removed=d.graph_nodes_removed,
        graph_error=d.graph_error,
    )
