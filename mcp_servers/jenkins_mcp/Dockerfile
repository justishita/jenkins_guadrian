FROM python:3.11-slim

WORKDIR /app

COPY mcp_servers/jenkins_mcp/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
COPY agents/jenkins_agent/tools/ /app/agents/jenkins_agent/tools/
COPY agents/jenkins_agent/parsing/ /app/agents/jenkins_agent/parsing/
COPY mcp_servers/jenkins_mcp/ /app/mcp_servers/jenkins_mcp/

ENV PYTHONPATH=/app

CMD ["python", "-m", "mcp_servers.jenkins_mcp.server"]
