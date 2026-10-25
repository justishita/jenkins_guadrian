FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY agents/jenkins_agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agents/__init__.py ./agents/__init__.py
COPY agents/jenkins_agent ./agents/jenkins_agent
COPY common ./common
COPY coordinator/__init__.py coordinator/a2a_contract.py coordinator/a2a_client.py ./coordinator/

CMD ["python", "-m", "agents.jenkins_agent.agent"]