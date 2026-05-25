FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

EXPOSE 8000

# Container-level liveness probe hitting the no-DB /health endpoint. Lets the
# orchestrator detect a wedged worker (e.g. a frozen event loop) and restart it,
# instead of a TCP-only check reporting a hung process as healthy. Note: Koyeb uses
# its own service health-check config too -- set that to HTTP GET /health as well.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=4)" || exit 1

# Run multiple workers so a single frozen worker can't take down the whole service.
# pool_size is per-worker, so total DB connections = WEB_CONCURRENCY * (pool_size + max_overflow).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --timeout-graceful-shutdown 25"]
