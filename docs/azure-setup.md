---
layout: docs
title: "Azure Connector: VM Proxy Setup"
nav_id: azure-setup
description: "Set up the Azure connector so Octoprox provisions virtual machines as proxy servers: managed identity or service principal, roles, tenant federation and connector configuration."
---

# Azure Connector Setup

The Azure Connector allows Octoprox to dynamically provision Azure Virtual Machines as proxy servers. This guide covers how to grant Octoprox access to your subscription and configure the connector.

## Prerequisites

- An Azure account with an active subscription
- A resource group for Octoprox resources

## Step 1: Choose How Octoprox Authenticates

An Azure credential in Octoprox has an **authentication mode** and, in host mode, an optional **target app registration** in another tenant:

| Mode | What is stored | When to use it |
|------|----------------|----------------|
| `host_identity` | Nothing | Octoprox runs on Azure (AKS, a VM, Container Apps) with a managed identity or workload identity. Recommended. |
| `client_secret` | A service principal's client secret, encrypted at rest | Octoprox runs outside Azure. |

Within one tenant no hop is needed: assign the role to the host identity on whichever subscription the connector targets. To provision in **another Entra tenant**, name an app registration there (`target_tenant_id` + `target_client_id`) that trusts the host identity through a federated identity credential. Octoprox exchanges the host identity's token for that app's token, repeating the exchange whenever the token needs refreshing. The customer never issues a secret, and the federated credential pins the exact host identity, so no shared external ID is needed.

### 1.1 The role assignment

Whichever identity ends up calling Azure Resource Manager (the host identity, the target app's service principal, or the service principal whose secret you store) needs **Contributor**, or a custom role with the compute and network permissions, on the subscription or resource group the connector targets.

### 1.2 Option A: Host identity (recommended)

Give the workload that runs Octoprox an identity:

- **AKS:** enable workload identity on the cluster, create a user-assigned managed identity, add a federated credential on it for the Octoprox Kubernetes service account, and label the pod with `azure.workload.identity/use: "true"`. The webhook injects the `AZURE_*` variables the SDK reads.
- **VM or Container Apps:** enable a system-assigned managed identity, or attach a user-assigned one. If the host has several user-assigned identities, set `managed_identity_client_id` so Octoprox picks the right one.
- **Elsewhere:** set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` on the Octoprox container, if you would rather supply credentials to the process than store them in Octoprox.

Then assign the role from 1.1 to that identity on the target subscription or resource group:

```bash
az role assignment create \
  --assignee-object-id <IDENTITY_PRINCIPAL_ID> \
  --assignee-principal-type ServicePrincipal \
  --role Contributor \
  --scope /subscriptions/YOUR_SUBSCRIPTION_ID/resourceGroups/octoprox-resources
```

Credential config: `{"auth_mode": "host_identity"}`, plus `"managed_identity_client_id"` when needed.

### 1.3 Option B: Host identity federated into another tenant

Use this when the proxies should live in a subscription that belongs to a different Entra tenant, such as a customer's.

1. **In the target tenant**, create an app registration (for example `octoprox-proxies`) under **Microsoft Entra ID → App registrations → New registration**. Note its **Application (client) ID** and the **Directory (tenant) ID**.

2. **Add a federated credential** on it that trusts the host identity. In the portal: **Certificates & secrets → Federated credentials → Add credential**, scenario **Other issuer**. Or with the CLI:

   ```bash
   az ad app federated-credential create --id <APP_OBJECT_ID> --parameters '{
     "name": "octoprox-host",
     "issuer": "https://login.microsoftonline.com/<HOST_TENANT_ID>/v2.0",
     "subject": "<HOST_IDENTITY_PRINCIPAL_ID>",
     "audiences": ["api://AzureADTokenExchange"]
   }'
   ```

   The subject is the object (principal) ID of the managed identity Octoprox runs with.

3. **Create the app's service principal in the target tenant and assign the role:**

   ```bash
   az ad sp create --id <APP_CLIENT_ID>
   az role assignment create \
     --assignee <APP_CLIENT_ID> \
     --role Contributor \
     --scope /subscriptions/TARGET_SUBSCRIPTION_ID/resourceGroups/octoprox-resources
   ```

Credential config: `{"auth_mode": "host_identity", "target_tenant_id": "<TARGET_TENANT_ID>", "target_client_id": "<APP_CLIENT_ID>"}`.

The host identity is the managed identity attached to the Octoprox workload in your own tenant, and it never changes. For each additional tenant you want to provision in, repeat steps 1 to 3 in that tenant and add an Octoprox credential naming its app registration; there is no limit on how many.

### 1.4 Option C: Service principal with client secret

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

Credential config: `{"auth_mode": "client_secret", "subscription_id": "...", "tenant_id": "...", "client_id": "...", "client_secret": "..."}`.

## Step 2: Create a Resource Group

Create a resource group to contain all Octoprox-managed resources:

```bash
az group create \
  --name octoprox-resources \
  --location eastus
```

## Step 3: Register Required Resource Providers

Azure subscriptions must have the required resource providers registered:

```bash
az provider register --namespace Microsoft.Compute
az provider register --namespace Microsoft.Network

# Check registration status (wait until both show "Registered")
az provider show --namespace Microsoft.Compute --query "registrationState"
az provider show --namespace Microsoft.Network --query "registrationState"
```

## Step 4: Create a Virtual Network and Subnet

Azure VMs require a virtual network and subnet:

```bash
az network vnet create \
  --resource-group octoprox-resources \
  --name octoprox-vnet \
  --address-prefix 10.0.0.0/16 \
  --subnet-name octoprox-subnet \
  --subnet-prefix 10.0.1.0/24
```

## Step 5: Create a Network Security Group

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

> **Note:** For production, restrict `--source-address-prefixes` to your specific IP ranges.

## Step 6: Create Azure Credential in Octoprox

Create a credential for the mode you chose in Step 1.

**Via API, host identity (Option A):**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Azure Production",
    "type": "azure",
    "config": {
      "auth_mode": "host_identity"
    }
  }'
```

**Via API, host identity federated into another tenant (Option B):**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Azure Customer Tenant",
    "type": "azure",
    "config": {
      "auth_mode": "host_identity",
      "target_tenant_id": "customer-tenant-id",
      "target_client_id": "app-client-id"
    }
  }'
```

**Via API, client secret (Option C):**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Azure Production",
    "type": "azure",
    "config": {
      "auth_mode": "client_secret",
      "subscription_id": "your-subscription-id",
      "tenant_id": "your-tenant-id",
      "client_id": "your-client-id",
      "client_secret": "your-client-secret"
    }
  }'
```

`auth_mode` defaults to `client_secret` when omitted, so credentials created with earlier versions keep working unchanged.

**Via Web UI:**
1. Navigate to your project
2. Go to **Credentials** tab
3. Click **Add Credential**
4. Select **Azure** as the type
5. Choose **Authentication**: *Host identity* or *Client secret* (then enter the Subscription ID, Tenant ID, Client ID, and Client Secret)
6. In host mode, optionally enter the **Target tenant ID** and **Target app (client) ID**
7. Click **Save**

Octoprox does not call Azure when the credential is saved. The first connector that uses it will surface any permission problem in its error state and in the logs.

## Step 7: Create Azure Connector

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/connectors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Azure East US Proxies",
    "credential_id": "<credential-id-from-step-6>",
    "config": {
      "subscription_id": "your-subscription-id",
      "resource_group": "octoprox-resources",
      "instance_name": "octoprox-proxy",
      "location": "eastus",
      "vm_size": "Standard_B2ls_v2",
      "vnet_name": "octoprox-vnet",
      "subnet_name": "octoprox-subnet",
      "ssh_public_key": "ssh-rsa AAAA... user@host",
      "min_proxies": 1,
      "max_proxies": 10,
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
4. Select your Azure credential
5. Fill in the configuration fields
6. Click **Save**

## Configuration Reference

### Credential

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `auth_mode` | No | `host_identity` or `client_secret` (default) | `host_identity` |
| `subscription_id` | In `client_secret` mode | Subscription the service principal was created for | `xxxxxxxx-...` |
| `tenant_id` | In `client_secret` mode | Service principal's tenant | `xxxxxxxx-...` |
| `client_id` | In `client_secret` mode | Service principal's application (client) ID | `xxxxxxxx-...` |
| `client_secret` | In `client_secret` mode | Service principal's secret | `...` |
| `managed_identity_client_id` | No | Host mode: client ID of the user-assigned identity to use | `xxxxxxxx-...` |
| `target_tenant_id` | With `target_client_id` | Host mode: tenant of the federated app registration | `customer.onmicrosoft.com` |
| `target_client_id` | With `target_tenant_id` | Host mode: application (client) ID of the federated app | `xxxxxxxx-...` |

The subscription VMs are created in always comes from the connector.

### Connector

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `subscription_id` | Yes | Azure subscription ID | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| `resource_group` | Yes | Resource group name | `octoprox-resources` |
| `instance_name` | Yes | Name prefix for VMs | `octoprox-proxy` |
| `location` | Yes | Azure region | `eastus` |
| `vm_size` | Yes | VM size | `Standard_B2ls_v2` |
| `vnet_name` | Yes | Virtual network name | `octoprox-vnet` |
| `subnet_name` | Yes | Subnet name | `octoprox-subnet` |
| `ssh_public_key` | Yes | SSH public key for VM access | `ssh-rsa AAAA... user@host` |
| `min_proxies` | No | Minimum proxy instances (default: 1) | `1` |
| `max_proxies` | No | Maximum proxy instances (default: 10) | `10` |
| `tags` | No | Custom tags for VMs | `{"environment": "prod"}` |

> **Note:** The VM image is automatically selected. Octoprox uses Ubuntu 24.04 LTS for all instances.

To generate an SSH key pair if you don't have one:

```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/octoprox_azure
cat ~/.ssh/octoprox_azure.pub  # Copy this value for ssh_public_key
```

## Troubleshooting

**"AuthorizationFailed" errors:**
- Verify the identity that calls Azure (host identity, federated app, or service principal) has Contributor on the connector's subscription or resource group
- In `client_secret` mode, check that the client_id, client_secret, and tenant_id are correct and the secret has not expired

**"DefaultAzureCredential failed to retrieve a token" with `host_identity`:**
- Confirm the host has an identity: workload identity on AKS, or a managed identity on the VM or Container App
- With several user-assigned identities, set `managed_identity_client_id`
- On AKS, check the pod carries the `azure.workload.identity/use` label and the service account annotation

**"AADSTS70021: No matching federated identity record found" with a target app:**
- The federated credential's issuer must be `https://login.microsoftonline.com/<HOST_TENANT_ID>/v2.0` and its subject the host identity's principal (object) ID
- The audience must be `api://AzureADTokenExchange`
- The target app needs a service principal in the target tenant with the role assignment

**"ResourceNotFound" errors:**
- Verify the resource group exists
- Check that the virtual network and subnet exist in the specified resource group
- Ensure the location matches where your resources are deployed

**"MissingSubscriptionRegistration" errors:**
- Register the required resource providers (see Step 3)
- Wait 1-2 minutes for registration to complete before retrying

**VMs not getting public IPs:**
- Public IPs are created automatically for each VM
- Check that your subscription has sufficient quota for public IP addresses

**Proxy not responding after VM starts:**
- The Squid proxy takes 1-2 minutes to install and start after the VM launches
- Check the NSG allows inbound traffic on port 3128
- SSH into the VM: `az ssh vm --resource-group <rg> --name <vm-name> --local-user octoprox`
- Check cloud-init logs: `cat /var/log/cloud-init-output.log`

