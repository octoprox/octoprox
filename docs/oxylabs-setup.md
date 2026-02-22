---
layout: docs
title: Oxylabs Connector Setup
nav_id: oxylabs-setup
---

# Oxylabs Connector Setup

The Oxylabs Connector allows Octoprox to use Oxylabs proxy services including residential, mobile, ISP, and datacenter proxies. This guide covers how to configure the connector with your Oxylabs credentials.

## Prerequisites

- An Oxylabs account with an active subscription
- Your Oxylabs username and password

## Supported Proxy Types

Oxylabs offers several proxy types, each with different characteristics:

| Proxy Type | Description | Routing Mode |
|------------|-------------|--------------|
| `residential` | Residential IPs from real devices | Session-based |
| `mobile` | Mobile carrier IPs | Session-based |
| `isp` | ISP (static residential) proxies | Port-based |
| `dedicated_isp` | Dedicated ISP proxies | Port-based |
| `datacenter` | Shared datacenter proxies | Port-based |
| `datacenter_dedicated` | Dedicated datacenter proxies | Port-based |

**Session-based proxies** (residential, mobile) use random session IDs to maintain sticky sessions. Each session provides a consistent IP for the configured duration.

**Port-based proxies** (ISP, datacenter) use sequential ports where each port corresponds to a dedicated IP address.

## Step 1: Get Your Oxylabs Credentials

1. **Log in to your Oxylabs dashboard** at [dashboard.oxylabs.io](https://dashboard.oxylabs.io)

2. **Locate your credentials.**

3. **Verify your subscription** includes the proxy type you want to use

## Step 2: Create Oxylabs Credential in Octoprox

Using the Octoprox API or web UI, create a credential with your Oxylabs account:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Oxylabs Residential",
    "type": "oxylabs",
    "config": {
      "proxy_type": "residential",
      "username": "your-oxylabs-username",
      "password": "your-oxylabs-password"
    }
  }'
```

**Via Web UI:**
1. Navigate to your project
2. Go to **Credentials** tab
3. Click **Add Credential**
4. Select **Oxylabs** as the type
5. Choose your proxy type (residential, mobile, isp, etc.)
6. Enter your Oxylabs username and password
7. Click **Save**

## Step 3: Create Oxylabs Connector

Create a connector that uses your Oxylabs credential:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/connectors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Oxylabs US Residential",
    "credential_id": "<credential-id-from-step-2>",
    "config": {
      "num_proxies": 10,
      "country_code": "US",
      "session_duration_minutes": 10
    }
  }'
```

**Via Web UI:**
1. Navigate to your project
2. Go to **Connectors** tab
3. Click **Add Connector**
4. Select your Oxylabs credential
5. Fill in the configuration fields
6. Click **Save**

## Configuration Reference

### Credential Configuration

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `proxy_type` | Yes | Type of Oxylabs proxy | `residential` |
| `username` | Yes | Oxylabs account username | `user123` |
| `password` | Yes | Oxylabs account password | `secretpass` |

### Connector Configuration

| Field | Required | Description | Default |
|-------|----------|-------------|---------|
| `num_proxies` | No | Number of proxy slots to create (1-1000) | `1` |
| `country_code` | No | 2-letter country code for geo-targeting (session-based only) | `null` (all countries) |
| `session_duration_minutes` | No | Session duration in minutes (1-30, session-based only) | `10` |

> **Note:** `country_code` and `session_duration_minutes` only apply to session-based proxy types (residential, mobile). They are ignored for port-based types.

## Proxy Type Endpoints

Octoprox automatically configures the correct Oxylabs endpoint based on your proxy type:

| Proxy Type | Endpoint | Default Port |
|------------|----------|--------------|
| `residential` | pr.oxylabs.io | 7777 |
| `mobile` | pr.oxylabs.io | 7777 |
| `isp` | isp.oxylabs.io | 8001 |
| `dedicated_isp` | disp.oxylabs.io | 8001 |
| `datacenter` | dc.oxylabs.io | 8001 |
| `datacenter_dedicated` | ddc.oxylabs.io | 8001 |

## Troubleshooting

**"Authentication failed" errors:**
- Verify your Oxylabs username and password are correct
- Check that your subscription is active and includes the selected proxy type
- Ensure you're using the correct proxy type for your subscription

**Proxies showing as unhealthy:**
- For port-based proxies, ensure you have enough IPs allocated in your Oxylabs account
- Check your Oxylabs dashboard for any usage limits or restrictions

**Geo-targeting not working:**
- Geo-targeting only works with session-based proxies (residential, mobile)
- Verify the country code is a valid 2-letter ISO code (e.g., `US`, `GB`, `DE`)

**Session IPs changing unexpectedly:**
- Session duration is limited to 30 minutes maximum
- Increase `session_duration_minutes` if you need longer sessions

