---
layout: default
title: Azure Connector Setup
---

# Azure Connector Setup

The Azure Connector allows Octoprox to dynamically provision Azure Virtual Machines as proxy servers. This guide covers how to obtain the required Azure credentials and configure the connector.

## Prerequisites

- An Azure account with an active subscription
- A resource group for Octoprox resources

## Step 1: Create a Service Principal for Octoprox

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

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
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
1. Navigate to your project
2. Go to **Credentials** tab
3. Click **Add Credential**
4. Select **Azure** as the type
5. Enter your Subscription ID, Tenant ID, Client ID, and Client Secret
6. Click **Save**

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
- Verify the service principal has Contributor role on the subscription or resource group
- Check that the client_id, client_secret, and tenant_id are correct
- Ensure the service principal secret has not expired

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

