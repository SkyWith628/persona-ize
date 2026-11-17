"""회원가입·로그인·소셜 로그인·로그아웃·내 정보·동의·세션 관리·토큰 발급 (project-design §7 인증, 계약 §4.2).

- 브라우저: 세션 토큰은 HttpOnly 쿠키 `pi_session`으로만 내려간다. 응답 본문의 `csrf_token`을 쓰기 요청의
  `X-CSRF-Token` 헤더에 넣는다. `Secure`는 설정(`SESSION_COOKIE_SECURE`) — 로컬 http에서는 꺼 둔다.
- 비브라우저(모바일 키보드·PC 프로그램): `POST /v1/auth/token`이 같은 세션 토큰을 본문으로 준다. 이후
  `Authorization: Bearer <토큰>`으로 모든 인증 경로를 쓰고 CSRF 헤더는 필요 없다.
- 승인제(`AUTH_SIGNUP_REQUIRES_APPROVAL`): 가입 응답이 201(세션) 대신 202 `{"status": "pending", …}`이고
  쿠키를 내리지 않는다. 승인 전 로그인은 403 `account_pending`.

소셜 로그인 (2026-11-16, 인가 코드 방식 — `services/oauth.py`):
- `start`가 일회용 `state`를 만들어 서버(`AppState.oauth_states`)에 PKCE verifier·돌아갈 경로·가입 조건과 함께
  보관하고, 같은 값을 10분짜리 HttpOnly 쿠키 `pi_oauth`(Path=/v1/auth/oauth, SameSite=Lax)로 브라우저에
  묶는다.
- `callback`은 ① 쿠키의 state와 쿼리의 state를 상수 시간으로 비교하고(다른 브라우저가 만든 링크·위조 요청은
  여기서 끝난다 — 이때는 보관분을 건드리지 않는다) ② 보관분을 꺼내며 지운다(같은 state는 두 번 못 쓴다).
- 로그인 뒤에는 시작할 때 받아 **검사해 둔** 같은 출처 경로(`safe_next`)로만 돌아간다. 실패는 그 경로에
  `auth_error=<코드>`를 붙여 돌려보낸다(state를 확인하지 못했으면 `/`). 코드는 우리가 정한 값뿐이다 —
  제공자가 준 `error`·`error_description`은 응답에 싣지 않는다. 인가 코드·토큰은 주소에 싣지 않는다.
- 설정되지 않은 제공자의 경로는 404다 (아무것도 설정하지 않으면 소셜 로그인은 없는 기능이다).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

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
    OAuthStartIn,
    OAuthStartOut,
    PasswordChange,
    PasswordChangeOut,
    ProvidersOut,
    RegisterIn,
    SessionOut,
    SignupIn,
    SignupPendingOut,
    TokenIn,
    TokenOut,
)
from app.core.errors import (
    AccountPendingError,
    AccountSuspendedError,
    ConsentRequiredError,
    DomainError,
    EmailInUseError,
    InvalidInviteError,
    NotFoundError,
    SignupClosedError,
    TooManyAttemptsError,
)
from app.core.ratelimit import client_key
from app.core.security import tokens_equal
from app.domain.auth import AuthResult, PendingSignup, User
from app.services.oauth import (
    MAX_CODE_CHARS,
    OAUTH_STATE_TTL_SECONDS,
    OAuthError,
    OAuthProvider,
    OAuthStateStoreFullError,
    PendingLogin,
    new_pkce_verifier,
    pkce_challenge,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

OAUTH_COOKIE = "pi_oauth"
OAUTH_COOKIE_PATH = "/v1/auth/oauth"
MAX_NEXT = 512
DEFAULT_NEXT = "/"
# 소셜 로그인이 실패했을 때 화면으로 돌려보내는 코드 (`?auth_error=<코드>`). 여기 없는 실패는 oauth_failed.
OAUTH_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (OAuthError, "oauth_failed"),
    (SignupClosedError, "signup_closed"),
    (InvalidInviteError, "invalid_invite"),
    (ConsentRequiredError, "consent_required"),
    (AccountPendingError, "account_pending"),
    (AccountSuspendedError, "account_suspended"),
    (EmailInUseError, "email_in_use"),
)


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


def _signed_up(state: AppState, response: Response, result: AuthResult | PendingSignup) -> AuthOut | Response:
    """가입 응답: 세션이 열렸으면 201 + 쿠키, 승인 대기면 202(쿠키 없음)."""
    if isinstance(result, PendingSignup):
        pending = SignupPendingOut(username=result.user.username, email=result.user.email)
        return JSONResponse(pending.model_dump(), status_code=status.HTTP_202_ACCEPTED)
    _set_cookie(response, result, state)
    return _auth_out(state, result)


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
    )


@router.get("/providers", response_model=ProvidersOut, summary="가입 정책과 켜진 소셜 로그인")
async def providers(state: State) -> ProvidersOut:
    """로그인 없이 읽는다. google·kakao는 서버에 클라이언트 ID와 시크릿이 모두 있을 때만 true."""
    s = state.settings
    return ProvidersOut(
        signup_open=s.auth_allow_register,
        requires_approval=s.auth_signup_requires_approval,
        invite_required=bool(s.register_invite_code.strip()),
        require_consent=s.require_consent,
        google="google" in state.oauth_providers,
        kakao="kakao" in state.oauth_providers,
    )


@router.post(
    "/register",
    response_model=AuthOut,
    status_code=status.HTTP_201_CREATED,
    responses={202: {"model": SignupPendingOut, "description": "승인제 — 계정은 만들어졌고 승인을 기다린다"}},
)
async def register(
    body: RegisterIn, request: Request, response: Response, state: State
) -> AuthOut | Response:
    """아이디 가입. 초대 코드가 틀리면 403 `invalid_invite`, 동의가 필요한데 없으면 422 `consent_required`.

    승인제 서버에서는 202 `{"status": "pending"}`(쿠키 없음)이다.
    """
    result = await state.auth_service.signup(
        body.username,
        body.password,
        invite_code=body.invite_code,
        consent=bool(body.consent),
        client_ip=_client_ip(request),
    )
    return _signed_up(state, response, result)


@router.post(
    "/signup",
    response_model=AuthOut,
    status_code=status.HTTP_201_CREATED,
    responses={202: {"model": SignupPendingOut, "description": "승인제 — 계정은 만들어졌고 승인을 기다린다"}},
)
async def signup(body: SignupIn, request: Request, response: Response, state: State) -> AuthOut | Response:
    """이메일 가입. 가입 정책(닫힘·초대 코드·동의·승인제)은 아이디 가입과 같다. 이메일 중복은 409.

    아이디는 서버가 이메일 앞부분에서 만든다(응답의 `username`). 로그인은 이메일·아이디 어느 쪽으로도 된다.
    """
    result = await state.auth_service.signup_email(
        body.email,
        body.password,
        display_name=body.display_name,
        invite_code=body.invite_code,
        consent=bool(body.consent),
        client_ip=_client_ip(request),
    )
    return _signed_up(state, response, result)


@router.post("/login", response_model=AuthOut)
async def login(body: LoginIn, request: Request, response: Response, state: State) -> AuthOut:
    """`username`에 아이디 또는 이메일. 비밀번호가 맞은 뒤에만 403 `account_pending`·`account_suspended`."""
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


# ---------- 소셜 로그인 ----------


def safe_next(raw: str | None) -> str:
    """로그인 뒤 돌아갈 곳. `/`로 시작하는 같은 출처 경로만 — 아니면 `/` (오픈 리다이렉트 방지).

    `//host`(스킴 없는 다른 출처), 역슬래시(브라우저가 `/`로 읽는다), 제어 문자, 너무 긴 값을 버린다.
    """
    if not raw or len(raw) > MAX_NEXT or not raw.startswith("/") or raw.startswith("//"):
        return DEFAULT_NEXT
    if "\\" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):  # noqa: PLR2004
        return DEFAULT_NEXT
    return raw


def with_auth_error(next_path: str, code: str) -> str:
    """돌아갈 경로에 `auth_error=<code>`를 붙인다 (기존 쿼리는 유지, #조각 앞에 넣는다)."""
    path, hash_mark, fragment = next_path.partition("#")
    joined = f"{path}{'&' if '?' in path else '?'}auth_error={code}"
    return f"{joined}{hash_mark}{fragment}"


def _redirect_uri(request: Request, state: AppState, provider: str) -> str:
    # 운영 모드는 PUBLIC_BASE_URL(https) 없이 소셜 로그인을 켜면 시작하지 않는다 (core/guard).
    base = state.settings.public_base_url.strip().rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/v1/auth/oauth/{provider}/callback"


def _provider(state: AppState, name: str) -> OAuthProvider:
    provider = state.oauth_providers.get(name)
    if provider is None:
        raise NotFoundError("지원하지 않거나 설정되지 않은 로그인 방식입니다.")
    return provider


def _clear_oauth_cookie(response: Response, state: AppState) -> None:
    response.delete_cookie(
        OAUTH_COOKIE,
        path=OAUTH_COOKIE_PATH,
        secure=state.settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _begin(
    request: Request,
    state: AppState,
    response: Response,
    provider: str,
    *,
    next_path: str | None,
    consent: bool,
    invite_ok: bool,
) -> str:
    """진행 중 로그인을 보관하고 state 쿠키를 심는다. 제공자 로그인 주소를 돌려준다.

    보관소가 가득 차 있으면 `OAuthStateStoreFullError`가 올라간다 (쿠키를 심기 전이다) — 부르는 쪽이
    POST는 429, GET은 `auth_error=oauth_failed`로 바꾼다.
    """
    impl = _provider(state, provider)
    verifier = new_pkce_verifier() if impl.uses_pkce else None
    oauth_state = state.oauth_states.issue(
        PendingLogin(
            provider=provider,
            next_path=safe_next(next_path),
            code_verifier=verifier,
            consent=consent,
            invite_ok=invite_ok,
        ),
        # 요청 제한(`core/ratelimit.py`)·로그인 실패 제한과 같은 클라이언트 키로 자리 수를 센다.
        client=client_key(_client_ip(request)),
    )
    response.set_cookie(
        key=OAUTH_COOKIE,
        value=oauth_state,
        httponly=True,
        secure=state.settings.session_cookie_secure,
        samesite="lax",  # 제공자에서 돌아오는 최상위 GET에는 실려야 한다 (Strict면 콜백에 쿠키가 없다)
        max_age=OAUTH_STATE_TTL_SECONDS,
        path=OAUTH_COOKIE_PATH,
    )
    return impl.authorize_url(
        redirect_uri=_redirect_uri(request, state, provider),
        state=oauth_state,
        code_challenge=pkce_challenge(verifier) if verifier else None,
    )


@router.get("/oauth/{provider}/start", summary="소셜 로그인 시작 (제공자 로그인으로 302)")
async def oauth_start(provider: str, request: Request, state: State, next: str | None = None) -> Response:  # noqa: A002
    """provider = google | kakao. 설정되지 않은 제공자는 404. 링크로 바로 여는 길이다.

    이미 가입한 사용자의 로그인에는 이것으로 충분하다. 새 계정에 동의·초대 코드가 필요한 서버에서는
    `POST`로 시작해 그 값을 함께 보낸다 (이 길로 온 새 사용자는 `auth_error=consent_required`나
    `invalid_invite`로 돌아간다). 진행 중인 소셜 로그인 보관소가 가득 차 있으면 제공자로 보내지 않고
    `<next>?auth_error=oauth_failed`로 돌려보낸다.
    """
    _provider(state, provider)  # 쿠키를 심기 전에 404부터
    response = Response(status_code=status.HTTP_302_FOUND)
    invite_ok = not state.auth_service.invite_required
    # 제공자 주소는 우리가 만든 것(고정 주소 + urlencode한 값)이라 그대로 Location에 넣는다.
    try:
        response.headers["location"] = _begin(
            request, state, response, provider, next_path=next, consent=False, invite_ok=invite_ok
        )
    except OAuthStateStoreFullError:
        # 링크로 연 화면이라 JSON 오류 대신 콜백의 실패와 같은 길로 돌려보낸다 (검사한 같은 출처 경로로).
        # state 쿠키는 지우지 않는다 — 이 브라우저가 다른 탭에서 진행 중인 로그인이 있을 수 있다.
        log.warning("oauth state store full provider=%s", provider)
        return RedirectResponse(
            with_auth_error(safe_next(next), "oauth_failed"), status_code=status.HTTP_302_FOUND
        )
    return response


@router.post(
    "/oauth/{provider}/start", response_model=OAuthStartOut, summary="소셜 로그인 시작 (주소를 본문으로)"
)
async def oauth_start_with_terms(
    provider: str, request: Request, response: Response, state: State, body: OAuthStartIn | None = None
) -> OAuthStartOut:
    """동의·초대 코드를 함께 보내는 시작. 응답의 `authorize_url`로 브라우저를 보낸다.

    초대 코드가 필요한 서버에서 코드를 보냈는데 틀리면 여기서 403 `invalid_invite`(가입과 같은 시도 제한).
    진행 중인 소셜 로그인 보관소가 가득 차 있으면 429 `too_many_attempts`(`Retry-After`).
    코드를 보내지 않으면 통과시키되, 콜백에서 새 계정을 만들어야 할 때 `auth_error=invalid_invite`가 된다 —
    이미 가입한 사용자는 초대 코드 없이 로그인된다. 세션이 없는 요청이라 CSRF 토큰은 없다(로그인과 같다).
    """
    body = body or OAuthStartIn()
    _provider(state, provider)
    service = state.auth_service
    invite_ok = not service.invite_required
    # 가입이 닫혀 있으면 초대 코드가 맞는지조차 알려 주지 않는다 (아이디 가입과 같은 순서).
    if service.invite_required and body.invite_code and state.settings.auth_allow_register:
        service.check_invite(body.invite_code, f"oauth:{provider}", _client_ip(request))
        invite_ok = True
    try:
        url = _begin(
            request,
            state,
            response,
            provider,
            next_path=body.next,
            consent=bool(body.consent),
            invite_ok=invite_ok,
        )
    except OAuthStateStoreFullError as exc:
        log.warning("oauth state store full provider=%s", provider)
        raise TooManyAttemptsError(
            "지금 진행 중인 소셜 로그인이 너무 많습니다. 잠시 뒤에 다시 시도하세요.",
            retry_after=exc.retry_after,
        ) from None
    return OAuthStartOut(authorize_url=url)


def _oauth_failed(state: AppState, code: str, next_path: str = DEFAULT_NEXT) -> RedirectResponse:
    response = RedirectResponse(with_auth_error(next_path, code), status_code=status.HTTP_302_FOUND)
    _clear_oauth_cookie(response, state)
    return response


@router.get("/oauth/{provider}/callback", summary="소셜 로그인 콜백 (세션 쿠키 설정 뒤 302)")
async def oauth_callback(
    provider: str, request: Request, state: State, code: str | None = None, error: str | None = None
) -> Response:
    """성공: 세션 쿠키 + 302 → 시작할 때의 next. 실패: 302 → `<next>?auth_error=<code>`.

    `auth_error` 값: `oauth_failed`(state 불일치·만료·재사용, 교환 실패) · `oauth_denied`(사용자가 취소) ·
    `signup_closed` · `invalid_invite` · `consent_required` · `account_pending` · `account_suspended` ·
    `email_in_use`. state를 확인하지 못한 실패는 돌아갈 곳을 믿을 수 없으므로 `/`로 보낸다.
    """
    impl = _provider(state, provider)
    # `state`는 앱 상태 의존성 이름과 겹쳐 쿼리에서 직접 읽는다.
    returned_state = request.query_params.get("state")
    # ① 이 브라우저가 시작한 흐름인가 — 쿠키와 쿼리의 state가 같아야 한다. 다르면 보관분은 건드리지 않는다.
    if not tokens_equal(request.cookies.get(OAUTH_COOKIE), returned_state):
        return _oauth_failed(state, "oauth_failed")
    # ② 서버가 내준 state인가 — 꺼내면서 지운다(일회용). 만료됐거나 이미 썼으면 None.
    pending = state.oauth_states.consume(returned_state)
    if pending is None or pending.provider != provider:
        return _oauth_failed(state, "oauth_failed")
    if error:  # 제공자가 준 문장은 응답에 싣지 않는다
        return _oauth_failed(state, "oauth_denied", pending.next_path)
    if not code or len(code) > MAX_CODE_CHARS:
        return _oauth_failed(state, "oauth_failed", pending.next_path)
    try:
        profile = await impl.fetch_profile(
            code=code,
            redirect_uri=_redirect_uri(request, state, provider),
            code_verifier=pending.code_verifier,
        )
        result = await state.auth_service.login_with_identity(
            provider, profile, consent=pending.consent, invite_ok=pending.invite_ok
        )
    except (OAuthError, DomainError) as exc:
        failure = next((c for kind, c in OAUTH_ERROR_CODES if isinstance(exc, kind)), None)
        if failure is None:
            # 예외 문장은 남기지 않는다 (종류만)
            log.warning("oauth login failed provider=%s kind=%s", provider, type(exc).__name__)
            failure = "oauth_failed"
        return _oauth_failed(state, failure, pending.next_path)
    response = RedirectResponse(pending.next_path, status_code=status.HTTP_302_FOUND)
    _clear_oauth_cookie(response, state)
    _set_cookie(response, result, state)
    return response


# ---------- 세션·계정 ----------


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
        providers=await state.auth_service.providers_of(user),
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
        providers=await state.auth_service.providers_of(user),
    )


@router.delete("/me", response_model=AccountDeletionOut)
async def delete_me(
    body: AccountDeleteIn, request: Request, response: Response, state: State
) -> AccountDeletionOut:
    """계정 삭제 (비밀번호 확인 + CSRF). 상대·대화·첨삭 이력·그래프·사용량·세션을 지운다. 되돌릴 수 없다.

    비밀번호가 없는 소셜 전용 계정은 `{"password": ""}`(세션과 CSRF만으로 지운다).
    하나뿐인 활성 관리자는 탈퇴할 수 없다 — 409 `last_admin`(아무것도 지우지 않는다). 다른 관리자를 먼저
    지정한 뒤 다시 한다.
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
