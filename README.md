# Octoprox

A dynamic and flexible proxy manager that acts as an intelligent proxy aggregator, accepting client requests and routing them through managed proxy pools.

## Features

- **Multiple Proxy Sources**: Support for static proxy lists, API-based providers, and cloud integrations (AWS, GCP, Azure)
- **Routing Strategies**: Round-robin, least-used, random, sticky session, and health-based routing
- **Health Monitoring**: Automatic health checks with configurable intervals and thresholds
- **Performance Metrics**: Track latency, success rates, and request counts per proxy
- **REST API**: Full CRUD operations for managing proxies and sources
- **Web Dashboard**: React-based UI for monitoring and configuration
- **Kubernetes Ready**: Includes deployment manifests for k8s

## Quick Start

### Prerequisites

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

### Kubernetes Deployment

```bash
# Apply Kubernetes manifests
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```

## Project Structure

```
octoprox/
├── api/                    # Python backend
│   ├── main.py            # FastAPI application entry point
│   ├── core/              # Core business logic
│   │   ├── config.py      # Configuration management
│   │   ├── proxy_manager.py   # Proxy pool management
│   │   └── health_checker.py  # Health check logic
│   ├── models/            # Pydantic data models
│   │   ├── proxy.py       # Proxy model
│   │   └── source.py      # Source model
│   ├── routes/            # API endpoints
│   │   ├── health.py      # Health check endpoints
│   │   ├── proxies.py     # Proxy CRUD endpoints
│   │   ├── sources.py     # Source CRUD endpoints
│   │   └── metrics.py     # Metrics endpoints
│   ├── strategies/        # Routing strategies
│   │   ├── round_robin.py
│   │   ├── least_used.py
│   │   ├── random.py
│   │   ├── sticky.py
│   │   └── health_based.py
│   └── providers/         # Proxy source providers
│       ├── static.py      # Static proxy lists
│       ├── api_provider.py    # API-based providers
│       └── cloud.py       # Cloud integrations
├── web/                   # React frontend
│   └── src/
│       ├── components/    # React components
│       └── api/           # API client
├── config/                # Configuration files
│   ├── dev.yaml          # Development config
│   └── prod.yaml         # Production config
├── k8s/                   # Kubernetes manifests
│   ├── deployment.yaml
│   └── service.yaml
├── tests/                 # Test suite
├── Dockerfile
├── docker-compose.yml
├── Makefile
└── pyproject.toml
```

## API Endpoints

### Health
- `GET /health` - Service health check
- `GET /ready` - Kubernetes readiness probe
- `GET /live` - Kubernetes liveness probe

### Proxies
- `GET /api/v1/proxies` - List all proxies
- `POST /api/v1/proxies` - Add a new proxy
- `GET /api/v1/proxies/{id}` - Get proxy details
- `PATCH /api/v1/proxies/{id}` - Update a proxy
- `DELETE /api/v1/proxies/{id}` - Remove a proxy
- `POST /api/v1/proxies/strategy` - Change routing strategy
- `GET /api/v1/proxies/select/next` - Get next proxy using current strategy

### Sources
- `GET /api/v1/sources` - List all sources
- `POST /api/v1/sources` - Add a new source
- `GET /api/v1/sources/{id}` - Get source details
- `PATCH /api/v1/sources/{id}` - Update a source
- `DELETE /api/v1/sources/{id}` - Remove a source
- `POST /api/v1/sources/{id}/refresh` - Refresh source proxies

### Metrics
- `GET /api/v1/metrics` - Get pool metrics
- `GET /api/v1/metrics/prometheus` - Prometheus format metrics

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
    interval_seconds: 30
    timeout_seconds: 10

sources:
  - name: "my-proxies"
    type: static
    enabled: true
    proxies:
      - host: "proxy1.example.com"
        port: 8080
        protocol: http
```

## AWS Connector Setup

The AWS Connector allows Octoprox to dynamically provision EC2 instances as proxy servers. This section covers how to obtain the required AWS credentials and configure the connector.

### Prerequisites

- An AWS account with permissions to create and manage EC2 instances

### Step 1: Create an IAM User for Octoprox

1. **Sign in to the AWS Console** and navigate to **IAM** (Identity and Access Management).

2. **Create a new IAM user:**
   - Go to **Users** → **Create user**
   - Enter a username (e.g., `octoprox-service`)
   - Select **Programmatic access** to generate access keys

3. **Attach permissions:**
   Create a custom policy with the minimum required permissions:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "ec2:RunInstances",
           "ec2:TerminateInstances",
           "ec2:DescribeInstances",
           "ec2:DescribeInstanceStatus",
           "ec2:CreateTags",
           "ec2:DescribeImages",
           "ec2:DescribeSecurityGroups",
           "ec2:DescribeKeyPairs",
           "ec2:DescribeSubnets",
           "ec2:DescribeVpcs"
         ],
         "Resource": "*"
       }
     ]
   }
   ```

   > **Note:** For production, restrict the `Resource` field to specific VPCs, subnets, or use resource tags for finer-grained control.

4. **Generate access keys:**
   - After creating the user, go to **Security credentials** tab
   - Click **Create access key**
   - Select **Application running outside AWS**
   - Download or copy the **Access Key ID** and **Secret Access Key**

   > **Important:** Store these credentials securely. The secret key is only shown once.

### Step 2: Prepare AWS Resources

Before creating a connector, ensure you have the following AWS resources:

#### 2.1 Choose a Base AMI

Octoprox automatically installs and configures Squid proxy on instances at launch time using a startup script. You just need a base Linux AMI that supports either `apt` (Debian/Ubuntu) or `yum`/`dnf` (Amazon Linux/RHEL/CentOS).

**Recommended:** Ubuntu 25.10 (Questing Quokka) or Ubuntu 24.04 LTS (Noble Numbat).

AMI IDs change frequently. Use the following AWS CLI commands to find the latest Ubuntu AMI for your region:

```bash
# Ubuntu 25.10 (Questing Quokka) - x86_64
aws ec2 describe-images \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-questing-25.10-amd64-server-*" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
  --output text

# Ubuntu 25.10 (Questing Quokka) - ARM64 (Graviton)
aws ec2 describe-images \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-questing-25.10-arm64-server-*" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
  --output text

# Ubuntu 24.04 LTS (Noble Numbat) - x86_64
aws ec2 describe-images \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
  --output text
```

> **Note:** The owner ID `099720109477` is Canonical's official AWS account. Using this ensures you get the official Ubuntu images without marketplace subscription requirements.

#### 2.2 Create a Security Group

Create a security group that allows:
- **Inbound:** TCP port 3128 (or your proxy port) from your allowed IP ranges
- **Outbound:** All traffic (for the proxy to reach the internet)

```bash
# Using AWS CLI
aws ec2 create-security-group \
  --group-name octoprox-proxy-sg \
  --description "Security group for Octoprox proxy instances"

aws ec2 authorize-security-group-ingress \
  --group-name octoprox-proxy-sg \
  --protocol tcp \
  --port 3128 \
  --cidr 0.0.0.0/0
```

Note the Security Group ID (e.g., `sg-0123456789abcdef0`).

#### 2.3 Create an EC2 Key Pair

Create a key pair for SSH access to the proxy instances:

```bash
aws ec2 create-key-pair \
  --key-name octoprox-key \
  --query 'KeyMaterial' \
  --output text > octoprox-key.pem

chmod 400 octoprox-key.pem
```

Note the key pair name (e.g., `octoprox-key`).

### Step 3: Create AWS Credential in Octoprox

Using the Octoprox API or web UI, create a credential with your AWS access keys:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "AWS Production",
    "type": "aws",
    "config": {
      "access_key": "AKIAIOSFODNN7EXAMPLE",
      "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    }
  }'
```

**Via Web UI:**
1. Navigate to **Credentials** in the sidebar
2. Click **Add Credential**
3. Select **AWS** as the type
4. Enter your Access Key ID and Secret Access Key
5. Click **Save**

### Step 4: Create AWS Connector

Create a connector that uses your AWS credential:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "AWS US-East Proxies",
    "credential_id": "<credential-id-from-step-3>",
    "config": {
      "instance_name": "octoprox-proxy",
      "region": "us-east-1",
      "instance_type": "t3.micro",
      "ami_id": "ami-0123456789abcdef0",
      "security_group": "sg-0123456789abcdef0",
      "key_pair_name": "octoprox-key",
      "min_proxies": 1,
      "max_proxies": 10,
      "min_rotation_period_minutes": 60,
      "max_rotation_period_minutes": 1440,
      "tags": {
        "Environment": "production",
        "ManagedBy": "octoprox"
      }
    }
  }'
```

**Via Web UI:**
1. Navigate to **Connectors** in the sidebar
2. Click **Add Connector**
3. Select your AWS credential
4. Fill in the configuration fields (see below)
5. Click **Save**

### AWS Connector Configuration Reference

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `instance_name` | Yes | Name prefix for EC2 instances | `octoprox-proxy` |
| `region` | Yes | AWS region for instances | `us-east-1` |
| `instance_type` | Yes | EC2 instance type | `t3.micro` |
| `ami_id` | Yes | AMI ID with proxy software | `ami-0123456789abcdef0` |
| `security_group` | Yes | Security group ID | `sg-0123456789abcdef0` |
| `key_pair_name` | Yes | EC2 key pair name | `octoprox-key` |
| `min_proxies` | No | Minimum proxy instances (default: 1) | `1` |
| `max_proxies` | No | Maximum proxy instances (default: 10) | `10` |
| `min_rotation_period_minutes` | No | Minimum instance lifetime (default: 60) | `60` |
| `max_rotation_period_minutes` | No | Maximum instance lifetime (default: 1440) | `1440` |
| `tags` | No | Custom tags for instances | `{"Environment": "prod"}` |

### Troubleshooting

**"Access Denied" errors:**
- Verify your IAM user has the required EC2 permissions
- Check that the access key and secret key are correct
- Ensure the IAM user is not restricted by SCPs or permission boundaries

**Instances not getting public IPs:**
- Ensure your subnet has "Auto-assign public IPv4 address" enabled

**Proxy not responding after instance starts:**
- The Squid proxy takes 1-2 minutes to install and start after the instance launches
- Check the security group allows inbound traffic on port 3128
- SSH into the instance to check Squid status: `ssh -i octoprox-key.pem ubuntu@<instance-ip>` then `systemctl status squid`
- Check the startup script logs: `cat /var/log/cloud-init-output.log`

## GCP Connector Setup

The GCP Connector allows Octoprox to dynamically provision Compute Engine instances as proxy servers. This section covers how to obtain the required GCP credentials and configure the connector.

### Prerequisites

- A Google Cloud Platform account with a project
- Billing enabled on the project

### Step 1: Create a Service Account for Octoprox

1. **Sign in to the Google Cloud Console** and select your project.

2. **Navigate to IAM & Admin → Service Accounts:**
   - Go to **IAM & Admin** → **Service Accounts**
   - Click **Create Service Account**

3. **Configure the service account:**
   - Enter a name (e.g., `octoprox-service`)
   - Enter a description (e.g., "Service account for Octoprox proxy management")
   - Click **Create and Continue**

4. **Grant required permissions:**
   Add the following roles to the service account:
   - `Compute Instance Admin (v1)` - To create and delete instances
   - `Service Account User` - To attach service accounts to instances

   Or create a custom role with these specific permissions:
   ```
   compute.instances.create
   compute.instances.delete
   compute.instances.get
   compute.instances.list
   compute.instances.setMetadata
   compute.instances.setTags
   compute.disks.create
   compute.subnetworks.use
   compute.subnetworks.useExternalIp
   compute.networks.use
   ```

5. **Create and download the JSON key:**
   - After creating the service account, click on it to open details
   - Go to the **Keys** tab
   - Click **Add Key** → **Create new key**
   - Select **JSON** format
   - Click **Create** - the key file will be downloaded automatically

   > **Important:** Store this JSON key file securely. It provides full access to the permissions granted to the service account.

### Step 2: Enable Required APIs

Ensure the Compute Engine API is enabled for your project:

```bash
gcloud services enable compute.googleapis.com --project=YOUR_PROJECT_ID
```

Or via the Console:
1. Go to **APIs & Services** → **Library**
2. Search for "Compute Engine API"
3. Click **Enable**

### Step 3: Configure Firewall Rules

Create a firewall rule to allow inbound traffic to the proxy port:

```bash
gcloud compute firewall-rules create octoprox-allow-proxy \
  --project=YOUR_PROJECT_ID \
  --direction=INGRESS \
  --priority=1000 \
  --network=default \
  --action=ALLOW \
  --rules=tcp:3128 \
  --source-ranges=0.0.0.0/0
```

> **Note:** For production, restrict `--source-ranges` to your specific IP ranges instead of `0.0.0.0/0`.

### Step 4: Create GCP Credential in Octoprox

Using the Octoprox API or web UI, create a credential with your service account JSON:

**Via API:**

```bash
# Read the service account JSON file and create the credential
SERVICE_ACCOUNT_JSON=$(cat path/to/your-service-account-key.json)

curl -X POST http://localhost:8000/api/v1/credentials \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"GCP Production\",
    \"type\": \"gcp\",
    \"config\": {
      \"service_account_json\": $(cat path/to/your-service-account-key.json),
      \"project_id\": \"your-project-id\"
    }
  }"
```

**Via Web UI:**
1. Navigate to **Credentials** in the sidebar
2. Click **Add Credential**
3. Select **GCP** as the type
4. Paste the contents of your service account JSON key file
5. Enter your GCP Project ID
6. Click **Save**

### Step 5: Create GCP Connector

Create a connector that uses your GCP credential:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "GCP US-Central Proxies",
    "credential_id": "<credential-id-from-step-4>",
    "config": {
      "project_id": "your-project-id",
      "instance_name": "octoprox-proxy",
      "zone": "us-central1-a",
      "machine_type": "e2-micro",
      "network": "default",
      "source_image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
      "min_proxies": 1,
      "max_proxies": 10,
      "min_rotation_period_minutes": 60,
      "max_rotation_period_minutes": 1440,
      "tags": {
        "environment": "production",
        "managed-by": "octoprox"
      }
    }
  }'
```

**Via Web UI:**
1. Navigate to **Connectors** in the sidebar
2. Click **Add Connector**
3. Select your GCP credential
4. Fill in the configuration fields (see below)
5. Click **Save**

### Choosing a Source Image

Octoprox automatically installs and configures Squid proxy on instances at launch time using a startup script. You just need a base Linux image that supports `apt` (Debian/Ubuntu) or `yum`/`dnf` (RHEL/CentOS).

**Recommended images:**

| Image Family | Source Image Path | Description |
|--------------|-------------------|-------------|
| Ubuntu 24.04 LTS | `projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64` | Long-term support (recommended) |
| Ubuntu 22.04 LTS | `projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts` | Previous LTS |
| Debian 12 | `projects/debian-cloud/global/images/family/debian-12` | Stable Debian |

To list available Ubuntu images:

```bash
gcloud compute images list --project=ubuntu-os-cloud --filter="family~ubuntu" --format="table(family,name)"
```

### GCP Connector Configuration Reference

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `project_id` | Yes | GCP project ID | `my-project-123` |
| `instance_name` | Yes | Name prefix for instances | `octoprox-proxy` |
| `zone` | Yes | GCP zone for instances | `us-central1-a` |
| `machine_type` | Yes | Compute Engine machine type | `e2-micro` |
| `network` | No | VPC network name (default: `default`) | `default` |
| `subnetwork` | No | Subnetwork name (optional) | `my-subnet` |
| `source_image` | Yes | Source image for instances | `projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64` |
| `min_proxies` | No | Minimum proxy instances (default: 1) | `1` |
| `max_proxies` | No | Maximum proxy instances (default: 10) | `10` |
| `min_rotation_period_minutes` | No | Minimum instance lifetime (default: 60) | `60` |
| `max_rotation_period_minutes` | No | Maximum instance lifetime (default: 1440) | `1440` |
| `tags` | No | Custom labels for instances | `{"environment": "prod"}` |

### Troubleshooting

**"Permission denied" errors:**
- Verify the service account has the required Compute Engine permissions
- Check that the service account JSON key is valid and not expired
- Ensure the Compute Engine API is enabled for your project

**Instances not getting external IPs:**
- Ensure your VPC network allows external IP addresses
- Check that your project has sufficient quota for external IPs

**Proxy not responding after instance starts:**
- The Squid proxy takes 1-2 minutes to install and start after the instance launches
- Check the firewall rules allow inbound traffic on port 3128
- SSH into the instance to check Squid status: `gcloud compute ssh <instance-name> --zone=<zone> --command="systemctl status squid"`
- Check the startup script logs: `gcloud compute ssh <instance-name> --zone=<zone> --command="sudo cat /var/log/syslog | grep startup-script"`

## Azure Connector Setup

The Azure Connector allows Octoprox to dynamically provision Azure Virtual Machines as proxy servers. This section covers how to obtain the required Azure credentials and configure the connector.

### Prerequisites

- An Azure account with an active subscription
- A resource group for Octoprox resources

### Step 1: Create a Service Principal for Octoprox

1. **Sign in to the Azure Portal** or use the Azure CLI.

2. **Create a service principal using Azure CLI:**

   ```bash
   # Login to Azure
   az login

   # Create a service principal with Contributor role on your subscription
   az ad sp create-for-rbac \
     --name "octoprox-service-principal" \
     --role Contributor \
     --scopes /subscriptions/YOUR_SUBSCRIPTION_ID
   ```

   This command outputs:
   ```json
   {
     "appId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",      # This is the client_id
     "displayName": "octoprox-service-principal",
     "password": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",   # This is the client_secret
     "tenant": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"      # This is the tenant_id
   }
   ```

   > **Important:** Save these values securely. The password (client_secret) is only shown once.

3. **Alternative: Create via Azure Portal:**
   - Go to **Microsoft Entra ID** (formerly Azure Active Directory)
   - Navigate to **App registrations** → **New registration**
   - Enter a name (e.g., `octoprox-service-principal`)
   - Click **Register**
   - Note the **Application (client) ID** and **Directory (tenant) ID**
   - Go to **Certificates & secrets** → **New client secret**
   - Create a secret and note the **Value** (this is your client_secret)

4. **Assign permissions to the service principal:**
   - Go to your **Subscription** → **Access control (IAM)**
   - Click **Add role assignment**
   - Select **Contributor** role (or create a custom role with minimal permissions)
   - Assign to your service principal

### Step 2: Create a Resource Group

Create a resource group to contain all Octoprox-managed resources:

```bash
az group create \
  --name octoprox-resources \
  --location eastus
```

### Step 3: Register Required Resource Providers

Azure subscriptions must have the required resource providers registered before creating resources. Register the Compute and Network providers:

**Via Azure CLI:**

```bash
az provider register --namespace Microsoft.Compute
az provider register --namespace Microsoft.Network

# Check registration status (wait until both show "Registered")
az provider show --namespace Microsoft.Compute --query "registrationState"
az provider show --namespace Microsoft.Network --query "registrationState"
```

**Via Azure Portal:**

1. Go to **Subscriptions** and select your subscription
2. In the left menu, navigate to **Settings** → **Resource providers**
3. Search for `Microsoft.Compute`, select it, and click **Register**
4. Search for `Microsoft.Network`, select it, and click **Register**
5. Wait 1-2 minutes for registration to complete (status changes to "Registered")

### Step 4: Create a Virtual Network and Subnet

Azure VMs require a virtual network and subnet:

```bash
# Create virtual network
az network vnet create \
  --resource-group octoprox-resources \
  --name octoprox-vnet \
  --address-prefix 10.0.0.0/16 \
  --subnet-name octoprox-subnet \
  --subnet-prefix 10.0.1.0/24
```

### Step 5: Create a Network Security Group

Create a network security group (NSG) to allow inbound traffic on the proxy port:

```bash
# Create NSG
az network nsg create \
  --resource-group octoprox-resources \
  --name octoprox-nsg

# Allow inbound traffic on port 3128
az network nsg rule create \
  --resource-group octoprox-resources \
  --nsg-name octoprox-nsg \
  --name allow-proxy \
  --priority 100 \
  --direction Inbound \
  --access Allow \
  --protocol Tcp \
  --destination-port-ranges 3128 \
  --source-address-prefixes '*'

# Associate NSG with subnet
az network vnet subnet update \
  --resource-group octoprox-resources \
  --vnet-name octoprox-vnet \
  --name octoprox-subnet \
  --network-security-group octoprox-nsg
```

> **Note:** For production, restrict `--source-address-prefixes` to your specific IP ranges instead of `'*'`.

### Step 6: Create Azure Credential in Octoprox

Using the Octoprox API or web UI, create a credential with your service principal details:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Azure Production",
    "type": "azure",
    "config": {
      "subscription_id": "your-subscription-id",
      "tenant_id": "your-tenant-id",
      "client_id": "your-client-id",
      "client_secret": "your-client-secret"
    }
  }'
```

**Via Web UI:**
1. Navigate to **Credentials** in the sidebar
2. Click **Add Credential**
3. Select **Azure** as the type
4. Enter your Subscription ID, Tenant ID, Client ID, and Client Secret
5. Click **Save**

### Step 7: Create Azure Connector

Create a connector that uses your Azure credential:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Azure East US Proxies",
    "credential_id": "<credential-id-from-step-6>",
    "config": {
      "subscription_id": "your-subscription-id",
      "resource_group": "octoprox-resources",
      "instance_name": "octoprox-proxy",
      "location": "eastus",
      "vm_size": "Standard_D2s_v3",
      "vnet_name": "octoprox-vnet",
      "subnet_name": "octoprox-subnet",
      "ssh_public_key": "ssh-rsa AAAA... user@host",
      "min_proxies": 1,
      "max_proxies": 10,
      "min_rotation_period_minutes": 60,
      "max_rotation_period_minutes": 1440,
      "tags": {
        "environment": "production",
        "managed-by": "octoprox"
      }
    }
  }'
```

**Via Web UI:**
1. Navigate to **Connectors** in the sidebar
2. Click **Add Connector**
3. Select your Azure credential
4. Fill in the configuration fields (see below)
5. Click **Save**

### VM Image

Octoprox automatically uses Ubuntu 22.04 LTS for Azure VMs and installs Squid proxy at launch time using cloud-init. No image configuration is required.

### Azure Connector Configuration Reference

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `subscription_id` | Yes | Azure subscription ID | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| `resource_group` | Yes | Resource group name | `octoprox-resources` |
| `instance_name` | Yes | Name prefix for VMs | `octoprox-proxy` |
| `location` | Yes | Azure region (must match resource group location) | `eastus` |
| `vm_size` | Yes | VM size | `Standard_D2s_v3` |
| `vnet_name` | Yes | Virtual network name | `octoprox-vnet` |
| `subnet_name` | Yes | Subnet name | `octoprox-subnet` |
| `ssh_public_key` | Yes | SSH public key for VM access | `ssh-rsa AAAA... user@host` |
| `min_proxies` | No | Minimum proxy instances (default: 1) | `1` |
| `max_proxies` | No | Maximum proxy instances (default: 10) | `10` |
| `min_rotation_period_minutes` | No | Minimum instance lifetime (default: 60) | `60` |
| `max_rotation_period_minutes` | No | Maximum instance lifetime (default: 1440) | `1440` |
| `tags` | No | Custom tags for VMs | `{"environment": "prod"}` |

To generate an SSH key pair if you don't have one:

```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/octoprox_azure
cat ~/.ssh/octoprox_azure.pub  # Copy this value for ssh_public_key
```

### Troubleshooting

**"AuthorizationFailed" errors:**
- Verify the service principal has Contributor role on the subscription or resource group
- Check that the client_id, client_secret, and tenant_id are correct
- Ensure the service principal secret has not expired

**"ResourceNotFound" errors:**
- Verify the resource group exists
- Check that the virtual network and subnet exist in the specified resource group
- Ensure the location matches where your resources are deployed

**"MissingSubscriptionRegistration" errors:**
- Your subscription needs to have the required resource providers registered
- Register them via CLI: `az provider register --namespace Microsoft.Compute` and `az provider register --namespace Microsoft.Network`
- Or via Portal: Go to **Subscriptions** → your subscription → **Resource providers**, search for `Microsoft.Compute` and `Microsoft.Network`, and click **Register** for each
- Wait 1-2 minutes for registration to complete before retrying

**VMs not getting public IPs:**
- Public IPs are created automatically for each VM
- Check that your subscription has sufficient quota for public IP addresses

**Proxy not responding after VM starts:**
- The Squid proxy takes 1-2 minutes to install and start after the VM launches
- Check the NSG allows inbound traffic on port 3128
- SSH into the VM to check Squid status: `az ssh vm --resource-group <rg> --name <vm-name> --local-user octoprox` then `systemctl status squid`
- Check cloud-init logs: `cat /var/log/cloud-init-output.log`

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

MIT
