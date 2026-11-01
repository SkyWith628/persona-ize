"""Persona_ize 백엔드 진입점. 현재는 상태 확인 엔드포인트만 있다."""
from fastapi import FastAPI

app = FastAPI(title="Persona_ize")


@app.get("/health")
def health() -> dict[str, str]:
    """서비스 기동 여부를 반환한다."""
    return {"status": "ok"}
