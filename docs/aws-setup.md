---
layout: docs
title: "AWS Connector: EC2 Proxy Setup"
nav_id: aws-setup
description: "Set up the AWS connector so Octoprox provisions EC2 instances as proxy servers: IAM roles or access keys, permissions and connector configuration."
---

# AWS Connector Setup

The AWS Connector allows Octoprox to dynamically provision EC2 instances as proxy servers. This guide covers how to grant Octoprox access to your AWS account and configure the connector.

## Prerequisites

- An AWS account with permissions to create IAM roles or users and to manage EC2 instances

## Step 1: Choose How Octoprox Authenticates

An AWS credential in Octoprox has an **authentication mode** and an optional **role to assume**:

| Mode | What is stored | When to use it |
|------|----------------|----------------|
| `host_role` | Nothing | Octoprox runs on AWS (EC2, ECS, EKS) and gets its identity from an IAM role attached to the host. Recommended. |
| `access_keys` | An IAM user's access key and secret key, encrypted at rest | Octoprox runs outside AWS, or you cannot attach a role to the host. |

In either mode you can add a `role_arn`. Octoprox then calls `sts:AssumeRole` from the base identity and provisions EC2 with the role's temporary credentials, which it refreshes before they expire. This is how you let Octoprox manage proxies in a separate account, or in a customer's account, without that account issuing any long-lived keys. An optional `external_id` is sent with the AssumeRole call for [confused deputy protection](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html).

### 1.1 The EC2 permissions policy

Whichever identity ends up calling EC2 (the host role, the assumed role, or the IAM user) needs this policy:

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

### 1.2 Option A: Host IAM role (recommended)

Create an IAM role with the policy above and attach it to whatever runs Octoprox:

- **EC2:** create the role with the EC2 trust policy (`ec2.amazonaws.com`) and attach it as the instance profile.
- **ECS / Fargate:** use it as the task role (`ecs-tasks.amazonaws.com`).
- **EKS:** bind it to the Octoprox service account with EKS Pod Identity or IRSA.

Octoprox picks the role up through the AWS SDK's default credential chain, so no Octoprox-side configuration is needed beyond choosing the mode. The same chain also honours `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` environment variables on the host, and `AWS_PROFILE` with a mounted `~/.aws` directory, if you would rather supply keys to the process than store them in Octoprox.

> **Docker on EC2:** containers on a bridge network are one hop further from the instance metadata service than the host. If the credential fails with "Unable to locate credentials", raise the IMDS hop limit on the instance: `aws ec2 modify-instance-metadata-options --instance-id <id> --http-put-response-hop-limit 2`. Containers using `network_mode: host` are not affected.

Credential config: `{"auth_mode": "host_role"}`.

### 1.3 Option B: Host role assuming a role in another account

Use this when the proxies should live in a different AWS account from the one Octoprox runs in. The host role stays small; the EC2 permissions live in the target account.

1. **In the target account**, create a role (for example `octoprox-proxies`) with the EC2 policy from 1.1 and a trust policy that allows the Octoprox host role to assume it. Pick an external ID of your own choosing; it is a shared value, not a secret.

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": {
           "AWS": "arn:aws:iam::111111111111:role/octoprox-host"
         },
         "Action": "sts:AssumeRole",
         "Condition": {
           "StringEquals": {
             "sts:ExternalId": "acme-7f3b9c"
           }
         }
       }
     ]
   }
   ```

2. **In the Octoprox account**, allow the host role to assume it:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "sts:AssumeRole",
         "Resource": "arn:aws:iam::222222222222:role/octoprox-proxies"
       }
     ]
   }
   ```

Credential config: `{"auth_mode": "host_role", "role_arn": "arn:aws:iam::222222222222:role/octoprox-proxies", "external_id": "acme-7f3b9c"}`.

One Octoprox installation can hold one such credential per customer or environment account, all backed by the same host role.

### 1.4 Option C: IAM user access keys

1. **Sign in to the AWS Console** and navigate to **IAM** (Identity and Access Management).

2. **Create a new IAM user:**
   - Go to **Users** → **Create user**
   - Enter a username (e.g., `octoprox-service`)
   - Select **Programmatic access** to generate access keys

3. **Attach permissions:** either the EC2 policy from 1.1 directly, or only `sts:AssumeRole` on a role that carries the EC2 policy (the role's trust policy must then name the user as a principal, as in 1.3).

4. **Generate access keys:**
   - After creating the user, go to **Security credentials** tab
   - Click **Create access key**
   - Select **Application running outside AWS**
   - Download or copy the **Access Key ID** and **Secret Access Key**

   > **Important:** Store these credentials securely. The secret key is only shown once.

Credential config: `{"auth_mode": "access_keys", "access_key": "...", "secret_key": "...", "role_arn": "<optional>"}`.

## Step 2: Prepare AWS Resources

### 2.1 Create a Security Group

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

### 2.2 Create an EC2 Key Pair

Create a key pair for SSH access to the proxy instances:

```bash
aws ec2 create-key-pair \
  --key-name octoprox-key \
  --query 'KeyMaterial' \
  --output text > octoprox-key.pem

chmod 400 octoprox-key.pem
```

Note the key pair name (e.g., `octoprox-key`).

## Step 3: Create AWS Credential in Octoprox

Using the Octoprox API or web UI, create a credential for the mode you chose in Step 1.

**Via API, host role (Option A):**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "AWS Production",
    "type": "aws",
    "config": {
      "auth_mode": "host_role"
    }
  }'
```

**Via API, host role assuming a role in another account (Option B):**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "AWS Customer Account",
    "type": "aws",
    "config": {
      "auth_mode": "host_role",
      "role_arn": "arn:aws:iam::222222222222:role/octoprox-proxies",
      "external_id": "acme-7f3b9c"
    }
  }'
```

**Via API, access keys (Option C):**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "AWS Production",
    "type": "aws",
    "config": {
      "auth_mode": "access_keys",
      "access_key": "AKIAIOSFODNN7EXAMPLE",
      "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    }
  }'
```

`auth_mode` defaults to `access_keys` when omitted, so credentials created with earlier versions keep working unchanged.

**Via Web UI:**
1. Navigate to your project
2. Go to **Credentials** tab
3. Click **Add Credential**
4. Select **AWS** as the type
5. Choose **Authentication**: *Host IAM role* or *Access keys* (then enter the Access Key ID and Secret Access Key)
6. Optionally enter a **Role to assume** and its **External ID**
7. Click **Save**

Octoprox does not call AWS when the credential is saved. The first connector that uses it will surface any IAM problem in its error state and in the logs.

## Step 4: Create AWS Connector

Create a connector that uses your AWS credential:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/connectors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "AWS US-East Proxies",
    "credential_id": "<credential-id-from-step-3>",
    "config": {
      "instance_name": "octoprox-proxy",
      "region": "us-east-1",
      "instance_type": "t3.micro",
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
1. Navigate to your project
2. Go to **Connectors** tab
3. Click **Add Connector**
4. Select your AWS credential
5. Fill in the configuration fields
6. Click **Save**

## Configuration Reference

### Credential

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `auth_mode` | No | `host_role` or `access_keys` (default) | `host_role` |
| `access_key` | In `access_keys` mode | IAM user access key ID | `AKIAIOSFODNN7EXAMPLE` |
| `secret_key` | In `access_keys` mode | IAM user secret access key | `wJalr...` |
| `role_arn` | No | Role assumed with `sts:AssumeRole` before calling EC2 | `arn:aws:iam::222222222222:role/octoprox-proxies` |
| `external_id` | No | `ExternalId` for the AssumeRole call; requires `role_arn` | `acme-7f3b9c` |

The AssumeRole session is named `octoprox-<credential id>`, which is what appears in the target account's CloudTrail.

### Connector

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `instance_name` | Yes | Name prefix for EC2 instances | `octoprox-proxy` |
| `region` | Yes | AWS region for instances | `us-east-1` |
| `instance_type` | Yes | EC2 instance type | `t3.micro` |
| `security_group` | Yes | Security group ID | `sg-0123456789abcdef0` |
| `key_pair_name` | Yes | EC2 key pair name | `octoprox-key` |
| `min_proxies` | No | Minimum proxy instances (default: 1) | `1` |
| `max_proxies` | No | Maximum proxy instances (default: 10) | `10` |
| `min_rotation_period_minutes` | No | Minimum instance lifetime (default: 60) | `60` |
| `max_rotation_period_minutes` | No | Maximum instance lifetime (default: 1440) | `1440` |
| `tags` | No | Custom tags for instances | `{"Environment": "prod"}` |

> **Note:** The AMI is automatically selected based on region and instance type. Octoprox uses Ubuntu 24.04 LTS for all instances.

## Troubleshooting

**"Access Denied" errors:**
- Verify the identity that calls EC2 (host role, assumed role or IAM user) has the required EC2 permissions
- In `access_keys` mode, check that the access key and secret key are correct
- Ensure the identity is not restricted by SCPs or permission boundaries

**"Unable to locate credentials" with `host_role`:**
- Confirm a role is attached to the host (instance profile, ECS task role, or EKS pod identity / IRSA)
- For Docker on EC2, raise the IMDS hop limit as described in Option A
- Octoprox reads the credential chain from the process environment, so `AWS_*` variables must be set on the Octoprox container, not only on the host shell

**"is not authorized to perform: sts:AssumeRole" with a `role_arn`:**
- The target role's trust policy must name the base identity (host role ARN or IAM user ARN) as a principal
- If the trust policy has an `sts:ExternalId` condition, the credential's `external_id` must match it exactly
- The base identity needs an `sts:AssumeRole` allow on the target role ARN
- Role chaining (a role assumed from another assumed role) is limited by AWS to one-hour sessions; Octoprox refreshes before expiry, so this only matters for very slow instance launches

**Instances not getting public IPs:**
- Ensure your subnet has "Auto-assign public IPv4 address" enabled

**Proxy not responding after instance starts:**
- The Squid proxy takes 1-2 minutes to install and start after the instance launches
- Check the security group allows inbound traffic on port 3128
- SSH into the instance to check Squid status: `ssh -i octoprox-key.pem ubuntu@<instance-ip>` then `systemctl status squid`
- Check the startup script logs: `cat /var/log/cloud-init-output.log`

