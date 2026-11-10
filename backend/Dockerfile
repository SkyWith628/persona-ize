# syntax=docker/dockerfile:1
# Persona_ize 백엔드 이미지 (다단계). 빌드 컨텍스트는 backend/ 이다.
#
#   prod (마지막 단계 = 기본 타깃)  운영용. 비루트 실행, e5 모델 내장(오프라인), HEALTHCHECK, alembic → uvicorn
#   dev                             prod 내용 + pytest·ruff + tests/. 테스트·lint를 컨테이너로 돌릴 때 쓴다
#
#   docker build --target prod -t persona-ize/backend:prod backend/
#   docker build --target dev  -t persona-ize/backend:dev  backend/
#
# 레이어 순서가 곧 재빌드 시간이다.
#   torch(CPU) → 나머지 의존성 → e5 모델   ← 여기까지는 의존성 목록이 바뀔 때만 다시 돈다
#   → 앱 소스(app/, migrations/)            ← 코드만 고치면 이 COPY 몇 개만 다시 돈다 (몇 초)
# 예전에는 `COPY app` 뒤에 `pip install -e`가 있어 코드 한 줄에 torch를 다시 깔았다.

ARG PYTHON_VERSION=3.12

# ---- base: 공통 환경 ----
FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /app

# ---- requirements: pyproject.toml에서 설치 목록만 뽑는다 ----
# 소스 없이 의존성만 깔기 위한 단계다. 결과 파일의 내용이 같으면 pyproject.toml의 다른 줄(버전, pytest 설정)이
# 바뀌어도 아래 deps의 pip install 레이어는 캐시를 그대로 쓴다.
FROM base AS requirements
COPY pyproject.toml ./
RUN python - <<'PY'
import pathlib
import tomllib

project = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
extras = project["optional-dependencies"]
# 운영 이미지: 기본 + db(PostgreSQL) + embed(e5) + graph(Graphiti). dev는 pytest·ruff.
runtime = list(dict.fromkeys(project["dependencies"] + extras["db"] + extras["embed"] + extras["graph"]))
out = pathlib.Path("/requirements")
out.mkdir()
(out / "runtime.txt").write_text("\n".join(runtime) + "\n", encoding="utf-8")
(out / "dev.txt").write_text("\n".join(extras["dev"]) + "\n", encoding="utf-8")
PY

# ---- deps: 의존성 + 임베딩 모델 (무겁고 드물게 바뀐다) ----
FROM base AS deps
# torch는 CPU 휠을 먼저 깐다 — 기본 인덱스는 CUDA 휠(수 GB)을 끌어온다.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch
COPY --from=requirements /requirements/ /opt/pz/requirements/
RUN pip install -r /opt/pz/requirements/runtime.txt \
    && pip freeze > /opt/pz/pip-freeze.txt

# e5 임베딩 모델을 이미지에 넣는다. 실제 로더(SentenceTransformer)로 한 번 올려서 받으므로 런타임이 찾는 파일이
# 빠짐없이 캐시에 남는다. 두 번째 RUN은 오프라인으로 다시 올려 보는 자체 점검이다(캐시가 모자라면 빌드가 실패).
ARG EMBEDDING_MODEL=intfloat/multilingual-e5-small
ENV HF_HOME=/opt/hf-cache \
    HF_HUB_DISABLE_TELEMETRY=1
RUN python -c "from sentence_transformers import SentenceTransformer as S; S('${EMBEDDING_MODEL}')" \
    && chmod -R a+rX /opt/hf-cache
RUN HF_HUB_OFFLINE=1 python -c "from sentence_transformers import SentenceTransformer as S; print('e5 offline ok', S('${EMBEDDING_MODEL}').encode(['query: 확인']).shape)"

# ---- runtime: 의존성 + 앱 소스 (prod와 dev가 함께 쓰는 몸통) ----
FROM deps AS runtime
# 런타임은 huggingface.co에 접속하지 않는다 (모델은 위에서 이미지에 넣었다).
# PYTHONPATH: 패키지를 pip로 설치하지 않고 /app을 그대로 쓴다 — /app 밖에서 도는 운영 스크립트도 `import app`이 된다.
ENV HF_HUB_OFFLINE=1 \
    PYTHONPATH=/app
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/app --shell /usr/sbin/nologin app

# 컨테이너 헬스체크. TRUSTED_HOSTS(Host 허용 목록)가 켜져 있으면 127.0.0.1이라는 Host는 거절되므로
# 허용 목록의 첫 이름을 Host 헤더로 보낸다. 프록시 환경변수는 무시한다(자기 자신에게 묻는 요청이다).
COPY --chmod=755 <<'PY' /usr/local/bin/pz-healthcheck
#!/usr/local/bin/python
import os
import sys
import urllib.request

hosts = [h.strip() for h in os.environ.get("TRUSTED_HOSTS", "").split(",")]
hosts = [h.replace("*", "healthcheck", 1) for h in hosts if h and h != "*"]
request = urllib.request.Request("http://127.0.0.1:8000/healthz", headers={"Host": hosts[0]} if hosts else {})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(request, timeout=4) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception as exc:  # 어떤 실패든 unhealthy
    print(f"healthcheck failed: {exc}", file=sys.stderr)
    sys.exit(1)
PY

COPY pyproject.toml alembic.ini ./
COPY migrations ./migrations
COPY app ./app
EXPOSE 8000

# ---- dev: 테스트·lint용 (호스트 venv를 못 쓰는 PC에서 컨테이너로 돌린다) ----
# root로 둔다 — 저장소를 바인드 마운트해 쓰는 용도라 캐시 파일을 쓸 수 있어야 한다.
# 이 이미지에 .env를 넣어 돌리지 않는다(docker-compose.yml의 test 서비스 주석 참고).
FROM runtime AS dev
RUN pip install -r /opt/pz/requirements/dev.txt
COPY tests ./tests
CMD ["pytest", "-q"]

# ---- prod: 운영용 (기본 타깃) ----
FROM runtime AS prod
USER app
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=4 CMD ["pz-healthcheck"]
# 스키마를 최신으로 올린 뒤 서버. exec로 uvicorn이 PID 1이 되어 종료 신호(SIGTERM)를 직접 받는다.
# - --workers 1: 로그인 시도 제한·요청 제한·그래프 수집 잠금이 프로세스 메모리에 있다. 워커를 늘리면 각자 센다.
# - --proxy-headers: 프록시(Caddy)가 넘긴 X-Forwarded-For/Proto를 믿는다. 믿을 프록시 주소는 FORWARDED_ALLOW_IPS.
# - --timeout-keep-alive 75: 프록시의 유휴 연결 유지(60초)보다 길게 — 프록시가 재사용하려는 연결을 먼저 끊지 않게.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --no-server-header --timeout-keep-alive 75"]
