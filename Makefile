.PHONY: help setup setup-dev install run run-dev test lint format type-check clean build docker-build docker-run docker-compose-up docker-compose-down cluster-up cluster-down cluster-logs cluster-rebuild web-install web-dev web-build

PYTHON := python3
VENV := .venv
PIP := $(VENV)/bin/pip
PYTHON_VENV := $(VENV)/bin/python
UVICORN := $(VENV)/bin/uvicorn

# Default target
help:
	@echo "Octoprox - Dynamic Proxy Manager"
	@echo ""
	@echo "Setup targets:"
	@echo "  setup          - Create virtual environment and install dependencies"
	@echo "  setup-dev      - Setup with development dependencies"
	@echo "  install        - Install dependencies only (assumes venv exists)"
	@echo ""
	@echo "Run targets:"
	@echo "  run            - Run the API server (production mode)"
	@echo "  run-dev        - Run the API server (development mode with reload)"
	@echo ""
	@echo "Test & Quality targets:"
	@echo "  test           - Run tests with coverage"
	@echo "  lint           - Run linter (ruff)"
	@echo "  format         - Format code with ruff"
	@echo "  type-check     - Run type checker (mypy)"
	@echo ""
	@echo "Build targets:"
	@echo "  build          - Build Python package"
	@echo "  clean          - Remove build artifacts and cache"
	@echo ""
	@echo "Docker targets:"
	@echo "  docker-build   - Build Docker image"
	@echo "  docker-run     - Run Docker container"
	@echo "  docker-compose-up   - Start all services with docker-compose (single instance)"
	@echo "  docker-compose-down - Stop all services"
	@echo "  cluster-up          - Start 3-instance cluster behind HAProxy (local build)"
	@echo "  cluster-down        - Stop the cluster"
	@echo "  cluster-logs        - Tail logs from the cluster"
	@echo "  cluster-rebuild     - Rebuild images and restart the cluster"
	@echo ""
	@echo "Frontend targets:"
	@echo "  web-install    - Install frontend dependencies"
	@echo "  web-dev        - Run frontend dev server"
	@echo "  web-build      - Build frontend for production"

# Setup virtual environment
$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

setup: $(VENV)/bin/activate
	$(PIP) install -e .

setup-dev: $(VENV)/bin/activate
	$(PIP) install -e ".[dev]"

install:
	$(PIP) install -e .

# Run targets
run:
	$(UVICORN) api.main:app --host 0.0.0.0 --port 8000

run-dev:
	$(UVICORN) api.main:app --host 0.0.0.0 --port 8000 --reload

# Test & Quality
test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check api tests

format:
	$(VENV)/bin/ruff format api tests
	$(VENV)/bin/ruff check --fix api tests

type-check:
	$(VENV)/bin/mypy api

# Build
build:
	$(PYTHON_VENV) -m build

clean:
	rm -rf $(VENV)
	rm -rf build dist *.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -rf htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Docker
docker-build:
	docker build -t octoprox:latest .

docker-run:
	docker run -p 8000:8000 -p 8080:8080 octoprox:latest

docker-compose-up:
	docker-compose up -d

docker-compose-down:
	docker-compose down

# Multi-instance cluster: 3 octoprox replicas behind HAProxy.
# API + Web UI:    http://localhost:8000
# Proxy traffic:   localhost:8080  (HTTP/SOCKS via Proxy-Authorization)
# HAProxy stats:   http://localhost:8404
cluster-up:
	docker compose -f docker-compose.cluster.yml up -d --build

cluster-down:
	docker compose -f docker-compose.cluster.yml down

cluster-rebuild:
	docker compose -f docker-compose.cluster.yml up -d --build --force-recreate

cluster-logs:
	docker compose -f docker-compose.cluster.yml logs -f --tail=100

# Frontend
web-install:
	cd web && npm install

web-dev:
	cd web && npm run dev

web-build:
	cd web && npm run build

