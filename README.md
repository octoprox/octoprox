# Octoprox

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A dynamic and flexible proxy manager that acts as an intelligent proxy aggregator, accepting client requests and routing them through managed proxy pools.

## Features

- **Cloud Integrations**: Dynamically provision proxy instances on AWS, GCP, and Azure
- **Static Proxy Support**: Manage manually configured proxy servers
- **Routing Strategies**: Round-robin, least-used, random, sticky session, and health-based routing
- **Health Monitoring**: Automatic health checks with configurable intervals and thresholds
- **Performance Metrics**: Track latency, success rates, and request counts per proxy
- **REST API**: Full CRUD operations for managing projects, credentials, connectors, and proxies
- **Web Dashboard**: React-based UI for monitoring and configuration

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
| `OCTOPROX_AUTH_ENABLED` | Enable authentication for the web UI and API | false |
| `OCTOPROX_AUTH_USERNAME` | Login username (required if auth enabled) | admin |
| `OCTOPROX_AUTH_PASSWORD` | Login password (required if auth enabled) | (empty) |
| `OCTOPROX_JWT_SECRET` | Secret key for JWT token signing | change-me-in-production |
| `OCTOPROX_JWT_EXPIRY_HOURS` | JWT token expiry in hours | 24 |

### Authentication

Octoprox supports optional authentication to protect the web UI and API endpoints. When enabled, users must log in with a username and password to access the dashboard and API.

#### Enabling Authentication

Set the following environment variables to enable authentication:

```bash
export OCTOPROX_AUTH_ENABLED=true
export OCTOPROX_AUTH_USERNAME=admin
export OCTOPROX_AUTH_PASSWORD=your-secure-password
export OCTOPROX_JWT_SECRET=your-random-secret-key
```

Or create a `.env` file in the project root:

```env
OCTOPROX_AUTH_ENABLED=true
OCTOPROX_AUTH_USERNAME=admin
OCTOPROX_AUTH_PASSWORD=your-secure-password
OCTOPROX_JWT_SECRET=your-random-secret-key
```

#### Security Notes

- **Always set a strong `OCTOPROX_JWT_SECRET`** in production. The default value is insecure.
- **Never commit credentials** to version control. Use environment variables or `.env` files (which should be gitignored).
- The `/api/v1/auth/login` and `/api/v1/auth/status` endpoints are always public.
- The `/health` endpoint is always public for load balancer health checks.
- All other API endpoints require authentication when `OCTOPROX_AUTH_ENABLED=true`.

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

## Cloud Provider Setup

Octoprox can dynamically provision proxy instances on major cloud providers. Each cloud connector automatically uses Ubuntu 24.04 LTS and installs Squid proxy on instance startup.

For detailed setup instructions, see:

- [AWS Connector Setup](docs/aws-setup.md) - EC2 instances as proxy servers
- [GCP Connector Setup](docs/gcp-setup.md) - Compute Engine VMs as proxy servers
- [Azure Connector Setup](docs/azure-setup.md) - Azure VMs as proxy servers

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
make docker-compose-up    # Start all services
make docker-compose-down  # Stop all services
make web-install   # Install frontend dependencies
make web-dev       # Run frontend dev server
make web-build     # Build frontend for production
```

## License

This project is licensed under the Apache License, Version 2.0 - see the [LICENSE](LICENSE) file for details.
