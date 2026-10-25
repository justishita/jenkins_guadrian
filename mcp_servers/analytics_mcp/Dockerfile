FROM python:3.11-slim

WORKDIR /app

COPY mcp_servers/analytics_mcp/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
COPY agents/__init__.py /app/agents/__init__.py
COPY agents/jenkins_agent/parsing/ /app/agents/jenkins_agent/parsing/
COPY mcp_servers/analytics_mcp/ /app/mcp_servers/analytics_mcp/

ENV PYTHONPATH=/app

CMD ["python", "-m", "mcp_servers.analytics_mcp.server"]
