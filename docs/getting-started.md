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

Octoprox ships with four ready-made Docker Compose flavours so you can
match the deployment shape to what you actually need — a single
instance for development, or a horizontally-scaled cluster fronted by
HAProxy for HA / higher throughput. Two of those flavours use the
pre-built image from GitHub Container Registry (no local build), and
two build from your checkout.

| File                              | Image source         | Instances | Use for                              |
|-----------------------------------|----------------------|-----------|--------------------------------------|
| `docker-compose.yml`              | local build          | 1         | day-to-day development               |
| `docker-compose.ghcr.yml`         | `ghcr.io/.../latest` | 1         | quickest "just run it" demo          |
| `docker-compose.cluster.yml`      | local build          | 3 + HAProxy | testing distributed code paths      |
| `docker-compose.cluster.ghcr.yml` | `ghcr.io/.../latest` | 3 + HAProxy | production-ready starting point    |

Single-instance shortcuts:

```bash
# Local build
make docker-compose-up
make docker-compose-down

# Build the image manually
make docker-build
make docker-run
```

Multi-instance cluster shortcuts:

```bash
# Local build (3 replicas + HAProxy + shared Postgres/Redis)
make cluster-up
make cluster-logs
make cluster-down

# Pre-built GHCR image — production-ready starting point
docker compose -f docker-compose.cluster.ghcr.yml up -d
docker compose -f docker-compose.cluster.ghcr.yml logs -f
docker compose -f docker-compose.cluster.ghcr.yml down
```

The cluster exposes the same `:8000` (API/UI) and `:8080` (proxy
traffic) ports as the single-instance setup, plus `:8404` for HAProxy
stats. See [Deployment & Scaling]({{ site.baseurl }}/deployment) for
how the multi-instance machinery works — what's shared, what's elected,
how failover happens.

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
| `make docker-compose-up` | Start all services with Docker (single instance) |
| `make docker-compose-down` | Stop all services |
| `make cluster-up` | Start 3-instance cluster behind HAProxy (local build) |
| `make cluster-down` | Stop the cluster |
| `make cluster-logs` | Tail aggregated logs from the cluster |
| `make cluster-rebuild` | Rebuild images and restart the cluster |
| `make web-install` | Install frontend dependencies |
| `make web-dev` | Run frontend dev server |
| `make web-build` | Build frontend for production |

## Next Steps

- [Configuration]({{ site.baseurl }}/configuration) - Learn about configuration options
- [Deployment & Scaling]({{ site.baseurl }}/deployment) - Single vs multi-instance, HA, and how horizontal scaling works
- [AWS Setup]({{ site.baseurl }}/aws-setup) - Set up AWS cloud connector
- [GCP Setup]({{ site.baseurl }}/gcp-setup) - Set up GCP cloud connector
- [Azure Setup]({{ site.baseurl }}/azure-setup) - Set up Azure cloud connector
- [BrightData Setup]({{ site.baseurl }}/brightdata-setup) - Set up BrightData proxy provider
- [Oxylabs Setup]({{ site.baseurl }}/oxylabs-setup) - Set up Oxylabs proxy provider
- [API Reference]({{ site.baseurl }}/api) - Explore the REST API

