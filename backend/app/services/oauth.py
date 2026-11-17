"""소셜 로그인 제공자 (OAuth 2.0 인가 코드 방식, 서버에서 토큰 교환).

제공자 호출은 `OAuthProvider` Protocol 뒤에 둔다 — 테스트는 가짜 제공자를 끼워 네트워크 없이 돈다.
주소·파라미터는 2026-11-16에 공식 문서로 확인했다.
- Google: OpenID Connect discovery(`accounts.google.com/.well-known/openid-configuration`)의
  authorization/token/userinfo 주소, PKCE S256 지원. userinfo의 `sub`·`email`·`email_verified`·`name`.
- Kakao: `kauth.kakao.com/oauth/authorize`·`/oauth/token`, `kapi.kakao.com/v2/user/me`. 이메일은 동의 항목을
  켠 앱에서만 오고(없을 수 있다) `is_email_valid`·`is_email_verified`가 모두 참일 때만 확인된 것으로 본다.
  닉네임은 `kakao_account.profile.nickname`. 클라이언트 시크릿은 콘솔에서 켠 경우에만 보낸다.

토큰·코드·응답 본문은 로그에 남기지 않는다 (상태 코드만).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.domain.auth import MAX_DISPLAY_NAME, MAX_EMAIL

log = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - 주소
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
KAKAO_AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"  # noqa: S105 - 주소
KAKAO_ME_URL = "https://kapi.kakao.com/v2/user/me"

HTTP_TIMEOUT_SECONDS = 10.0
MAX_SUBJECT = 128
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OAuthError(Exception):
    """제공자와의 교환이 실패했다. 메시지에는 토큰·코드를 넣지 않는다."""


@dataclass(frozen=True)
class OAuthProfile:
    """제공자가 확인해 준 사용자. email은 소문자, 형식이 이상하면 None."""

    subject: str
    email: str | None = None
    email_verified: bool = False
    name: str | None = None


class OAuthProvider(Protocol):
    name: str
    uses_pkce: bool

    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str | None) -> str:
        """사용자를 보낼 제공자 로그인 주소."""
        ...

    async def fetch_profile(self, *, code: str, redirect_uri: str, code_verifier: str | None) -> OAuthProfile:
        """인가 코드를 토큰으로 바꾸고 사용자 정보를 읽는다. 실패하면 OAuthError."""
        ...


def new_pkce_verifier() -> str:
    return secrets.token_urlsafe(48)


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256: BASE64URL(SHA256(verifier)), 패딩 없음."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def clean_email(value: Any) -> str | None:  # noqa: ANN401 - 제공자 응답 값
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or len(email) > MAX_EMAIL or not _EMAIL_RE.fullmatch(email):
        return None
    return email


def clean_name(value: Any) -> str | None:  # noqa: ANN401
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())[:MAX_DISPLAY_NAME]
    return name or None


def _subject(value: Any) -> str:  # noqa: ANN401
    subject = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not subject or len(subject) > MAX_SUBJECT:
        raise OAuthError("제공자가 사용자 식별자를 주지 않았습니다.")
    return subject


async def _exchange(provider: str, token_url: str, form: dict[str, str], userinfo_url: str) -> dict[str, Any]:
    """코드 → 액세스 토큰 → 사용자 정보(JSON 객체). 토큰은 이 함수 밖으로 나가지 않는다."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            token_response = await client.post(token_url, data=form, headers={"Accept": "application/json"})
            if token_response.status_code != httpx.codes.OK:
                log.warning(
                    "oauth token exchange failed provider=%s status=%s", provider, token_response.status_code
                )
                raise OAuthError("토큰 교환에 실패했습니다.")
            payload = token_response.json()
            if not isinstance(payload, dict):  # 배열·문자열이 오면 .get에서 죽는다
                raise OAuthError("토큰 응답을 읽지 못했습니다.")
            access_token = payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise OAuthError("토큰 응답에 access_token이 없습니다.")
            info_response = await client.get(
                userinfo_url, headers={"Authorization": f"Bearer {access_token}"}
            )
            if info_response.status_code != httpx.codes.OK:
                log.warning(
                    "oauth userinfo failed provider=%s status=%s", provider, info_response.status_code
                )
                raise OAuthError("사용자 정보를 읽지 못했습니다.")
            info = info_response.json()
    except httpx.HTTPError as exc:
        log.warning("oauth http error provider=%s type=%s", provider, type(exc).__name__)
        raise OAuthError("제공자에 연결하지 못했습니다.") from exc
    except ValueError as exc:  # JSON 아님
        raise OAuthError("제공자 응답을 읽지 못했습니다.") from exc
    if not isinstance(info, dict):
        raise OAuthError("제공자 응답을 읽지 못했습니다.")
    return info


class GoogleProvider:
    name = "google"
    uses_pkce = True

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str | None) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
        }
        if code_challenge:
            params |= {"code_challenge": code_challenge, "code_challenge_method": "S256"}
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def fetch_profile(self, *, code: str, redirect_uri: str, code_verifier: str | None) -> OAuthProfile:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            form["code_verifier"] = code_verifier
        info = await _exchange(self.name, GOOGLE_TOKEN_URL, form, GOOGLE_USERINFO_URL)
        return OAuthProfile(
            subject=_subject(info.get("sub")),
            email=clean_email(info.get("email")),
            email_verified=info.get("email_verified") is True,
            name=clean_name(info.get("name")),
        )


class KakaoProvider:
    name = "kakao"
    uses_pkce = False  # 카카오 REST API 문서에 PKCE 파라미터가 없다

    def __init__(self, client_id: str, client_secret: str = "") -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str | None) -> str:
        # scope는 보내지 않는다 — 콘솔의 동의 항목 설정을 따른다 (꺼 둔 항목을 요청하면 오류가 난다).
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
        return f"{KAKAO_AUTH_URL}?{urlencode(params)}"

    async def fetch_profile(self, *, code: str, redirect_uri: str, code_verifier: str | None) -> OAuthProfile:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
        }
        if self._client_secret:
            form["client_secret"] = self._client_secret
        info = await _exchange(self.name, KAKAO_TOKEN_URL, form, KAKAO_ME_URL)
        account = info.get("kakao_account")
        account = account if isinstance(account, dict) else {}
        profile = account.get("profile")
        profile = profile if isinstance(profile, dict) else {}
        return OAuthProfile(
            subject=_subject(info.get("id")),
            email=clean_email(account.get("email")),
            email_verified=account.get("is_email_valid") is True and account.get("is_email_verified") is True,
            name=clean_name(profile.get("nickname")),
        )


def build_oauth_providers(settings: Settings) -> dict[str, OAuthProvider]:
    """설정된 제공자만. Google은 ID와 시크릿이 둘 다 있어야 하고, Kakao는 REST API 키만으로 켜진다."""
    providers: dict[str, OAuthProvider] = {}
    if settings.google_client_id and settings.google_client_secret:
        providers["google"] = GoogleProvider(settings.google_client_id, settings.google_client_secret)
    if settings.kakao_client_id:
        providers["kakao"] = KakaoProvider(settings.kakao_client_id, settings.kakao_client_secret)
    return providers
