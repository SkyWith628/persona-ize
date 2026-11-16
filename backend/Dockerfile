# syntax=docker/dockerfile:1
# 빌드 단계에서 가상환경을 만들고 실행 단계에는 그 결과만 옮긴다. 의존성 레이어가 소스보다 먼저라
# 코드만 바뀐 빌드는 캐시를 쓴다. 비밀정보는 이미지에 넣지 않는다 (런타임 환경변수).
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml ./
# torch는 CPU 휠을 먼저 깐다 — 기본 인덱스는 CUDA 휠(수 GB)을 끌어온다. 그 뒤 db·embed(e5)·graph(graphiti) extra.
# 편집 가능 설치라 /app/app 경로만 맞으면 되므로 빈 패키지로 의존성만 먼저 깐다.
RUN mkdir app && touch app/__init__.py \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -e ".[db,embed,graph]"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH \
    HF_HOME=/var/cache/huggingface
# root가 아닌 사용자로 실행한다. e5 모델(~470MB)은 첫 기동 때 HF_HOME에 내려받는다 (compose가 볼륨으로 유지).
RUN useradd --uid 10001 --create-home app \
    && mkdir -p /var/cache/huggingface && chown app:app /var/cache/huggingface
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app pyproject.toml alembic.ini ./
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
USER app
EXPOSE 8000
# 프로세스가 응답하는지만 본다. DB·Neo4j까지 보는 점검은 /healthz?deep=true (배포 뒤 확인용).
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
# 스키마를 최신으로 올린 뒤 서버. 프록시 뒤에서 실제 접속 IP를 받는다 (로그인 시도 제한이 IP를 쓴다).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
