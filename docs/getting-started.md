---
layout: docs
title: "Getting Started with Docker"
nav_id: getting-started
description: "Run Octoprox in minutes with the pre-built Docker image, as a single instance or as a role cluster, or set up a local development environment."
---

# Getting Started

<p class="subtitle">Get Octoprox up and running in minutes with Docker or set up a local development environment.</p>

> **Coming from Scrapoxy?** Scrapoxy was discontinued in February 2026. Start Octoprox as described here, then follow [Migrating from Scrapoxy](scrapoxy-migration): it maps every Scrapoxy setting to its Octoprox equivalent and lists the client changes.

## Quick Start with Docker (Recommended)

The fastest way to get Octoprox running is with the pre-built Docker image from GitHub Container Registry. You can run a **single instance**, one process that does everything, or a **role cluster** behind nginx, where gateways, workers, the api and a controller pair each run in containers of their own - pick whichever fits. See [Architecture]({{ site.baseurl }}/architecture) for what each role does.

### Standalone (single instance)

```bash
# Download the docker-compose file
curl -O https://raw.githubusercontent.com/octoprox/octoprox/main/docker-compose.ghcr.yml

# Start all services
docker compose -f docker-compose.ghcr.yml up -d --remove-orphans

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
> - Set secure random values for `OCTOPROX_JWT_SECRET` and `OCTOPROX_INTERNAL_SECRET`, two different ones
> - Change `OCTOPROX_DB_PASSWORD` and update `POSTGRES_PASSWORD` to match
>
> Upgrading a 5.x install? Do not download over your compose file, which holds your settings: [Migrating to 6.0]({{ site.baseurl }}/migrating-to-6) says how to carry them into the 6.0 one. A single instance switches with one restart.

To stop the services:

```bash
docker compose -f docker-compose.ghcr.yml down
```

### Role cluster (behind nginx)

For higher availability and throughput, run each role in containers of its own behind an nginx load balancer: two api services for the web UI and API, two gateways for proxy and tunnel traffic, two controllers (one leads, the other stands by) and two workers for health checks, syncs and scaling, all sharing a single Postgres and Redis. The cluster compose file mounts its nginx config from `nginx/nginx.conf`, so you need to download **both** files - keep the config in an `nginx/` directory next to the compose file:

```bash
# Download the cluster compose file and the nginx config it mounts
curl -O https://raw.githubusercontent.com/octoprox/octoprox/main/docker-compose.cluster.ghcr.yml
mkdir -p nginx
curl -o nginx/nginx.conf https://raw.githubusercontent.com/octoprox/octoprox/main/nginx/nginx.conf

# Start the cluster (api, gateways, controllers, workers + nginx + shared Postgres/Redis)
docker compose -f docker-compose.cluster.ghcr.yml up -d --remove-orphans

# View aggregated logs
docker compose -f docker-compose.cluster.ghcr.yml logs -f
```

Once started, the cluster exposes the same endpoints as the single instance, plus the tunnel ports:
- **Web UI / API**: [http://localhost:8000](http://localhost:8000)
- **Proxy Server**: [http://localhost:8080](http://localhost:8080)
- **WireGuard**: `localhost:51820/udp` (see [WireGuard Devices]({{ site.baseurl }}/wireguard))
- **OpenVPN**: `localhost:1194` over UDP or TCP (see [OpenVPN Devices]({{ site.baseurl }}/openvpn))

The gateways route requests once the controller has published its first routing feed, a few seconds after the start.

> **Important**: the compose file mounts `./nginx/nginx.conf`, so make sure the config is downloaded first: Compose refuses to start the cluster without it. Apply the same production hardening as the single instance: set a strong `OCTOPROX_AUTH_PASSWORD`, secure random values for `OCTOPROX_JWT_SECRET` and `OCTOPROX_INTERNAL_SECRET` (the internal secret the same on every service), and change `OCTOPROX_DB_PASSWORD` to match `POSTGRES_PASSWORD`.
>
> Upgrading a 5.x cluster? Do not download over your files, which hold your settings: [Migrating to 6.0]({{ site.baseurl }}/migrating-to-6) says how to carry them across.

To stop the cluster:

```bash
docker compose -f docker-compose.cluster.ghcr.yml down
```

For how the roles work together - what each runs, how the routing feed reaches the gateways, and how failover happens - see [Architecture]({{ site.baseurl }}/architecture) and [Deployment & Scaling]({{ site.baseurl }}/deployment).

## Local Development Setup

### Prerequisites

- Python 3.12+ (use `pyenv` for version management)
- Node.js 24+ (for frontend development, use `nvm` for version management)
- Chrome or Edge 111+, Safari 16.4+ or Firefox 128+ for the web UI
- Docker and Docker Compose (for Redis 7.2+ and PostgreSQL)

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
nvm use 24  # or: nvm use --lts

# Install frontend dependencies
make web-install

# Start the development server
make web-dev
```

The web UI will be available at `http://localhost:3000`.

> **Note:** If you see npm errors, ensure Node.js is properly installed:
> ```bash
> node --version  # Should be v24.x.x or higher
> npm --version   # Should be v11.x.x or higher
> ```

## Docker Deployment

Octoprox ships with four ready-made Docker Compose flavours so you can
match the deployment shape to what you actually need - a single
instance for development, or a role cluster fronted by nginx for HA and
higher throughput. Two of those flavours use the pre-built image from
GitHub Container Registry (no local build), and two build from your
checkout.

| File                              | Image source         | Processes | Use for                              |
|-----------------------------------|----------------------|-----------|--------------------------------------|
| `docker-compose.yml`              | local build          | 1 (role `all`) | day-to-day development          |
| `docker-compose.ghcr.yml`         | `ghcr.io/.../latest` | 1 (role `all`) | quickest "just run it" demo     |
| `docker-compose.cluster.yml`      | local build          | 2 api, 2 gateways, 2 controllers, 2 workers + nginx | testing the role cluster |
| `docker-compose.cluster.ghcr.yml` | `ghcr.io/.../latest` | 2 api, 2 gateways, 2 controllers, 2 workers + nginx | production-ready starting point |

Single-instance shortcuts:

```bash
# Local build
make docker-compose-up
make docker-compose-down

# Build the image manually
make docker-build
make docker-run
```

Role cluster shortcuts:

```bash
# Local build (api, gateways, controllers, workers + nginx + shared Postgres/Redis)
make cluster-up
make cluster-logs
make cluster-down

# Pre-built GHCR image - production-ready starting point.
# Needs the compose file + nginx/nginx.conf - see the cluster Quick Start above.
docker compose -f docker-compose.cluster.ghcr.yml up -d --remove-orphans
docker compose -f docker-compose.cluster.ghcr.yml logs -f
docker compose -f docker-compose.cluster.ghcr.yml down
```

The cluster exposes the same `:8000` (API/UI) and `:8080` (proxy
traffic) ports as the single-instance setup, plus `:51820/udp` for
WireGuard and `:1194` for OpenVPN. See [Deployment & Scaling]({{ site.baseurl }}/deployment) for
scaling each role, failover and inspecting the cluster, and
[Running on Kubernetes]({{ site.baseurl }}/kubernetes) for the same layout on Kubernetes.

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
| `make cluster-up` | Start the role cluster behind nginx (local build) |
| `make cluster-down` | Stop the cluster |
| `make cluster-logs` | Tail aggregated logs from the cluster |
| `make cluster-rebuild` | Rebuild the image and restart the cluster |
| `make web-install` | Install frontend dependencies |
| `make web-dev` | Run frontend dev server |
| `make web-build` | Build frontend for production |

## Next Steps

- [Configuration]({{ site.baseurl }}/configuration) - Learn about configuration options
- [Architecture]({{ site.baseurl }}/architecture) - The roles, the routing feed, jobs, and what happens when Redis is lost
- [Deployment & Scaling]({{ site.baseurl }}/deployment) - Single instance or role cluster, scaling each role, failover
- [Migrating to 6.0]({{ site.baseurl }}/migrating-to-6) - Upgrading a 5.x install
- [AWS Setup]({{ site.baseurl }}/aws-setup) - Set up AWS cloud connector
- [GCP Setup]({{ site.baseurl }}/gcp-setup) - Set up GCP cloud connector
- [Azure Setup]({{ site.baseurl }}/azure-setup) - Set up Azure cloud connector
- [Proxy Providers & SDK]({{ site.baseurl }}/providers) - Add any proxy vendor as a declarative descriptor
- [BrightData Setup]({{ site.baseurl }}/brightdata-setup) - Set up BrightData proxy provider
- [Oxylabs Setup]({{ site.baseurl }}/oxylabs-setup) - Set up Oxylabs proxy provider
- [API Reference]({{ site.baseurl }}/api) - Explore the REST API

