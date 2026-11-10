# 기본 CPU 대표색·이미지 품질 분석기와 교체 플러그인 실행기를 함께 배포한다.
FROM python:3.11.9-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_PACKAGE=torch==2.13.0+cpu
ARG TORCHVISION_PACKAGE=torchvision==0.28.0+cpu
WORKDIR /app
COPY server/services/analysis/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# PersonAttributeAnalyzer가 쓰는 학습 모델 의존성. 기본은 CPU wheel이며 GPU Compose는
# 같은 PyTorch 버전의 CUDA wheel을 선택한다. 코드보다 먼저 설치해 재설치를 피한다.
RUN pip install --no-cache-dir --index-url ${TORCH_INDEX_URL} \
    ${TORCH_PACKAGE} ${TORCHVISION_PACKAGE}
COPY lib /opt/ai-cctv-core
RUN pip install --no-cache-dir '/opt/ai-cctv-core[processing]'
COPY server/services/analysis/app /app/server/services/analysis/app
COPY server/services/analysis/processors /app/server/services/analysis/processors
USER 65532:65532
RUN python -c "from ai_cctv_core.processing.plugins import load_factory; from server.services.analysis.processors.attributes import PersonAttributeAnalyzer; refs = ('server.services.analysis.processors.attributes:PersonAttributeAnalyzer', 'server.services.analysis.processors:PersonAttributeAnalyzer', 'server.services.analysis.processors:LocalAppearanceAnalyzer', 'server.services.analysis.processors.appearance:LocalAppearanceAnalyzer'); assert all(load_factory(ref) is PersonAttributeAnalyzer for ref in refs)"
CMD ["uvicorn", "server.services.analysis.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 개발 단계에만 테스트·검사 도구를 추가한다.
# target을 생략해도 마지막 단계는 운영 이미지다.
FROM runtime AS production
