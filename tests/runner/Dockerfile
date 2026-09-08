# 운영 env와 저장소를 포함하지 않는 서버 검증용 이미지다.
FROM python:3.11.9-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/workspace:/workspace/lib:/workspace/edge/src
WORKDIR /workspace
COPY server/services/data/requirements.txt server/services/data/requirements.txt
COPY server/services/external/requirements.txt server/services/external/requirements.txt
COPY server/services/analysis/requirements.txt server/services/analysis/requirements.txt
COPY tests/runner/requirements.txt tests/runner/requirements.txt
RUN pip install --no-cache-dir -r tests/runner/requirements.txt
COPY lib /opt/ai-cctv-core
RUN pip install --no-cache-dir /opt/ai-cctv-core
COPY . /workspace
RUN useradd --uid 10001 --create-home tester
USER tester
CMD ["python", "-m", "pytest", "-c", "tests/runner/pytest.ini", "--rootdir=.", "-q"]
