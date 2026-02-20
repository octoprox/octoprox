# GCP Connector Setup

The GCP Connector allows Octoprox to dynamically provision Compute Engine instances as proxy servers. This guide covers how to obtain the required GCP credentials and configure the connector.

## Prerequisites

- A Google Cloud Platform account with a project
- Billing enabled on the project

## Step 1: Create a Service Account for Octoprox

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

Using the Octoprox API or web UI, create a credential with your service account JSON:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
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
1. Navigate to your project
2. Go to **Credentials** tab
3. Click **Add Credential**
4. Select **GCP** as the type
5. Paste the contents of your service account JSON key file
6. Enter your GCP Project ID
7. Click **Save**

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

