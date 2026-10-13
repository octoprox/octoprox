---
layout: docs
title: "GCP Connector: Compute Engine Proxy Setup"
nav_id: gcp-setup
description: "Set up the GCP connector so Octoprox provisions Compute Engine instances as proxy servers: host identity or service account key, roles, impersonation and connector configuration."
---

# GCP Connector Setup

The GCP Connector allows Octoprox to dynamically provision Compute Engine instances as proxy servers. This guide covers how to grant Octoprox access to your project and configure the connector.

## Prerequisites

- A Google Cloud Platform account with a project
- Billing enabled on the project

## Step 1: Choose How Octoprox Authenticates

A GCP credential in Octoprox has an **authentication mode** and an optional **service account to impersonate**:

| Mode | What is stored | When to use it |
|------|----------------|----------------|
| `host_identity` | Nothing | Octoprox runs on GCP (GKE, GCE, Cloud Run) and gets its identity from the attached service account or Workload Identity. Recommended. |
| `service_account_key` | A service account JSON key, encrypted at rest | Octoprox runs outside GCP. |

In either mode you can add `impersonate_service_account`. Octoprox then mints short-lived access tokens for that service account through the IAM Credentials API and provisions Compute Engine as it, refreshing the tokens on its own. This is how you let Octoprox manage proxies in another project, or in a customer's project, without that project issuing a key. There is no external ID equivalent: the IAM binding names the exact identity allowed to impersonate.

### 1.1 The Compute Engine roles

Whichever service account ends up calling Compute Engine (the host's, the impersonated one, or the one whose key you store) needs:

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

### 1.2 Option A: Host identity (recommended)

Give the workload that runs Octoprox a service account with the roles above:

- **GKE:** enable Workload Identity Federation for GKE on the cluster and grant the roles to the Octoprox Kubernetes service account's principal, or bind that Kubernetes service account to a Google service account that has them.
- **GCE:** attach the service account to the VM with the `cloud-platform` access scope.
- **Elsewhere:** point `GOOGLE_APPLICATION_CREDENTIALS` on the Octoprox container at a key file or a Workload Identity Federation configuration file, if you would rather supply credentials to the process than store them in Octoprox.

Octoprox picks the identity up through Application Default Credentials, so no Octoprox-side configuration is needed beyond choosing the mode.

Credential config: `{"auth_mode": "host_identity"}`.

### 1.3 Option B: Host identity impersonating a service account in another project

Use this when the proxies should live in a different project from the one Octoprox runs in. The host identity stays small; the Compute Engine roles live on a service account in the target project.

1. **In the target project**, create a service account (for example `octoprox-proxies`) and grant it the roles from 1.1.

2. **Allow the host identity to impersonate it:**

   ```bash
   gcloud iam service-accounts add-iam-policy-binding \
     octoprox-proxies@TARGET_PROJECT.iam.gserviceaccount.com \
     --member="serviceAccount:octoprox-host@HOST_PROJECT.iam.gserviceaccount.com" \
     --role="roles/iam.serviceAccountTokenCreator"
   ```

   For a GKE workload using Workload Identity Federation directly, the member is the workload's principal (`principal://iam.googleapis.com/projects/.../subject/...`) instead.

3. **Enable the IAM Service Account Credentials API** in the target project:

   ```bash
   gcloud services enable iamcredentials.googleapis.com --project=TARGET_PROJECT
   ```

Credential config: `{"auth_mode": "host_identity", "impersonate_service_account": "octoprox-proxies@TARGET_PROJECT.iam.gserviceaccount.com"}`.

The host identity is the service account attached to the Octoprox workload in the project Octoprox runs in, and it never changes. For each additional GCP project you want to provision in, repeat steps 1 to 3 in that project and add an Octoprox credential naming its service account; there is no limit on how many.

### 1.4 Option C: Service account key

1. **Sign in to the Google Cloud Console** and select your project.

2. **Navigate to IAM & Admin → Service Accounts:**
   - Go to **IAM & Admin** → **Service Accounts**
   - Click **Create Service Account**

3. **Configure the service account:**
   - Enter a name (e.g., `octoprox-service`)
   - Enter a description (e.g., "Service account for Octoprox proxy management")
   - Click **Create and Continue**

4. **Grant required permissions:** either the roles from 1.1 directly, or only `roles/iam.serviceAccountTokenCreator` on a service account that carries them (as in 1.3).

5. **Create and download the JSON key:**
   - After creating the service account, click on it to open details
   - Go to the **Keys** tab
   - Click **Add Key** → **Create new key**
   - Select **JSON** format
   - Click **Create** - the key file will be downloaded automatically

   > **Important:** Store this JSON key file securely. It provides full access to the permissions granted to the service account.

Credential config: `{"auth_mode": "service_account_key", "service_account_json": "...", "project_id": "...", "impersonate_service_account": "<optional>"}`.

## Step 2: Enable Required APIs

Ensure the Compute Engine API is enabled for your project:

```bash
gcloud services enable compute.googleapis.com --project=YOUR_PROJECT_ID
```

Or via the Console:
1. Go to **APIs & Services** → **Library**
2. Search for "Compute Engine API"
3. Click **Enable**

## Step 3: Configure Firewall Rules

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

## Step 4: Create GCP Credential in Octoprox

Using the Octoprox API or web UI, create a credential for the mode you chose in Step 1.

**Via API, host identity (Option A):**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "GCP Production",
    "type": "gcp",
    "config": {
      "auth_mode": "host_identity"
    }
  }'
```

**Via API, host identity impersonating a service account in another project (Option B):**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "GCP Customer Project",
    "type": "gcp",
    "config": {
      "auth_mode": "host_identity",
      "impersonate_service_account": "octoprox-proxies@customer-project.iam.gserviceaccount.com"
    }
  }'
```

**Via API, service account key (Option C):**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"GCP Production\",
    \"type\": \"gcp\",
    \"config\": {
      \"auth_mode\": \"service_account_key\",
      \"service_account_json\": $(cat path/to/your-service-account-key.json),
      \"project_id\": \"your-project-id\"
    }
  }"
```

`auth_mode` defaults to `service_account_key` when omitted, so credentials created with earlier versions keep working unchanged.

**Via Web UI:**
1. Navigate to your project
2. Go to **Credentials** tab
3. Click **Add Credential**
4. Select **GCP** as the type
5. Choose **Authentication**: *Host identity* or *Service account key* (then paste the JSON key and enter the project ID)
6. Optionally enter a **Service account to impersonate**
7. Click **Save**

Octoprox does not call GCP when the credential is saved. The first connector that uses it will surface any IAM problem in its error state and in the logs.

## Step 5: Create GCP Connector

Create a connector that uses your GCP credential:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/connectors \
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
1. Navigate to your project
2. Go to **Connectors** tab
3. Click **Add Connector**
4. Select your GCP credential
5. Fill in the configuration fields
6. Click **Save**

## Configuration Reference

### Credential

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `auth_mode` | No | `host_identity` or `service_account_key` (default) | `host_identity` |
| `service_account_json` | In `service_account_key` mode | Contents of the JSON key file | `{"type": "service_account", ...}` |
| `project_id` | In `service_account_key` mode | Project the key belongs to | `my-project-123` |
| `impersonate_service_account` | No | Service account impersonated before calling Compute Engine | `octoprox-proxies@customer.iam.gserviceaccount.com` |

### Connector

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `project_id` | Yes | GCP project ID | `my-project-123` |
| `instance_name` | Yes | Name prefix for instances | `octoprox-proxy` |
| `zone` | Yes | GCP zone for instances | `us-central1-a` |
| `machine_type` | Yes | Compute Engine machine type | `e2-micro` |
| `network` | No | VPC network name (default: `default`) | `default` |
| `min_proxies` | No | Minimum proxy instances (default: 1) | `1` |
| `max_proxies` | No | Maximum proxy instances (default: 10) | `10` |
| `min_rotation_period_minutes` | No | Minimum instance lifetime (default: 60) | `60` |
| `max_rotation_period_minutes` | No | Maximum instance lifetime (default: 1440) | `1440` |
| `tags` | No | Custom labels for instances | `{"environment": "prod"}` |

> **Note:** The source image is automatically selected based on machine type. Octoprox uses Ubuntu 24.04 LTS for all instances.

## Troubleshooting

**"Permission denied" errors:**
- Verify the service account that calls Compute Engine (host, impersonated, or key owner) has the required permissions in the connector's project
- In `service_account_key` mode, check that the JSON key is valid and has not been revoked
- Ensure the Compute Engine API is enabled for the connector's project

**"Could not automatically determine credentials" with `host_identity`:**
- Confirm the workload has a service account: Workload Identity on GKE, an attached service account on GCE, or `GOOGLE_APPLICATION_CREDENTIALS` set on the Octoprox container
- On GKE, check the pod uses the annotated or federated Kubernetes service account

**"Permission 'iam.serviceAccounts.getAccessToken' denied" with `impersonate_service_account`:**
- The host identity (or key owner) needs `roles/iam.serviceAccountTokenCreator` on the target service account itself, not on the project
- The IAM Service Account Credentials API must be enabled in the target service account's project

**Instances not getting external IPs:**
- Ensure your VPC network allows external IP addresses
- Check that your project has sufficient quota for external IPs

**Proxy not responding after instance starts:**
- The Squid proxy takes 1-2 minutes to install and start after the instance launches
- Check the firewall rules allow inbound traffic on port 3128
- SSH into the instance to check Squid status: `gcloud compute ssh <instance-name> --zone=<zone> --command="systemctl status squid"`
- Check the startup script logs: `gcloud compute ssh <instance-name> --zone=<zone> --command="sudo cat /var/log/syslog | grep startup-script"`

