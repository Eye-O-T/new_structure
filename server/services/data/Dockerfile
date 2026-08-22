FROM python:3.11.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY server/services/data/requirements.txt /tmp/data-requirements.txt
RUN pip install --no-cache-dir -r /tmp/data-requirements.txt

COPY src/ai_cctv_core /app/ai_cctv_core
COPY server/services/data/app /app/app

RUN addgroup --system ai-cctv \
    && adduser --system --ingroup ai-cctv ai-cctv \
    && mkdir -p /data/database/backups /data/recordings /data/snapshots \
    && chown -R ai-cctv:ai-cctv /data /app

USER ai-cctv

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
