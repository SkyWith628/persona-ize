"""회원가입·로그인·로그아웃·내 정보·동의·세션 관리·토큰 발급 (project-design §7 인증, 계약 §4.2).

- 브라우저: 세션 토큰은 HttpOnly 쿠키 `pi_session`으로만 내려간다. 응답 본문의 `csrf_token`을 쓰기 요청의
  `X-CSRF-Token` 헤더에 넣는다. `Secure`는 설정(`SESSION_COOKIE_SECURE`) — 로컬 http에서는 꺼 둔다.
- 비브라우저(모바일 키보드·PC 프로그램): `POST /v1/auth/token`이 같은 세션 토큰을 본문으로 준다. 이후
  `Authorization: Bearer <토큰>`으로 모든 인증 경로를 쓰고 CSRF 헤더는 필요 없다.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response, status

from app.api.deps import SESSION_COOKIE, AppState, State, bearer_token, current_session, require_csrf
from app.api.schemas_auth import (
    AccountDeleteIn,
    AccountDeletionOut,
    AuthConfigOut,
    AuthOut,
    ConsentIn,
    Credentials,
    LogoutAllOut,
    MeOut,
    PasswordChange,
    PasswordChangeOut,
    RegisterIn,
    SessionOut,
    TokenIn,
    TokenOut,
)
from app.domain.auth import AuthResult, User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


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


@router.post("/login", response_model=AuthOut)
async def login(body: Credentials, request: Request, response: Response, state: State) -> AuthOut:
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
    return MeOut(**_account(state, user), csrf_token=session.csrf_token)


@router.post("/consent", response_model=MeOut)
async def consent(request: Request, state: State, body: ConsentIn | None = None) -> MeOut:
    """약관·개인정보 안내에 지금 버전으로 동의한다. 본문은 `{}`(생략 가능). CSRF 필수."""
    session = await current_session(request, state)
    require_csrf(request, state, session)
    user = await state.auth_service.record_consent(session)
    return MeOut(**_account(state, user), csrf_token=session.csrf_token)


@router.delete("/me", response_model=AccountDeletionOut)
async def delete_me(
    body: AccountDeleteIn, request: Request, response: Response, state: State
) -> AccountDeletionOut:
    """계정 삭제 (비밀번호 확인 + CSRF). 상대·대화·첨삭 이력·그래프·사용량·세션을 지운다. 되돌릴 수 없다."""
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
