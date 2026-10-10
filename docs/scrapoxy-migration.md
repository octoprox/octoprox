---
layout: docs
title: "Migrating from Scrapoxy (Scrapoxy Alternative)"
nav_id: scrapoxy-migration
description: "Scrapoxy shut down in February 2026. Move to Octoprox, a self-hosted open source alternative: map every Scrapoxy setting to its Octoprox equivalent and update your clients."
---

# Migrating from Scrapoxy

<p class="subtitle">Scrapoxy was discontinued in February 2026. Octoprox does the same job, self-hosted and open source. This guide walks through recreating a Scrapoxy setup by hand: where each value lives in your Scrapoxy storage, what it becomes in Octoprox, and what to change on your clients.</p>

## What happened to Scrapoxy

Scrapoxy's maintainer announced on 6 February 2026 that the project was shutting down after eleven years. For anyone still running it:

- The Docker images were removed from the public registries and the npm package was replaced by a stub, so an instance cannot be reinstalled or upgraded.
- The public documentation went offline.
- The shared backend that every instance called for GeoIP resolution, proxy online checks and country verification was switched off, so a running instance degrades even if it stays up.
- The licence prohibits forks and derivative works, so there is no community continuation.

Octoprox is Apache 2.0, self-hosted, and has no shared backend: IP attribution runs from databases you host and health checks run from your own instance. Nothing in it stops working because a third party goes away.

There is no automated importer. Scrapoxy had no export format, its code is no longer available to test against, and a half-working import would be worse than a clear checklist. A typical Scrapoxy setup is a handful of credentials and connectors, so the manual route below takes well under an hour.

## How the concepts map

| Scrapoxy | Octoprox | Notes |
|----------|----------|-------|
| Project | Project | One proxy endpoint with its own username and password, routing over the connectors below it. |
| Project token | Project username and password | The token is `base64("username:password")`; decode it and reuse both so clients keep working. |
| Credential | Credential | An account at a vendor or cloud, scoped to one project. |
| Connector | Connector | A pool of proxies from one credential. Scrapoxy's `# Proxies` is the connector's proxy count. |
| Free proxies list | Static proxy connector | One proxy row per entry, uploaded as a text list. |
| Free proxies URL sources | - | Octoprox has no periodic list fetch. Upload the list, or add the vendor as a provider descriptor that reads its API. |
| MITM | TLS interception | Scrapoxy MITM is `plain` interception; MITM with user-agent override is `override_ua` with a browser fingerprint. Clients need Octoprox's CA certificate instead of Scrapoxy's. See [TLS Interception](tls-interception). |
| Cookie sessions | `-sessid-` usernames or the `sticky` strategy | Octoprox pins a client to one exit per session id in the proxy username. See [Routing Strategies](routing-strategies). |
| Auto rotate (min/max) | Cloud connector rotation period | Scrapoxy stores milliseconds; Octoprox takes minutes. |
| Auto scale up/down, minimum proxies | Cloud connector min/max proxies | Octoprox scales cloud pools on request rate. See [Deployment & Scaling](deployment). |
| Project status OFF | Disabled connectors | Octoprox has no project-wide switch; connectors are enabled one by one. |
| Proxy port 8888, API 8890 | Proxy port 8080, API and UI on 8000 | Defaults; both configurable. |

## Step 1: read your Scrapoxy configuration

**Single instance (file storage).** Scrapoxy kept everything in one JSON document, `scrapoxy.json`, at the path in `STORAGE_FILE_FILENAME` (`/cfg/scrapoxy.json` in the documented Docker setup, mounted from the host):

```bash
docker cp scrapoxy:/cfg/scrapoxy.json ./scrapoxy.json     # if the container still exists
cp /path/to/scrapoxy/cfg/scrapoxy.json .                   # or from the mounted host directory
```

**Cluster (MongoDB storage).** The same entities live in the `projects`, `credentials`, `connectors` and `freeproxies` collections, linked by `projectId` and `connectorId`. `mongoexport --jsonArray` each one and read them side by side; the field names below are identical.

The document has this shape:

```json
{
  "projects": [{
    "name": "Shop", "status": "HOT", "token": "c2NyYXBlcjpzM2NyZXQ=",
    "mitm": false, "useragentOverride": false, "cookieSession": false,
    "autoRotate": {"enabled": true, "min": 1800000, "max": 3600000},
    "autoScaleUp": true, "autoScaleDown": {"enabled": true, "value": 600000}, "proxiesMin": 2,
    "credentials": [{"id": "...", "name": "Bright Data", "type": "brightdata", "config": {"token": "..."}}],
    "connectors": [{
      "id": "...", "name": "BD residential", "type": "brightdata", "credentialId": "...",
      "active": true, "proxiesMax": 10,
      "config": {"zoneName": "res", "productType": "res_shd", "password": "...", "country": "fr"},
      "freeproxies": [{"type": "http", "address": {"hostname": "203.0.113.10", "port": 8080}, "auth": null}]
    }]
  }]
}
```

This prints an overview of every project, credential and connector, which is all you need for the steps below:

```bash
python3 - <<'PY'
import json
d = json.load(open("scrapoxy.json"))
for p in d["projects"]:
    print(f"Project {p['name']}: status={p.get('status')} mitm={p.get('mitm')} cookieSession={p.get('cookieSession')}")
    for c in p.get("credentials", []):
        print(f"  credential {c['name']!r} type={c['type']} fields={sorted(c.get('config', {}))}")
    for k in p.get("connectors", []):
        print(f"  connector {k['name']!r} type={k['type']} active={k.get('active')} proxiesMax={k.get('proxiesMax')} config={k.get('config')} freeproxies={len(k.get('freeproxies', []))}")
PY
```

Scrapoxy stored vendor credentials in clear text. Treat the file as a secret and delete it when you are done.

## Step 2: create the project

Decode the project token to get the proxy username and password your clients already use:

```bash
echo 'c2NyYXBlcjpzM2NyZXQ=' | base64 -d      # prints username:password
```

In Octoprox, click **New project** and enter the same username and password. Then:

- **TLS interception**: `off` if Scrapoxy's `mitm` was false; `plain` if it was true; `override_ua` if `useragentOverride` was also true. Download the CA certificate from the project's TLS settings for step 5.
- **Routing strategy**: `round_robin` matches Scrapoxy's default rotation. If `cookieSession` was true, read the sticky session section of [Routing Strategies](routing-strategies) and decide between the `sticky` strategy and `-sessid-` usernames; the second lets one project serve many independent sessions.
- Everything else (health checks, timeouts, retries) can stay at the defaults.

## Step 3: recreate credentials and connectors

Work through the connectors in the overview. For each, create its credential under **Credentials**, then the connector under **Connectors**, using the table for the field mapping. Scrapoxy's `proxiesMax` is the connector's proxy count; `country` is the connector's country unless it is `all`; `active: false` means create the connector disabled.

| Scrapoxy type | Octoprox provider | Credential fields | Connector fields |
|---------------|-------------------|-------------------|------------------|
| `freeproxies` | Static proxy list | none | Create the connector, then upload the list (below). |
| `brightdata` | Bright Data | API token: `config.token` | Zone: `zoneName` (the zone password is filled from Bright Data when you pick the zone; Scrapoxy kept it in `config.password`). Product: `productType` starting `res_` is residential, `mob_` mobile, `isp_` ISP, `dc_` datacenter. Country: `country`. |
| `decodo` (older files: `smartproxy`) | Decodo | Product from `config.credentialType` (`residential`, `dc-*` datacenter, `isp-*` ISP), `username`, `password` | Country: `country`. Session duration: `sessionDuration` (minutes). |
| `iproyal-residential` | IPRoyal | `username`, `password` | Country: `country`. Session lifetime: `lifetime` (e.g. `10m`, `24h`). |
| `netnut` | NetNut | `username`, `password`. Product from the connector's `proxyType`: `res` residential, `stc` static residential, `mob` mobile, `dc` datacenter. One Octoprox credential per product. | Country: `country` unless `any`. |
| `oxylabs` | Oxylabs | `username`, `password`, product as named in your Oxylabs dashboard | Country from the connector config. |
| `gcp` | GCP | Project id: `projectId`. Service account JSON: download a fresh key for the account `clientEmail` from the GCP console (Scrapoxy stored the key split into fields). | Zone: `zone`. Machine type: `machineType`. Network: `networkName`. Max proxies: `proxiesMax`; min proxies: the project's `proxiesMin`. Rotation: the project's `autoRotate` min and max divided by 60000. See [GCP Setup](gcp-setup). |
| `aws` | AWS | Access key: `accessKeyId`. Secret: `secretAccessKey`. | Region: `region`. Instance type: `instanceType`. Security group: `securityGroupName`. Octoprox also needs an EC2 key pair name, which Scrapoxy did not use: create one in the region first. See [AWS Setup](aws-setup). |
| `azure` | Azure | Subscription: `subscriptionId`. Tenant: `tenantId`. Client id: `clientId`. Client secret: `secret`. | Location: `location`. VM size: `vmSize`. Resource group: `resourceGroupName`. Octoprox also needs an SSH public key for the VMs. See [Azure Setup](azure-setup). |
| `iproyal-server` | Static proxy list | none | IPRoyal ISP and datacenter orders are IP lists: download the list from the IPRoyal dashboard and upload it to a static connector. |
| `proxy-local`, `datacenter-local` | - | Scrapoxy development connectors; nothing to migrate. | |
| Anything else | A provider descriptor, or a static list | See "Vendors Octoprox does not ship" below. | |

Cloud connectors provision instances the moment they are enabled. Create them disabled, check the settings, then enable.

### Uploading a free proxies list

Open the project's **Proxies** page and use **Upload**, choosing the static connector. The file is one proxy per line, `protocol://[user:pass@]host:port`, with `http`, `https`, `socks4` or `socks5` as the protocol. This converts every free proxy in the Scrapoxy document into that format, one file per connector:

```bash
python3 - <<'PY'
import json, re
d = json.load(open("scrapoxy.json"))
for p in d["projects"]:
    for k in p.get("connectors", []):
        if k["type"] != "freeproxies" or not k.get("freeproxies"):
            continue
        name = re.sub(r"[^A-Za-z0-9]+", "-", f"{p['name']}-{k['name']}").strip("-")
        with open(f"{name}.txt", "w") as out:
            for fp in k["freeproxies"]:
                a, auth = fp["address"], fp.get("auth") or {}
                login = f"{auth['username']}:{auth['password']}@" if auth.get("username") else ""
                out.write(f"{fp['type']}://{login}{a['hostname']}:{a['port']}\n")
        print(name + ".txt", len(k["freeproxies"]), "proxies")
PY
```

If the list had URL sources (`sources` in the connector), fetch the current list from each URL and upload it the same way.

## Step 4: vendors Octoprox does not ship

Scrapoxy had connectors for Evomi, Geonode, HypeProxy, Live Proxies, Massive, Nimbleway, Ninjas Proxy, Proxidize, Proxy-Cheap, Proxy-Seller, Proxyrack, Rayobyte, XProxy and Zyte, plus DigitalOcean, OVH, Scaleway and Tencent for cloud instances. Octoprox ships Oxylabs, Bright Data, Decodo, Webshare, IPRoyal and NetNut, and the three big clouds.

Any proxy vendor can be added without code or a redeploy. A provider is a YAML descriptor: which fields a credential and a connector need, how they turn into a gateway host, port, username and password, and which vendor API calls validate the credential or list options. Most residential and datacenter vendors fit this shape, because they all hand out a gateway with targeting parameters encoded in the username.

- **An admin on your instance** adds it under **Settings → Providers**, tests it with throwaway credentials before saving, and it is live for every project immediately. The Bright Data descriptor exercises nearly every feature and can be exported as a starting point. The format is documented in [Providers & the Provider SDK](providers).
- **An operator** can also mount descriptors as files (`OCTOPROX_PROVIDERS_DIR`) so they ship with the deployment.
- **Ask for one.** Open an issue on [GitHub](https://github.com/octoprox/octoprox/issues) naming the vendor and linking its proxy documentation. Descriptors are small, and ones that are generally useful are added to the shipped set.
- Vendors that deliver a fixed list of IPs rather than a gateway work today through a static proxy connector.

The Scrapoxy document tells you what the vendor needs: `fields` in the overview are the credential keys Scrapoxy asked for, and the connector `config` shows the targeting options (country, product, session duration) a descriptor should expose.

## Step 5: repoint your clients

1. **Endpoint.** Replace Scrapoxy's host and port (`localhost:8888` by default) with Octoprox's proxy port (`8080` by default). The username and password are the ones you decoded in step 2.
2. **CA certificate.** If the project uses TLS interception, install Octoprox's CA certificate where Scrapoxy's was.
3. **Sticky sessions.** Where you relied on cookie sessions, add `-sessid-<id>` to the proxy username per session. One project can also target a country per request with `-cc-<code>`.
4. **Integrations.** Scrapoxy's Scrapy middleware, Puppeteer plugin and similar talked to its own API to wait for scaling or to inspect proxies. Octoprox needs none of them: configure the proxy URL as you would for any HTTP or SOCKS proxy. Pool status, metrics and the request inspector are in the web UI and the [REST API](api).
5. **Metrics.** Scrapoxy's counters do not carry over. Octoprox records latency, success rate, traffic and exit locations from the first request through it.

## What you gain

- **No shared backend.** Exit locations come from MaxMind, DB-IP, IPinfo or IP2Location databases you host, with vendor claims verified against them. See [IP Attribution](ip-attribution).
- **Traffic caps and spend.** Per-connector byte limits per billing period, alert, block or interrupt at the cap, and spend from your price per GB. See [Traffic Limits & Billing](traffic-limits).
- **Devices without proxy settings.** Routers, TVs, consoles and phones join a pool through a WireGuard or OpenVPN tunnel Octoprox terminates. See [WireGuard](wireguard) and [OpenVPN](openvpn).
- **Clustering.** Several instances behind one load balancer sharing Postgres and Redis. See [Deployment & Scaling](deployment).

If a Scrapoxy feature you depended on is missing, open an issue on [GitHub](https://github.com/octoprox/octoprox/issues) and say what it did for you.
