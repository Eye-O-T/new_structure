# MP4 송출과 로컬 복구 조각을 만드는 개발 전용 모의 장치다.
FROM python:3.11.9-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY tests/mock_edge/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY tests/__init__.py /workspace/tests/__init__.py
COPY tests/mock_edge /workspace/tests/mock_edge
RUN useradd --uid 10001 --create-home mock-edge && mkdir -p /state /recordings && chown mock-edge:mock-edge /state /recordings
USER mock-edge
ENTRYPOINT ["python", "-m", "tests.mock_edge"]
