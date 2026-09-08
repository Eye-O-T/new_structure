# 작업 실행기와 분석 연결부만 준비한다. 실제 metadata 모델·의존성은 담당자가 추가한다.
FROM python:3.11.9-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY server/services/analysis/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY lib /opt/ai-cctv-core
RUN pip install --no-cache-dir '/opt/ai-cctv-core[processing]'
COPY server/services/analysis/app /app/server/services/analysis/app
COPY server/services/analysis/processors /app/server/services/analysis/processors
USER 65532:65532
CMD ["uvicorn", "server.services.analysis.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 개발 단계에만 테스트·검사 도구를 추가한다.
FROM runtime AS development
USER root
COPY server/services/analysis/requirements.txt /tmp/dev/requirements.txt
COPY server/services/analysis/requirements-dev.txt /tmp/dev/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/dev/requirements-dev.txt
USER 65532:65532
WORKDIR /workspace
ENV PYTHONPATH=/workspace:/workspace/lib:/workspace/edge/src:/app

# target을 생략해도 마지막 단계는 운영 이미지다.
FROM runtime AS production
