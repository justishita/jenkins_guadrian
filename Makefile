.PHONY: up down logs test lint fmt policy-test policy-fmt migrate schema

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f

test:
	python -m pytest

lint:
	ruff check .

fmt:
	ruff format .

# --- P3: policy guardrails, audit migrations and the shared evidence contract ---

# Unit-test the OPA bundle. Needs the `opa` binary, or run it through the service:
#   docker compose exec opa opa test /policies
policy-test:
	opa test policies

policy-fmt:
	opa fmt --write policies

# Apply the audit-layer migrations. Reads DATABASE_URL from the environment.
migrate:
	alembic upgrade head

# Regenerate common/evidence_schema.json from common/models.py, then verify.
schema:
	python scripts/generate_evidence_schema.py
	python scripts/generate_evidence_schema.py --check
