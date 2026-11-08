"""회원가입·로그인·로그아웃·내 정보 (project-design §7 인증).

세션 토큰은 HttpOnly 쿠키 `pi_session`으로만 내려간다. 응답 본문의 `csrf_token`을 쓰기 요청의
`X-CSRF-Token` 헤더에 넣는다. `Secure`는 설정(`SESSION_COOKIE_SECURE`) — 로컬 http에서는 꺼 둔다.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.deps import SESSION_COOKIE, State, current_session
from app.api.schemas import (
    AccountDeleteIn,
    AccountDeletionOut,
    AuthOut,
    Credentials,
    MeOut,
    PasswordChange,
    PasswordChangeOut,
)
from app.domain.auth import AuthResult
from app.services.auth_service import ForbiddenError

router = APIRouter(prefix="/v1/auth", tags=["auth"])


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


def _out(result: AuthResult) -> AuthOut:
    return AuthOut(
        user_id=str(result.user.id),
        username=result.user.username,
        csrf_token=result.csrf_token,
        expires_at=result.expires_at,
    )


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def register(body: Credentials, response: Response, state: State) -> AuthOut:
    result = await state.auth_service.register(body.username, body.password)
    _set_cookie(response, result, state)
    return _out(result)


@router.post("/login", response_model=AuthOut)
async def login(body: Credentials, request: Request, response: Response, state: State) -> AuthOut:
    client_ip = request.client.host if request.client else None
    result = await state.auth_service.login(body.username, body.password, client_ip)
    _set_cookie(response, result, state)
    return _out(result)


@router.post("/password", response_model=PasswordChangeOut)
async def change_password(body: PasswordChange, request: Request, state: State) -> PasswordChangeOut:
    """현재 비밀번호 확인 뒤 변경. 이 세션만 남기고 다른 세션은 모두 로그아웃된다. CSRF 필수."""
    session = await current_session(request, state)
    if not state.auth_service.csrf_ok(session, request.headers.get("X-CSRF-Token")):
        raise ForbiddenError("CSRF 토큰이 없거나 맞지 않습니다 (X-CSRF-Token).")
    revoked = await state.auth_service.change_password(session, body.current_password, body.new_password)
    return PasswordChangeOut(revoked_sessions=revoked)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, state: State) -> None:
    """CSRF 검사 없이 받는다 — 로그아웃 강제는 피해가 없고, 쿠키가 없으면 아무 일도 없다."""
    await state.auth_service.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=MeOut)
async def me(request: Request, state: State) -> MeOut:
    session = await current_session(request, state)
    user = await state.auth_service.me(session)
    return MeOut(user_id=str(user.id), username=user.username, csrf_token=session.csrf_token)


@router.delete("/me", response_model=AccountDeletionOut)
async def delete_me(
    body: AccountDeleteIn, request: Request, response: Response, state: State
) -> AccountDeletionOut:
    """계정 삭제 (비밀번호 확인 + CSRF). 상대·대화·첨삭 이력·그래프·사용량·세션을 지운다. 되돌릴 수 없다."""
    session = await current_session(request, state)
    if not state.auth_service.csrf_ok(session, request.headers.get("X-CSRF-Token")):
        raise ForbiddenError("CSRF 토큰이 없거나 맞지 않습니다 (X-CSRF-Token).")
    d = await state.deletion_service.delete_account(session, body.password)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return AccountDeletionOut(
        contacts=d.contacts,
        conversations=d.conversations,
        messages=d.messages,
        graph_nodes_removed=d.graph_nodes_removed,
        graph_error=d.graph_error,
    )
