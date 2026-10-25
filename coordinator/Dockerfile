FROM python:3.11-slim

WORKDIR /app

COPY coordinator/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
COPY coordinator/ /app/coordinator/

ENV PYTHONPATH=/app

CMD ["python", "-m", "coordinator.app"]
