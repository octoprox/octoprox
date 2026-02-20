# AWS Connector Setup

The AWS Connector allows Octoprox to dynamically provision EC2 instances as proxy servers. This guide covers how to obtain the required AWS credentials and configure the connector.

## Prerequisites

- An AWS account with permissions to create and manage EC2 instances

## Step 1: Create an IAM User for Octoprox

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

Using the Octoprox API or web UI, create a credential with your AWS access keys:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
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
1. Navigate to your project
2. Go to **Credentials** tab
3. Click **Add Credential**
4. Select **AWS** as the type
5. Enter your Access Key ID and Secret Access Key
6. Click **Save**

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

