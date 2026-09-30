.PHONY: up down logs test lint fmt

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