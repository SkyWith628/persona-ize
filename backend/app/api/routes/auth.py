"""회원가입·로그인·로그아웃·내 정보·동의·세션 관리·토큰 발급 (project-design §7 인증, 계약 §4.2).

- 브라우저: 세션 토큰은 HttpOnly 쿠키 `pi_session`으로만 내려간다. 응답 본문의 `csrf_token`을 쓰기 요청의
  `X-CSRF-Token` 헤더에 넣는다. `Secure`는 설정(`SESSION_COOKIE_SECURE`) — 로컬 http에서는 꺼 둔다.
- 비브라우저(모바일 키보드·PC 프로그램): `POST /v1/auth/token`이 같은 세션 토큰을 본문으로 준다. 이후
  `Authorization: Bearer <토큰>`으로 모든 인증 경로를 쓰고 CSRF 헤더는 필요 없다.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import SESSION_COOKIE, AppState, State, bearer_token, current_session, require_csrf
from app.api.schemas_auth import (
    AccountDeleteIn,
    AccountDeletionOut,
    AuthConfigOut,
    AuthOut,
    ConsentIn,
    LoginIn,
    LogoutAllOut,
    MeOut,
    PasswordChange,
    PasswordChangeOut,
    RegisterIn,
    SessionOut,
    SignupEmailIn,
    TokenIn,
    TokenOut,
)
from app.core.errors import (
    AccountExistsError,
    AccountSuspendedError,
    DomainError,
    NotFoundError,
    SignupClosedError,
)
from app.core.security import tokens_equal
from app.domain.auth import AuthResult, User
from app.services.oauth import OAuthError, OAuthProvider, new_pkce_verifier, pkce_challenge

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

OAUTH_COOKIE = "pi_oauth"
OAUTH_COOKIE_PATH = "/v1/auth/oauth"
OAUTH_COOKIE_SECONDS = 600
MAX_NEXT = 512


def _set_cookie(response: Response, result: AuthResult, state: AppState) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=result.session_token,
        httponly=True,
        secure=state.settings.session_cookie_secure,
        samesite="lax",
        max_age=state.settings.session_ttl_days * 24 * 3600,
        path="/",
    )


def _clear_cookie(response: Response, state: AppState) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=state.settings.session_cookie_secure, httponly=True, samesite="lax"
    )


def _account(state: AppState, user: User) -> dict[str, object]:
    return {
        "user_id": str(user.id),
        "username": user.username,
        "is_admin": state.auth_service.is_admin(user),
        "consented_at": user.consented_at,
        "consent_version": user.consent_version,
        "consent_current": state.auth_service.consent_current(user),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
    }


def _auth_out(state: AppState, result: AuthResult) -> AuthOut:
    return AuthOut(**_account(state, result.user), csrf_token=result.csrf_token, expires_at=result.expires_at)


def _client_ip(request: Request) -> str | None:
    # 프록시 뒤에서는 uvicorn --proxy-headers가 채운 값이다. X-Forwarded-For를 직접 읽지 않는다.
    return request.client.host if request.client else None


@router.get("/config", response_model=AuthConfigOut)
async def auth_config(state: State) -> AuthConfigOut:
    """로그인 전 화면용 공개 설정 (인증 불필요). 초대 코드 값 자체는 내보내지 않는다."""
    s = state.settings
    return AuthConfigOut(
        allow_register=s.auth_allow_register,
        invite_required=bool(s.register_invite_code.strip()),
        require_consent=s.require_consent,
        consent_version=s.consent_version,
        app_env=s.app_env,
        requires_approval=s.auth_signup_requires_approval,
        google="google" in state.oauth_providers,
        kakao="kakao" in state.oauth_providers,
    )


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, request: Request, response: Response, state: State) -> AuthOut:
    """가입. 초대 코드가 틀리면 403 `invalid_invite`, 동의가 필요한데 없으면 422 `consent_required`."""
    result = await state.auth_service.signup(
        body.username,
        body.password,
        invite_code=body.invite_code,
        consent=bool(body.consent),
        client_ip=_client_ip(request),
    )
    _set_cookie(response, result, state)
    return _auth_out(state, result)


@router.post("/signup", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def signup_email(body: SignupEmailIn, request: Request, response: Response, state: State) -> AuthOut:
    """이메일 가입. 닫혀 있으면 403 `signup_closed`, 이메일 중복 409. 승인제면 `status`가 pending."""
    result = await state.auth_service.signup_email(
        body.email,
        body.password,
        body.display_name,
        invite_code=body.invite_code,
        consent=bool(body.consent),
        client_ip=_client_ip(request),
    )
    _set_cookie(response, result, state)
    return _auth_out(state, result)


@router.post("/login", response_model=AuthOut)
async def login(body: LoginIn, request: Request, response: Response, state: State) -> AuthOut:
    """`username`에 아이디 또는 이메일. 정지 계정은 403 `account_suspended`."""
    result = await state.auth_service.login(body.username, body.password, _client_ip(request))
    _set_cookie(response, result, state)
    return _auth_out(state, result)


@router.post("/token", response_model=TokenOut)
async def issue_token(body: TokenIn, request: Request, state: State) -> TokenOut:
    """비브라우저 클라이언트용 로그인. 쿠키를 내리지 않고 토큰을 본문으로 준다 (로그인 시도 제한 동일)."""
    result = await state.auth_service.login(body.username, body.password, _client_ip(request))
    log.info("bearer token issued client=%s", body.client)  # 누구의 토큰인지는 남기지 않는다
    return TokenOut(
        access_token=result.session_token,
        expires_at=result.expires_at,
        user_id=str(result.user.id),
        username=result.user.username,
    )


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
    """provider = google | kakao. 설정되지 않은 제공자는 404.

    무작위 `state`(+ Google은 PKCE verifier, 돌아갈 `next`)를 10분짜리 HttpOnly 쿠키 `pi_oauth`에 넣는다.
    """
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
    """성공: 세션 쿠키 + 302 → next. 실패: 302 → `<next>?auth_error=<code>` (상태 쿠키가 없으면 `/`로).

    쿠키의 state와 쿼리의 state를 상수 시간으로 비교한다. 코드·토큰은 주소에 싣지 않는다.
    """
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
    except AccountExistsError:
        return _fail("account_exists", saved)  # 같은 이메일의 비밀번호 계정이 있다
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
    """쿠키의 세션과 Bearer 토큰을 폐기한다.

    CSRF 검사 없이 받는다 — 로그아웃 강제는 피해가 없고, 쿠키도 토큰도 없으면 아무 일도 없다.
    """
    await state.auth_service.logout(request.cookies.get(SESSION_COOKIE))
    await state.auth_service.logout(bearer_token(request))
    _clear_cookie(response, state)


@router.post("/logout-all", response_model=LogoutAllOut)
async def logout_all(request: Request, state: State) -> LogoutAllOut:
    """지금 세션만 남기고 다른 기기·토큰을 모두 로그아웃한다. CSRF 필수."""
    session = await current_session(request, state)
    require_csrf(request, state, session)
    return LogoutAllOut(revoked_sessions=await state.auth_service.logout_all(session))


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(request: Request, state: State) -> list[SessionOut]:
    """내 활성 세션(기기·토큰) 목록. 최근에 만든 것 먼저."""
    session = await current_session(request, state)
    return [
        SessionOut(
            id=s.public_id,
            created_at=s.created_at,
            expires_at=s.expires_at,
            current=s.token_hash == session.token_hash,
        )
        for s in await state.auth_service.list_sessions(session)
    ]


@router.get("/me", response_model=MeOut)
async def me(request: Request, state: State) -> MeOut:
    session = await current_session(request, state)
    user = await state.auth_service.me(session)
    return MeOut(
        **_account(state, user),
        csrf_token=session.csrf_token,
        providers=await state.auth_service.providers_of(user),  # type: ignore[arg-type]
    )


@router.post("/consent", response_model=MeOut)
async def consent(request: Request, state: State, body: ConsentIn | None = None) -> MeOut:
    """약관·개인정보 안내에 지금 버전으로 동의한다. 본문은 `{}`(생략 가능). CSRF 필수."""
    session = await current_session(request, state)
    require_csrf(request, state, session)
    user = await state.auth_service.record_consent(session)
    return MeOut(
        **_account(state, user),
        csrf_token=session.csrf_token,
        # 빼면 기본값 []가 내려가 화면이 비밀번호 계정을 소셜 전용으로 오해한다
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
    _clear_cookie(response, state)
    return AccountDeletionOut(
        contacts=d.contacts,
        conversations=d.conversations,
        messages=d.messages,
        graph_nodes_removed=d.graph_nodes_removed,
        graph_error=d.graph_error,
    )
