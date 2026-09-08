# 저장소 루트를 빌드 기준으로 사용한다. 의존성을 먼저 설치해 코드 변경 시 캐시를 재사용한다.
FROM python:3.11.9-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY server/services/data/requirements.txt /tmp/data-requirements.txt
RUN pip install --no-cache-dir -r /tmp/data-requirements.txt

# 공유 계약과 이 서비스 코드만 가져오며 운영 DB·비밀값은 실행 시 별도로 마운트한다.
COPY lib /opt/ai-cctv-core
RUN pip install --no-cache-dir /opt/ai-cctv-core
COPY server/services/data/app /app/app

RUN addgroup --system ai-cctv \
    && adduser --system --ingroup ai-cctv ai-cctv \
    && mkdir -p /data/database/backups /data/recordings /data/snapshots \
    && chown -R ai-cctv:ai-cctv /data /app

# 애플리케이션은 관리자 권한 없이 실행한다. 호스트 저장소 권한은 Compose의 UID/GID와 맞춘다.
USER ai-cctv

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 개발 단계에만 테스트·검사 도구를 추가한다.
FROM runtime AS development
USER root
COPY server/services/data/requirements.txt /tmp/dev/requirements.txt
COPY server/services/data/requirements-dev.txt /tmp/dev/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/dev/requirements-dev.txt
USER ai-cctv
WORKDIR /workspace
ENV PYTHONPATH=/workspace:/workspace/lib:/workspace/edge/src:/app

# target을 생략해도 마지막 단계는 운영 이미지다.
FROM runtime AS production
