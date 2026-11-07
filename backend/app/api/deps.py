"""의존성 주입과 개발용 인증.

인증은 MVP에서 `X-User-Id` 헤더 스텁이다 (3주차에 세션 쿠키 + CSRF로 교체).
어디서도 요청 본문의 user_id를 신뢰하지 않는다 — 항상 이 의존성에서 받은 값을 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from app.ai.providers import build_provider
from app.ai.retrieval.base import NullRetriever
from app.ai.workflow import RewriteWorkflow
from app.core.config import Settings, get_settings
from app.repositories.memory import InMemoryContactRepo, InMemoryRewriteRepo
from app.services.rewrite_service import RewriteService


@dataclass
class AppState:
    settings: Settings
    contacts: InMemoryContactRepo
    rewrites: InMemoryRewriteRepo
    rewrite_service: RewriteService


def build_state(settings: Settings) -> AppState:
    """앱 시작 시 한 번 만든다. 테스트는 이 함수를 직접 호출해 격리된 상태를 만든다."""
    contacts = InMemoryContactRepo()
    rewrites = InMemoryRewriteRepo()
    null = NullRetriever()
    workflow = RewriteWorkflow(
        provider=build_provider(settings),
        # C/D는 2~3주차에 교체. 지금은 검색 없이 동작해 A/B 실험만 가능하다.
        retrievers={"A": null, "B": null, "C": null, "D": null},
        prompt_version=settings.prompt_version,
    )
    return AppState(
        settings=settings,
        contacts=contacts,
        rewrites=rewrites,
        rewrite_service=RewriteService(settings, contacts, rewrites, workflow),
    )


def get_state(request: Request) -> AppState:
    return request.app.state.container


def current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(
            status_code=401, detail={"code": "unauthorized", "message": "X-User-Id 헤더가 필요합니다."}
        )
    return x_user_id.strip()


State = Annotated[AppState, Depends(get_state)]
UserId = Annotated[str, Depends(current_user_id)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
