---
layout: docs
title: Getting Started
nav_id: getting-started
---

# Getting Started

<p class="subtitle">Get Octoprox up and running in minutes with Docker or set up a local development environment.</p>

## Quick Start with Docker (Recommended)

The fastest way to get Octoprox running is using the pre-built Docker image from GitHub Container Registry:

```bash
# Download the docker-compose file
curl -O https://raw.githubusercontent.com/octoprox/octoprox/main/docker-compose.ghcr.yml

# Start all services
docker compose -f docker-compose.ghcr.yml up -d

# View logs
docker compose -f docker-compose.ghcr.yml logs -f octoprox
```

Once started:
- **Web UI**: [http://localhost:8000](http://localhost:8000)
- **API**: [http://localhost:8000/api/v1](http://localhost:8000/api/v1)
- **Proxy Server**: [http://localhost:8080](http://localhost:8080)

Default credentials: `admin` / `admin`

> **Important**: For production, update the environment variables in `docker-compose.ghcr.yml`:
> - Set a strong `OCTOPROX_AUTH_PASSWORD`
> - Set a secure random `OCTOPROX_JWT_SECRET`
> - Change `OCTOPROX_DB_PASSWORD` and update `POSTGRES_PASSWORD` to match

To stop the services:

```bash
docker compose -f docker-compose.ghcr.yml down
```

## Local Development Setup

### Prerequisites

- Python 3.12+ (use `pyenv` for version management)
- Node.js 20+ (for frontend development, use `nvm` for version management)
- Docker and Docker Compose (for Redis and PostgreSQL)

### Step 1: Clone and Setup Python Environment

```bash
# Clone the repository
git clone https://github.com/octoprox/octoprox.git
cd octoprox

# Install Python 3.12 using pyenv
pyenv install 3.12
pyenv local 3.12

# Verify Python version
python --version  # Should output: Python 3.12.x

# Create virtual environment and install dependencies
make setup-dev

# Activate the virtual environment (if needed manually)
source .venv/bin/activate
```

### Step 2: Start Required Services

```bash
# Start Redis and PostgreSQL
docker-compose up -d redis
docker-compose up -d postgres
```

### Step 3: Run the API Server

```bash
make run-dev
```

The API will be available at `http://localhost:8000`.

### Step 4: Run the Frontend (Optional)

```bash
# Ensure you're using the correct Node.js version
nvm use 20  # or: nvm use --lts

# Install frontend dependencies
make web-install

# Start the development server
make web-dev
```

The web UI will be available at `http://localhost:3000`.

> **Note:** If you see npm errors, ensure Node.js is properly installed:
> ```bash
> node --version  # Should be v20.x.x or higher
> npm --version   # Should be v10.x.x or higher
> ```

## Docker Deployment

For production deployments using Docker:

```bash
# Build and run with docker-compose
make docker-compose-up

# Or build and run manually
make docker-build
make docker-run
```

## Makefile Commands

Here are the most commonly used commands:

| Command | Description |
|---------|-------------|
| `make setup` | Create virtual environment and install dependencies |
| `make setup-dev` | Setup with development dependencies |
| `make run` | Run the API server (production mode) |
| `make run-dev` | Run the API server (development mode with reload) |
| `make test` | Run tests with coverage |
| `make lint` | Run linter (ruff) |
| `make format` | Format code with ruff |
| `make docker-compose-up` | Start all services with Docker |
| `make docker-compose-down` | Stop all services |
| `make web-install` | Install frontend dependencies |
| `make web-dev` | Run frontend dev server |
| `make web-build` | Build frontend for production |

## Next Steps

- [Configuration]({{ site.baseurl }}/configuration) - Learn about configuration options
- [AWS Setup]({{ site.baseurl }}/aws-setup) - Set up AWS cloud connector
- [GCP Setup]({{ site.baseurl }}/gcp-setup) - Set up GCP cloud connector
- [Azure Setup]({{ site.baseurl }}/azure-setup) - Set up Azure cloud connector
- [BrightData Setup]({{ site.baseurl }}/brightdata-setup) - Set up BrightData proxy provider
- [Oxylabs Setup]({{ site.baseurl }}/oxylabs-setup) - Set up Oxylabs proxy provider
- [API Reference]({{ site.baseurl }}/api) - Explore the REST API

