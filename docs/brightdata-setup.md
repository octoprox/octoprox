---
layout: docs
title: BrightData Connector Setup
nav_id: brightdata-setup
---

# BrightData Connector Setup

> ⚠️ **Warning:** The BrightData integration is currently in beta and has not been fully tested in production environments. You may encounter issues or unexpected behavior. Please report any problems you encounter.

The BrightData Connector allows Octoprox to use BrightData proxy services including residential, mobile, ISP, and datacenter proxies. This guide covers how to configure the connector with your BrightData credentials.

## Prerequisites

- A BrightData account with an active subscription
- Your BrightData API token (available from the BrightData dashboard)
- At least one active zone configured in your BrightData account

## Supported Proxy Types

BrightData offers several proxy types through zones:

| Proxy Type | Description | Zone Type Prefix |
|------------|-------------|------------------|
| `residential` | Residential IPs from real devices | `res*` |
| `mobile` | Mobile carrier IPs | `mobile*` |
| `isp` | ISP (static residential) proxies | `isp*` |
| `datacenter` | Datacenter proxies | `dc*` |

All proxy types support country-based geo-targeting.

Octoprox routes traffic through BrightData's super proxy endpoint at `brd.superproxy.io:44445`. If you restrict outbound traffic from your Octoprox hosts, allow this host and port. BrightData has retired the previous port `33335`; Octoprox migrates existing proxies to the new port automatically on startup.

## Step 1: Get Your BrightData API Token

1. **Log in to your BrightData dashboard** at [brightdata.com](https://brightdata.com)

2. **Navigate to Account Settings** and locate your API token

3. **Copy your API token** - this will be used to authenticate with the BrightData API

4. **Ensure you have active zones** configured for the proxy types you want to use

## Step 2: Create BrightData Credential in Octoprox

Using the Octoprox API or web UI, create a credential with your BrightData API token:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/credentials \
  -H "Content-Type: application/json" \
  -d '{
    "name": "BrightData Account",
    "type": "brightdata",
    "config": {
      "token": "your-brightdata-api-token"
    }
  }'
```

> **Note:** The `customer_id` is automatically fetched from the BrightData API when you create the credential.

**Via Web UI:**
1. Navigate to your project
2. Go to **Credentials** tab
3. Click **Add Credential**
4. Select **BrightData** as the type
5. Enter your BrightData API token
6. Click **Save** - the token will be validated automatically

## Step 3: Create BrightData Connector

Create a connector that uses your BrightData credential. You'll need to select one of your active zones:

**Via API:**

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/connectors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "BrightData US Residential",
    "credential_id": "<credential-id-from-step-2>",
    "config": {
      "zone_name": "your-zone-name",
      "zone_password": "zone-password",
      "proxy_type": "residential",
      "num_proxies": 10,
      "country_code": "US"
    }
  }'
```

**Via Web UI:**
1. Navigate to your project
2. Go to **Connectors** tab
3. Click **Add Connector**
4. Select your BrightData credential
5. Choose a zone from the dropdown (zones are fetched automatically)
6. Configure the number of proxies and optional geo-targeting
7. Click **Save**

## Configuration Reference

### Credential Configuration

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `token` | Yes | BrightData API token | `abc123...` |

### Connector Configuration

| Field | Required | Description | Default |
|-------|----------|-------------|---------|
| `zone_name` | Yes | Name of the BrightData zone to use | - |
| `zone_password` | Yes | Password for the zone (auto-populated from API) | - |
| `proxy_type` | Yes | Type of proxy (derived from zone) | - |
| `num_proxies` | No | Number of proxy slots to create (1-1000) | `1` |
| `country_code` | No | 2-letter country code for geo-targeting | `null` (all countries) |
| `healthcheck_url` | No | Custom URL for proxy health checks | `https://httpbin.org/ip` |

## Troubleshooting

**"Token validation failed" errors:**
- Verify your BrightData API token is correct
- Check that your BrightData account is active
- Ensure the token has not expired or been revoked

**No zones appearing in the dropdown:**
- Verify you have active zones configured in your BrightData dashboard
- Check that your zones are of supported types (residential, mobile, ISP, datacenter)
- Ensure each zone has a password configured

**Proxies showing as unhealthy:**
- Check your BrightData dashboard for any usage limits or restrictions
- Verify the zone is active and has available bandwidth
- Try using a different healthcheck URL if the default is blocked

**Geo-targeting not working:**
- Verify the country code is a valid 2-letter ISO code (e.g., `US`, `GB`, `DE`)
- Check that your zone supports the requested country

## Known Limitations

Since this integration is in beta, please be aware of the following:

1. **Session management** - Session-based proxies (residential, mobile) may not maintain sessions as expected in all scenarios
2. **Error handling** - Some BrightData API errors may not be handled gracefully
3. **Rate limiting** - The integration does not currently implement rate limiting for BrightData API calls
4. **Zone types** - Only residential, mobile, ISP, and datacenter zones are supported; other zone types are ignored

If you encounter any issues, please report them with detailed logs and reproduction steps.

