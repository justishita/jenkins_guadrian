FROM python:3.11-slim

WORKDIR /app

COPY coordinator/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
COPY coordinator/ /app/coordinator/
# The remediation engine and GitHub clients (pure Python; agent.py's CrewAI is never imported here).
COPY agents/__init__.py /app/agents/__init__.py
COPY agents/code_agent/ /app/agents/code_agent/

ENV PYTHONPATH=/app

CMD ["python", "-m", "coordinator.app"]
