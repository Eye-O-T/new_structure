# 공유 계약과 감지·인물 연결 플러그인을 같은 이미지에 넣는다. 빌드 기준은 저장소 루트다.
FROM python:3.11.9-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# OpenCV의 이미지·영상 처리가 필요로 하는 운영체제 라이브러리를 설치한다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_VERSION=2.8.0
RUN pip install --no-cache-dir "torch==${TORCH_VERSION}" --index-url "${TORCH_INDEX_URL}"
COPY server/services/preprocessing/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY lib /opt/ai-cctv-core
RUN pip install --no-cache-dir '/opt/ai-cctv-core[processing]'
COPY server/services/preprocessing/app /app/server/services/preprocessing/app
COPY server/services/preprocessing/processors /app/server/services/preprocessing/processors

USER 65532:65532
CMD ["uvicorn", "server.services.preprocessing.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 개발 단계에만 테스트·검사 도구를 추가한다.
# target을 생략해도 마지막 단계는 운영 이미지다.
FROM runtime AS production
