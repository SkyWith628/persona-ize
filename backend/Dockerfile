FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml ./
# 소스는 compose에서 볼륨으로 마운트되지만, 이미지 단독 실행도 가능하게 복사한다.
COPY app ./app
RUN pip install --upgrade pip && pip install -e ".[db]"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
