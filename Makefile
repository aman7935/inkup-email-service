.PHONY: install run test lint format typecheck up down logs

install:
	uv sync --all-groups

run:
	uv run python -m inkup_email_service

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f
