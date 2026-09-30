🤖 AI-Powered DevOps Incident Investigation & Remediation

Multi-agent AI that investigates failed Jenkins builds, finds the root cause, and proposes a safe fix, with a human always in the loop.

Overview · Architecture · Quick start · Agents 

📖 Overview

When a CI/CD pipeline breaks, engineers dig through build logs, dashboards and recent commits by hand. This project automates that investigation.

When a Jenkins build fails, specialised LLM agents work in parallel, each looking at a different kind of evidence. A coordinator fuses their findings into one incident report, proposes a minimal fix, checks it against policy, and waits for human approval before anything is changed.

Goal: cut debugging time from hours to minutes while keeping every AI action guarded and auditable.

🚀 Quick start
Prerequisites
Ubuntu 22.04/24.04 (VM or local), 4 vCPU / 8 GB RAM minimum (16 GB recommended), 60 GB disk
Docker with Compose v2, Python 3.11+, Git
A Gemini API key and a GitHub fine-grained token
Synchronised clock (all timestamps are UTC)
Run it
bash
git clone
cd jenkins_guardian
git checkout develop

cp .env.example .env     # fill in your values
make up                  # build and start the whole stack

Then open Jenkins at http://localhost:8080, create an API token (User → Security → API Token), add it to .env as JENKINS_API_TOKEN, and restart the backend.

Verify
bash
curl http://localhost:8000/health
python scripts/send_fake_failure.py            # fake failure, sent twice to prove idempotency
python scripts/inject_fault.py tc01_unit_test  # real failure through Jenkins
python scripts/inject_fault.py revert          # clean up
Services
Service	URL
Jenkins	http://localhost:8080
Backend API	http://localhost:8000
Grafana	http://localhost:3000
Prometheus	http://localhost:9090
RabbitMQ UI	http://localhost:15672


📁 Repository layout
jenkins_guardian/
├── common/          # shared contract: evidence schema, models, LLM client, redaction
├── backend/         # FastAPI: webhooks, events, approvals, notifications
├── agents/
│   ├── jenkins_agent/
│   ├── metrics_agent/
│   └── code_agent/
├── coordinator/     # evidence fusion and incident report
├── policies/        # OPA / Conftest rules
├── target_app/      # sample app the pipeline builds (and we break on purpose)
├── jenkins/         # Dockerfile, plugins, Configuration-as-Code
├── monitoring/      # Prometheus config, Grafana dashboards
├── scenarios/       # ground truth per test case
├── scripts/         # fault injection, fixtures, evaluation
├── tests/
├── docs/
├── docker-compose.yml
└── Makefile


