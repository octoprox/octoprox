---
layout: docs
title: TLS Interception
nav_id: tls-interception
---

# TLS Interception (MITM)

<p class="subtitle">Decrypt and re-encrypt HTTPS traffic for header inspection and browser-grade TLS fingerprint impersonation.</p>

Octoprox can decrypt and re-encrypt HTTPS traffic between your client and the target server. This enables header inspection, User-Agent overriding, and browser-grade TLS fingerprint impersonation. TLS interception is configured per-project from the web UI.

## Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| **Disabled** | Traffic forwarded through an encrypted tunnel as-is. The proxy cannot inspect headers or content. Your client's TLS fingerprint, User-Agent, and all headers reach the target unchanged. | You handle anti-detection yourself. |
| **Plain** | Decrypts HTTPS to inspect headers, then re-encrypts using Python's standard TLS library. The target sees a Python/OpenSSL fingerprint — easily detectable by anti-bot systems. | Debugging and development only. |
| **Browser Match** | Decrypts HTTPS for inspection, then re-encrypts using a browser-grade TLS engine that matches the client's User-Agent. If your client sends a Chrome User-Agent, the target sees a Chrome TLS fingerprint. | Production scraping with basic anti-detection. |
| **Browser Override** | Same as Browser Match, but also replaces the client's User-Agent with the selected browser's default. TLS fingerprint and User-Agent are guaranteed consistent. | Maximum anti-detection when you don't need to control the User-Agent. |

## TLS Engines

When using Browser Match or Browser Override mode, you can choose between two TLS engines:

| Engine | Description |
|--------|-------------|
| **curl_cffi** | C/libcurl with BoringSSL. Mature, Chrome-grade fingerprints. |
| **rnet** | Rust/BoringSSL. Fast, 113+ browser profiles. |

## Browser Profiles

When using Browser Override mode, you choose which browser to impersonate:

| Profile | Description |
|---------|-------------|
| **Chrome** | Most common browser fingerprint, lowest detection risk. |
| **Firefox** | Alternative fingerprint for diversity. |
| **Safari** | macOS/iOS fingerprint. |
| **Edge** | Chromium-based, Windows-like fingerprint. |
| **Random** | Randomly selects a different browser profile for each request. Useful for fingerprint diversity across a large volume of requests. |

In Browser Match mode, the browser profile is automatically detected from the client's User-Agent header. If the User-Agent doesn't match any known browser, the engine defaults to Chrome and replaces the unrecognized User-Agent with Chrome's default.

## How It Works

TLS interception inherently creates two separate TLS sessions:

1. **Client &rarr; Proxy**: Your client connects to the proxy via TLS. The proxy presents a dynamically generated certificate for the target domain, signed by the Octoprox CA.
2. **Proxy &rarr; Target**: The proxy opens a new TLS connection to the target server using the selected engine. The target sees the engine's TLS fingerprint, not your client's.

This means the upstream TLS fingerprint (JA3/JA4) is always determined by the relay engine, never by your client. Your client's original fingerprint is not and cannot be passed through.

## Protocol Support

When MITM is **disabled**, Octoprox creates a raw TCP tunnel (`CONNECT`) and is completely protocol-agnostic — HTTP/2, HTTP/3 (QUIC), WebSocket, and any other protocol work transparently.

When MITM is **enabled**, the proxy terminates TLS and parses traffic itself. The current implementation uses [h11](https://h11.readthedocs.io/) on the client-facing side, which limits interception to **HTTP/1.1** only. On the relay side, the impersonation engines (`curl_cffi`, `rnet`) negotiate HTTP/2 with the target server automatically.

WebSocket and HTTP/2 client-side support are planned for a future release.

## CA Certificate

When any MITM mode is enabled, clients must trust the Octoprox CA certificate. Without it, clients will see TLS verification errors.

**Auto-generation**: The CA certificate and private key are automatically generated on first startup if they don't already exist. They are stored at the paths configured by `OCTOPROX_TLS_MITM_CA_CERT_PATH` and `OCTOPROX_TLS_MITM_CA_KEY_PATH` (default: `data/ca/`).

**Download**: The CA certificate can be downloaded from the web UI (shown when editing a project with MITM enabled) or directly at:

```
GET /api/v1/projects/ca-certificate
```

**Install the CA certificate** in your client's trust store:

```bash
# Download the certificate
curl -o octoprox-ca.crt http://localhost:8000/api/v1/projects/ca-certificate

# Python (requests/httpx)
export REQUESTS_CA_BUNDLE=/path/to/octoprox-ca.crt
export SSL_CERT_FILE=/path/to/octoprox-ca.crt

# curl
curl --cacert /path/to/octoprox-ca.crt https://example.com

# Node.js
export NODE_EXTRA_CA_CERTS=/path/to/octoprox-ca.crt

# System-wide (macOS)
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain octoprox-ca.crt

# System-wide (Ubuntu/Debian)
sudo cp octoprox-ca.crt /usr/local/share/ca-certificates/octoprox-ca.crt
sudo update-ca-certificates
```

**Bring your own CA**: To use a custom CA certificate instead of the auto-generated one, place your PEM-encoded certificate and private key at the configured paths before starting the server. The files must be readable by the Octoprox process.

```bash
# Example: provide your own CA
mkdir -p data/ca
cp /path/to/my-ca.crt data/ca/octoprox-ca.crt
cp /path/to/my-ca.key data/ca/octoprox-ca.key
chmod 600 data/ca/octoprox-ca.key
```

Or generate a new CA manually with OpenSSL:

```bash
# Generate a CA private key
openssl genrsa -out data/ca/octoprox-ca.key 2048
chmod 600 data/ca/octoprox-ca.key

# Generate a self-signed CA certificate (valid for 10 years)
openssl req -new -x509 -key data/ca/octoprox-ca.key \
  -out data/ca/octoprox-ca.crt -days 3650 \
  -subj "/O=Octoprox/CN=Octoprox MITM CA"
```

## Docker

In Docker deployments, the CA files are persisted in a named volume (`ca_data`) mounted at `/app/data/ca`. This ensures the CA certificate survives container restarts — clients only need to install it once.

## YAML Configuration

The CA certificate paths can also be set via YAML:

```yaml
tls_mitm:
  ca_cert_path: "data/ca/octoprox-ca.crt"
  ca_key_path: "data/ca/octoprox-ca.key"
```

## Security Notes

- **Never commit CA private keys** to version control. The `.gitignore` excludes `data/` and `*.key` by default.
- The CA private key allows anyone who has it to generate trusted certificates for any domain. Treat it like a password.
- CA certificates are valid for 10 years. Domain certificates generated for individual hosts are valid for 1 year.
- In production, consider using a dedicated CA with restricted access rather than the auto-generated one.
