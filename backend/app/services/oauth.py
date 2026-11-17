"""소셜 로그인 제공자 (OAuth 2.0 인가 코드 방식, 서버에서 토큰 교환) 와 state 보관소.

`design/iphone-mobile` c001cbb의 `services/oauth.py`를 옮기고 state를 서버에 두도록 바꿨다 (2026-11-16).

제공자 호출은 `OAuthProvider` Protocol 뒤에 둔다 — 테스트는 가짜 제공자를 끼우거나 `transport`에
`httpx.MockTransport`를 넣어 네트워크 없이 돈다. HTTP는 이미 의존성인 `httpx`만 쓴다(새 패키지 없음).
주소·파라미터는 원 브랜치가 2026-11-16에 공식 문서로 대조한 값이다. 실제 왕복은 앱 키가 없어 확인하지 못했다.
- Google: OpenID Connect의 authorization/token/userinfo 주소, PKCE S256. userinfo의 `sub`·`email`·
  `email_verified`·`name`.
- Kakao: `kauth.kakao.com/oauth/authorize`·`/oauth/token`, `kapi.kakao.com/v2/user/me`. 이메일은 동의 항목을
  켠 앱에서만 오고(없을 수 있다) `is_email_valid`·`is_email_verified`가 모두 참일 때만 확인된 것으로 본다.
  REST API 문서에 PKCE 파라미터가 없어 쓰지 않는다 — 대신 Client Secret을 반드시 요구한다.

지키는 것:
- 제공자의 액세스 토큰은 `_exchange` 안에서만 살고 저장·기록하지 않는다. 로그에는 제공자 이름과 HTTP 상태,
  예외 종류만 남긴다(코드·토큰·응답 본문 없음).
- 제공자가 준 오류 문장은 `OAuthError`에 싣지 않는다 — 응답으로 나가는 것은 우리가 정한 코드뿐이다.
- state(`OAuthStateStore`): 256비트 무작위, 한 번 꺼내면 사라지고(일회용), `OAUTH_STATE_TTL_SECONDS` 뒤에는
  못 쓴다. PKCE verifier·돌아갈 경로·가입 조건(동의·초대 코드 통과)은 쿠키가 아니라 여기에 둔다 — 브라우저가
  가진 것은 state 값 하나뿐이다. 프로세스 메모리다(워커 1개 전제, 재시작하면 진행 중이던 로그인은 다시 한다).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.core.security import token_hash
from app.domain.auth import MAX_DISPLAY_NAME, MAX_EMAIL, MAX_SUBJECT

log = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - 주소
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
KAKAO_AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"  # noqa: S105 - 주소
KAKAO_ME_URL = "https://kapi.kakao.com/v2/user/me"

HTTP_TIMEOUT_SECONDS = 10.0
OAUTH_STATE_TTL_SECONDS = 600
MAX_PENDING_STATES = 5000  # 시작만 하고 돌아오지 않는 요청이 쌓여도 메모리는 여기까지
MAX_CODE_CHARS = 2048  # 인가 코드는 수백 자다. 터무니없이 긴 값은 제공자에 보내지 않는다
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+")


class OAuthError(Exception):
    """제공자와의 교환이 실패했다. 메시지는 우리가 쓴 문장뿐이다 (토큰·코드·제공자 응답을 넣지 않는다)."""


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


# ---------- state ----------


@dataclass(frozen=True)
class PendingLogin:
    """`start`가 만들고 `callback`이 한 번 꺼내 쓰는 진행 중 로그인."""

    provider: str
    next_path: str
    code_verifier: str | None = None
    consent: bool = False  # 시작할 때 약관·개인정보 안내에 동의했는가 (새 계정을 만들 때만 쓴다)
    invite_ok: bool = False  # 시작할 때 초대 코드를 맞게 냈는가 (또는 초대 코드가 필요 없는 서버인가)


class OAuthStateStore:
    """state → 진행 중 로그인. 키는 state의 해시다(메모리가 새도 state를 되살릴 수 없다)."""

    def __init__(
        self,
        ttl_seconds: float = OAUTH_STATE_TTL_SECONDS,
        max_entries: int = MAX_PENDING_STATES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: dict[str, tuple[float, PendingLogin]] = {}  # 넣은 순서 = 오래된 순서

    def __len__(self) -> int:
        return len(self._entries)

    def issue(self, pending: PendingLogin) -> str:
        """새 state를 만들어 보관하고 원문을 돌려준다 (추측할 수 없는 256비트)."""
        now = self._clock()
        self._drop_expired(now)
        while len(self._entries) >= self._max:  # 가득 차면 가장 오래된 것부터 버린다
            del self._entries[next(iter(self._entries))]
        state = secrets.token_urlsafe(32)
        self._entries[token_hash(state)] = (now + self._ttl, pending)
        return state

    def consume(self, state: str | None) -> PendingLogin | None:
        """state를 꺼내며 지운다 — 두 번째 호출은 항상 None이다. 없거나 만료면 None."""
        if not state or len(state) > MAX_CODE_CHARS:
            return None
        found = self._entries.pop(token_hash(state), None)
        if found is None or found[0] <= self._clock():
            return None
        return found[1]

    def _drop_expired(self, now: float) -> None:
        for key in [k for k, (expires, _) in self._entries.items() if expires <= now]:
            del self._entries[key]


# ---------- 값 다듬기 ----------


def new_pkce_verifier() -> str:
    return secrets.token_urlsafe(48)


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256: BASE64URL(SHA256(verifier)), 패딩 없음."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def clean_email(value: Any) -> str | None:  # noqa: ANN401 - 제공자 응답 값
    """앞뒤 공백을 떼고 소문자로. 문자열이 아니거나 형식·길이가 이상하면 None."""
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or len(email) > MAX_EMAIL or not _EMAIL_RE.fullmatch(email):
        return None
    return email


def clean_name(value: Any) -> str | None:  # noqa: ANN401
    if not isinstance(value, str):
        return None
    # 제어 문자를 지우고 공백을 하나로 줄인다 (표시 이름은 화면과 관리자 목록에 그대로 나온다)
    printable = "".join(ch if ch.isprintable() else " " for ch in value)
    name = " ".join(printable.split())[:MAX_DISPLAY_NAME].strip()
    return name or None


def _subject(value: Any) -> str:  # noqa: ANN401
    subject = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not subject or len(subject) > MAX_SUBJECT:
        raise OAuthError("제공자가 사용자 식별자를 주지 않았습니다.")
    return subject


async def _exchange(
    provider: str,
    token_url: str,
    form: dict[str, str],
    userinfo_url: str,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    """코드 → 액세스 토큰 → 사용자 정보(JSON 객체). 토큰은 이 함수 밖으로 나가지 않는다."""
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS, transport=transport, follow_redirects=False
        ) as client:
            token_response = await client.post(token_url, data=form, headers={"Accept": "application/json"})
            if token_response.status_code != httpx.codes.OK:
                log.warning(
                    "oauth token exchange failed provider=%s status=%s", provider, token_response.status_code
                )
                raise OAuthError("토큰 교환에 실패했습니다.")
            token_body = token_response.json()
            access_token = token_body.get("access_token") if isinstance(token_body, dict) else None
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
        raise OAuthError("제공자에 연결하지 못했습니다.") from None  # 원래 예외(주소·본문)를 달고 가지 않는다
    except ValueError:  # JSON 아님
        raise OAuthError("제공자 응답을 읽지 못했습니다.") from None
    if not isinstance(info, dict):
        raise OAuthError("제공자 응답을 읽지 못했습니다.")
    return info


class GoogleProvider:
    name = "google"
    uses_pkce = True

    def __init__(
        self, client_id: str, client_secret: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport

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
        info = await _exchange(self.name, GOOGLE_TOKEN_URL, form, GOOGLE_USERINFO_URL, self._transport)
        return OAuthProfile(
            subject=_subject(info.get("sub")),
            email=clean_email(info.get("email")),
            email_verified=info.get("email_verified") is True,  # 문자열 "true"는 참으로 보지 않는다
            name=clean_name(info.get("name")),
        )


class KakaoProvider:
    name = "kakao"
    uses_pkce = False  # 카카오 REST API 문서에 PKCE 파라미터가 없다

    def __init__(
        self, client_id: str, client_secret: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport

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
            "client_secret": self._client_secret,
            "redirect_uri": redirect_uri,
        }
        info = await _exchange(self.name, KAKAO_TOKEN_URL, form, KAKAO_ME_URL, self._transport)
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


def build_oauth_providers(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, OAuthProvider]:
    """설정된 제공자만. 클라이언트 ID와 시크릿이 둘 다 있어야 켜진다 — 없으면 빈 dict(소셜 로그인 꺼짐)."""
    providers: dict[str, OAuthProvider] = {}
    enabled = settings.oauth_provider_names
    if "google" in enabled:
        providers["google"] = GoogleProvider(
            settings.google_client_id.strip(), settings.google_client_secret.strip(), transport=transport
        )
    if "kakao" in enabled:
        providers["kakao"] = KakaoProvider(
            settings.kakao_client_id.strip(), settings.kakao_client_secret.strip(), transport=transport
        )
    return providers
