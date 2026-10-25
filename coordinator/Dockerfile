# Build context is the repo root: docker compose build uses `context: .`.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY coordinator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY coordinator ./coordinator

ENV EVIDENCE_DIR=/app/data/evidence \
    AUDIT_DIR=/app/data/audit \
    REPORTS_DIR=/app/data/reports

EXPOSE 8090
CMD ["python", "-m", "coordinator.service"]
