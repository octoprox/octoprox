# Octoprox

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A dynamic and flexible proxy manager that acts as an intelligent proxy aggregator, accepting client requests and routing them through managed proxy pools.

## Features

- **Cloud Integrations**: Dynamically provision proxy instances on AWS, GCP, and Azure
- **Proxy Providers & SDK**: Oxylabs, Bright Data, Decodo, Webshare, IPRoyal and NetNut ship as declarative descriptors; admins add any other vendor from the UI without code or a redeploy (see [docs/providers.md](docs/providers.md))
- **Static Proxy Support**: Manage manually configured proxy servers
- **Routing Strategies**: Round-robin, least-used, random, sticky session, and health-based routing
- **Health Monitoring**: Automatic health checks with configurable intervals and thresholds
- **Performance Metrics**: Track latency, success rates, and request counts per proxy
- **REST API**: Full CRUD operations for managing projects, credentials, connectors, and proxies
- **Web Dashboard**: React-based UI for monitoring and configuration
- **Backup & Migration**: Export the entire setup as an encrypted file and restore it on another instance

## Quick Start

### Quick Start with Docker (Recommended)

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
- **Web UI**: http://localhost:8000
- **API**: http://localhost:8000/api/v1
- **Proxy Server**: http://localhost:8080

Default credentials: `admin` / `admin`

> **Important**: For production, update the environment variables in `docker-compose.ghcr.yml`:
> - Set a strong `OCTOPROX_AUTH_PASSWORD`
> - Set a secure random `OCTOPROX_JWT_SECRET`
> - Change `OCTOPROX_DB_PASSWORD` and update `POSTGRES_PASSWORD` to match

To stop the services:

```bash
docker compose -f docker-compose.ghcr.yml down
```

### Running a multi-instance cluster

Octoprox can also run as a horizontally-scaled cluster behind an L4 load
balancer. Two compose flavours are shipped, mirroring the single-instance
pair: one builds the image from your checkout, one pulls the pre-built
production image from GitHub Container Registry.

```bash
# Local-build cluster (fast iteration on your own code)
make cluster-up       # 3 octoprox replicas + HAProxy + Postgres + Redis
make cluster-logs
make cluster-down

# Production-ready cluster using the pre-built ghcr.io image.
# Download both the compose file and the HAProxy config it mounts:
curl -O https://raw.githubusercontent.com/octoprox/octoprox/main/docker-compose.cluster.ghcr.yml
mkdir -p haproxy
curl -o haproxy/haproxy.cfg https://raw.githubusercontent.com/octoprox/octoprox/main/haproxy/haproxy.cfg
docker compose -f docker-compose.cluster.ghcr.yml up -d
docker compose -f docker-compose.cluster.ghcr.yml logs -f
docker compose -f docker-compose.cluster.ghcr.yml down
```

> **Important**: the cluster compose files mount `./haproxy/haproxy.cfg`. If
> that file is missing when you run `up`, Docker silently creates an empty
> directory in its place and HAProxy crash-loops printing its usage banner —
> so make sure the config is present (it ships in your checkout for the
> local-build variant; download it as shown above for the pre-built image).

The full compose-file matrix:

| File                              | Image source         | Instances | Use for                              |
|-----------------------------------|----------------------|-----------|--------------------------------------|
| `docker-compose.yml`              | local build          | 1         | day-to-day development               |
| `docker-compose.ghcr.yml`         | `ghcr.io/.../latest` | 1         | quickest "just run it" demo          |
| `docker-compose.cluster.yml`      | local build          | 3 + HAProxy | testing distributed code paths      |
| `docker-compose.cluster.ghcr.yml` | `ghcr.io/.../latest` | 3 + HAProxy | production-ready starting point    |

Endpoints exposed on the host (same for both cluster variants):

| Port | What                                                    |
|------|---------------------------------------------------------|
| 8000 | API + Web UI (HAProxy → octoprox-{1,2,3}:8000, HTTP)    |
| 8080 | Proxy traffic (HAProxy → octoprox-{1,2,3}:8080, TCP)    |
| 8404 | HAProxy stats UI — useful for seeing which backend served a given request |

All three instances share the same Postgres and Redis, generate distinct
`OCTOPROX_INSTANCE_ID` values, advertise themselves via Redis heartbeat,
and elect leaders for singleton background workers (metrics flusher,
compactor) and per-connector workers (autoscaler, provider syncer). See
[`docker-compose.cluster.yml`](docker-compose.cluster.yml),
[`docker-compose.cluster.ghcr.yml`](docker-compose.cluster.ghcr.yml), and
[`haproxy/haproxy.cfg`](haproxy/haproxy.cfg) for the wiring details.

For the architecture behind multi-instance — what's shared, what's
elected, how failover works — see the
[Deployment & Scaling docs page](https://octoprox.com/deployment).

---

### Prerequisites (for local development)

- Python 3.12+ (use `pyenv` for version management)
- Node.js 20+ (for frontend development, use `nvm` for version management)
- Docker and Docker Compose (optional, for containerized deployment)
- Redis (for session storage and caching)

---

## Installing Prerequisites

### Installing pyenv (Python Version Manager)

#### macOS

```bash
# Install using Homebrew
brew update
brew install pyenv

# Add pyenv to your shell (for zsh - default on macOS)
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.zshrc
echo '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.zshrc
echo 'eval "$(pyenv init -)"' >> ~/.zshrc

# For bash users, use ~/.bashrc instead of ~/.zshrc

# Restart your shell
exec "$SHELL"

# Install Python build dependencies
brew install openssl readline sqlite3 xz zlib tcl-tk
```

#### Linux (Ubuntu/Debian)

```bash
# Install dependencies
sudo apt update
sudo apt install -y make build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
  libffi-dev liblzma-dev

# Install pyenv using the installer script
curl https://pyenv.run | bash

# Add to your shell (for bash)
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
echo 'eval "$(pyenv init -)"' >> ~/.bashrc

# For zsh users, use ~/.zshrc instead of ~/.bashrc

# Restart your shell
exec "$SHELL"
```

#### Linux (Fedora/RHEL/CentOS)

```bash
# Install dependencies
sudo dnf install -y make gcc zlib-devel bzip2 bzip2-devel \
  readline-devel sqlite sqlite-devel openssl-devel tk-devel \
  libffi-devel xz-devel

# Install pyenv using the installer script
curl https://pyenv.run | bash

# Add to your shell (same as Ubuntu above)
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
echo 'eval "$(pyenv init -)"' >> ~/.bashrc

exec "$SHELL"
```

#### Verify pyenv Installation

```bash
pyenv --version
# Should output: pyenv 2.x.x
```

---

### Installing nvm (Node Version Manager)

#### macOS and Linux

```bash
# Install nvm using the install script
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash

# The script automatically adds nvm to your shell profile
# Restart your shell or run:
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

# Verify installation
nvm --version
```

#### Install Node.js using nvm

```bash
# Install the latest LTS version of Node.js
nvm install --lts

# Or install a specific version
nvm install 20

# Set default Node.js version
nvm alias default 20

# Verify installation
node --version   # Should output: v20.x.x
npm --version    # Should output: 10.x.x
```

#### Alternative: Install Node.js using Homebrew (macOS only)

```bash
brew install node@20
brew link node@20
```

---

### Installing Docker (Optional)

#### macOS

```bash
# Install Docker Desktop using Homebrew
brew install --cask docker

# Or download from https://www.docker.com/products/docker-desktop
```

#### Linux (Ubuntu/Debian)

```bash
# Add Docker's official GPG key and repository
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add your user to the docker group (to run without sudo)
sudo usermod -aG docker $USER
newgrp docker
```

---

## Local Development Setup

1. **Clone and setup Python environment:**

```bash
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

2. **Start Redis and postgres:**

```bash
docker-compose up -d redis
docker-compose up -d postgres
```

3. **Run the API server:**

```bash
make run-dev
```

The API will be available at `http://localhost:8000`.

4. **Run the frontend (optional):**

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

### Docker Deployment

```bash
# Build and run with docker-compose
make docker-compose-up

# Or build and run manually
make docker-build
make docker-run
```

## Configuration

Configuration is loaded from YAML files in the `config/` directory based on the `OCTOPROX_ENV` environment variable.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OCTOPROX_ENV` | Environment (development/production) | development |
| `OCTOPROX_REDIS_URL` | Redis connection URL | redis://localhost:6379/0 |
| `OCTOPROX_LOG_LEVEL` | Logging level | INFO |
| `OCTOPROX_AUTH_USERNAME` | Initial admin username (used to seed admin on first startup) | admin |
| `OCTOPROX_AUTH_PASSWORD` | Initial admin password (required) | (empty) |
| `OCTOPROX_JWT_SECRET` | Secret key for JWT token signing | change-me-in-production |
| `OCTOPROX_JWT_EXPIRY_HOURS` | JWT token expiry in hours | 24 |

### Authentication

Octoprox requires authentication for all web UI and API access. Users must log in with a username and password. On first startup, an admin user is automatically created from `OCTOPROX_AUTH_USERNAME` and `OCTOPROX_AUTH_PASSWORD`.

There are three roles:
- **Admin** — Full access including user management
- **Editor** — Can manage projects, proxies, credentials, and connectors (no user management)
- **Viewer** — Read-only access to all data

#### Configuration

Set the following environment variables:

```bash
export OCTOPROX_AUTH_USERNAME=admin
export OCTOPROX_AUTH_PASSWORD=your-secure-password
export OCTOPROX_JWT_SECRET=your-random-secret-key
```

Or create a `.env` file in the project root:

```env
OCTOPROX_AUTH_USERNAME=admin
OCTOPROX_AUTH_PASSWORD=your-secure-password
OCTOPROX_JWT_SECRET=your-random-secret-key
```

#### Security Notes

- **Always set a strong `OCTOPROX_JWT_SECRET`** in production. The default value is insecure.
- **Never commit credentials** to version control. Use environment variables or `.env` files (which should be gitignored).
- The `/api/v1/auth/login` and `/api/v1/auth/status` endpoints are always public.
- The `/health` endpoint is always public for load balancer health checks.
- All other API endpoints require authentication.

### Example Configuration

```yaml
server:
  host: "0.0.0.0"
  api_port: 8000
  proxy_port: 8080

proxy:
  default_strategy: round_robin
  health_check:
    enabled: true
    interval_seconds: 60
    timeout_seconds: 30
```

## Backup & Migration

Admins can export the whole setup (users, projects, credentials, connectors,
proxies and optionally metrics) as a single passphrase-encrypted `.opbak` file
from **Settings → Backup & Migration**, and restore it on the same or another
instance.

- **Export** asks for a passphrase (8+ characters). The file is encrypted with
  it and the passphrase cannot be recovered, so store it safely.
- **Import** replaces *all* existing data on the target instance in one
  transaction. Both instances must run the same Octoprox version (the import
  checks the database schema revision before decrypting).
- **Keep my current account** (on by default) preserves the importing admin so
  you are not locked out when restoring a backup from a different instance. An
  imported user with the same username is renamed to `<username>-imported`, and
  a clashing email is cleared. Untick it to restore users exactly as they are in
  the backup; you will then need to log in with credentials from the backup.

The same operations are available via the API — see
[docs/api.md](docs/api.md#backup--migration). Backup files contain password
hashes and provider credentials protected only by your passphrase; treat them
as secrets.

## Cloud Provider Setup

Octoprox can dynamically provision proxy instances on major cloud providers. Each cloud connector automatically uses Ubuntu 24.04 LTS and installs Squid proxy on instance startup.

For detailed setup instructions, see:

- [AWS Connector Setup](docs/aws-setup.md) - EC2 instances as proxy servers
- [GCP Connector Setup](docs/gcp-setup.md) - Compute Engine VMs as proxy servers
- [Azure Connector Setup](docs/azure-setup.md) - Azure VMs as proxy servers

## Proxy Providers

Residential, ISP and datacenter vendors are integrated through the provider SDK: each vendor is a YAML descriptor that declares its credential and connector fields, how they turn into proxy endpoints (gateway sessions, port-mapped IPs or an API-served list) and which vendor API calls discover zones or validate credentials. Oxylabs, Bright Data, Decodo, Webshare, IPRoyal and NetNut are shipped; admins can add or duplicate providers under **Settings → Providers**, operators can mount YAML files or install Python plugins.

- [Providers & SDK](docs/providers.md) - Descriptor format, security model, UI builder
- [BrightData Setup](docs/brightdata-setup.md)
- [Oxylabs Setup](docs/oxylabs-setup.md)

## Makefile Commands

```bash
make help          # Show all available commands
make setup         # Setup virtual environment
make setup-dev     # Setup with dev dependencies
make run           # Run production server
make run-dev       # Run development server with reload
make test          # Run tests
make lint          # Run linter
make format        # Format code
make docker-build  # Build Docker image
make docker-compose-up    # Start all services (single instance)
make docker-compose-down  # Stop all services

# Multi-instance cluster (3 octoprox replicas behind HAProxy)
make cluster-up          # Start cluster, building image from local source
make cluster-down        # Stop the cluster
make cluster-logs        # Tail aggregated cluster logs
make cluster-rebuild     # Rebuild images and restart
# Production-ready cluster (pre-built GHCR image) — run docker compose directly:
#   docker compose -f docker-compose.cluster.ghcr.yml up -d

make web-install   # Install frontend dependencies
make web-dev       # Run frontend dev server
make web-build     # Build frontend for production
```

## License

Octoprox is licensed under the Apache License, Version 2.0 - see the [LICENSE](LICENSE) file for details.

**Exception:** the built-in provider descriptors in [api/providers/builtin/](api/providers/builtin/) (the YAML files describing Oxylabs, Bright Data, Decodo, Webshare, IPRoyal and NetNut) are proprietary, all rights reserved, and may only be used as part of an Octoprox installation. See [api/providers/builtin/LICENSE](api/providers/builtin/LICENSE). Descriptors you author yourself are your own work.
