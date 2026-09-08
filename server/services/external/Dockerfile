# 공개 API 서버의 실행 환경이다. 운영 비밀값은 이미지에 넣지 않고 Compose에서 전달한다.
FROM python:3.11.9-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY server/services/external/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --requirement requirements.txt

COPY lib /opt/ai-cctv-core
RUN pip install --no-cache-dir /opt/ai-cctv-core
COPY server/services/external/app ./app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).read()"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

# 개발 단계에만 테스트·검사 도구를 추가한다.
FROM runtime AS development
USER root
COPY server/services/external/requirements.txt /tmp/dev/requirements.txt
COPY server/services/external/requirements-dev.txt /tmp/dev/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/dev/requirements-dev.txt
USER app
WORKDIR /workspace
ENV PYTHONPATH=/workspace:/workspace/lib:/workspace/edge/src:/app

# target을 생략해도 마지막 단계는 운영 이미지다.
FROM runtime AS production
