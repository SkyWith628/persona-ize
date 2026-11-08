FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# 소스는 compose에서 볼륨으로 마운트되지만, 이미지 단독 실행도 가능하게 복사한다.
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations

# torch는 CPU 휠을 먼저 깐다 — 기본 인덱스는 CUDA 휠(수 GB)을 끌어온다. 그 뒤 db·embed(e5)·graph(graphiti) extra.
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -e ".[db,embed,graph]"

EXPOSE 8000
# 스키마를 최신으로 올린 뒤 서버. e5 모델은 첫 기동 때 내려받는다 (compose는 HF 캐시 볼륨으로 재사용).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
