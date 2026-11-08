from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import State

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(state: State) -> dict[str, str]:
    return {
        "status": "ok",
        "llm_provider": state.settings.llm_provider,
        "llm_model": state.settings.llm_model if state.settings.llm_provider != "fake" else "fake-echo",
        "prompt_version": state.settings.prompt_version,
        "storage": state.storage,
    }
