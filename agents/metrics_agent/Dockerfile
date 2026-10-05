# Build context is the repo root: docker compose build uses `context: .`.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY agents/metrics_agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agents/__init__.py ./agents/__init__.py
COPY agents/metrics_agent ./agents/metrics_agent
COPY common ./common

ENV EVIDENCE_DIR=/app/evidence

CMD ["python", "-m", "agents.metrics_agent"]
