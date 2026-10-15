---
layout: docs
title: "Connecting Clients"
nav_id: connecting
description: "Point an HTTP client at an Octoprox project: the proxy endpoint, the username with its country, state, city and session, and ready-made examples for curl, Python, Node.js and Playwright."
---

# Connecting Clients

<p class="subtitle">Send requests through a project's proxy, with a country, state, city or sticky session chosen per request.</p>

Every project is one proxy endpoint with its own credentials. A client points its HTTP proxy setting at Octoprox, authenticates with the project's username and password, and adds routing parameters to the username to choose where the request exits. The **Connect** page in each project (under Tools) builds all of this for you: pick a place and a session and it shows the credentials, the proxy URL, whether the project can serve that place right now, and code to copy.

## Endpoint

Octoprox speaks plain HTTP on its proxy port (`8080` by default, `server.proxy_port`): HTTP requests are forwarded and HTTPS goes through a `CONNECT` tunnel. Reached directly, the proxy URL starts with `http://`, even when every target is `https://`.

A load balancer that terminates TLS in front of the proxy port makes it an **HTTPS proxy**: clients use an `https://` proxy URL, and the connection to the proxy, the `Proxy-Authorization` credentials included, is encrypted. The balancer forwards the decrypted stream to the proxy port unchanged (a TCP or TLS stream listener, not an HTTP one, since `CONNECT` tunnels must pass through). curl, Python requests, undici and Chromium (Playwright) all take `https://` proxy URLs, with or without a port; without one they connect to 443.

| What | Value |
|------|-------|
| Protocol | HTTP, or HTTPS behind a load balancer that terminates TLS |
| Host | The address clients reach Octoprox at, usually the host of the web UI |
| Port | `8080`, or what the load balancer publishes; may be left out for HTTPS on 443 (curl reads a bare `http://` proxy as port 1080, so an HTTP URL names its port) |
| Username | The project's username, plus routing parameters |
| Password | The project's password |

Behind a load balancer or a NAT the proxy is published under another address than the instance listens on. Set `OCTOPROX_PROXY_PUBLIC_URL` (see [Configuration]({{ site.baseurl }}/configuration#environment-variables)) to the URL clients should use, for example `https://proxy.example.com` or `https://proxy.example.com:8443`: the Connect page and its examples use it as given, leaving the port out when an `https://` URL has none (an `http://` one without a port gets `80`). Without it the page shows `http://` on the host the browser reached the web UI at and the proxy port.

## Username parameters

Routing parameters are appended to the project username as `-<key>-<value>` segments, in any order. The password never changes.

| Parameter | Example | Effect |
|-----------|---------|--------|
| `-cc-<country>` | `-cc-us` | Exit in a country (ISO 3166-1 alpha-2, `uk` is accepted for `gb`). See [Country routing]({{ site.baseurl }}/routing-strategies#country-routing) |
| `-st-<state>` | `-st-ny` | Exit in a state, province or other first-level subdivision (ISO 3166-2 code). Needs `-cc-` |
| `-city-<city>` | `-city-new_york` | Exit in a city, as a slug: lower case, underscores for spaces. Needs `-cc-` |
| `-sessid-<id>` | `-sessid-order-42` | Keep the same exit across requests. See [Session IDs]({{ site.baseurl }}/routing-strategies#session-ids) |

```
myuser-cc-us-st-ny-city-new_york-sessid-order-42
```

The Connect page normalises what you type the way the proxy does (`New York`, `new-york` and `nyc` all become `new_york`) and shows the username in that form. A session ID must not contain one of the reserved delimiters (`-cc-`, `-st-`, `-city-`, `-sessid-`), nor start with one without its first dash (`-sessid-st-ny` reads as `-st-ny`), and must not contain a colon, since Basic auth splits the username from the password at the first colon. Without `-sessid-` each request is routed on its own; with it, the [sticky strategy]({{ site.baseurl }}/routing-strategies#session-ids) keeps the session on one proxy, and a [dynamic-sessions]({{ site.baseurl }}/providers#descriptor-reference) connector keeps one vendor session for it under every strategy.

## Examples

The examples fetch [ipinfo.io](https://ipinfo.io/json), which answers with the exit IP and where it is, so the targeting can be seen taking effect. Replace the credentials with your project's.

**curl**

```bash
curl --proxy 'http://proxy.example.com:8080' \
  --proxy-user 'myuser-cc-us-st-ny:mypassword' \
  'https://ipinfo.io/json'
```

**Python** (requests)

```python
import requests

proxy = "http://myuser-cc-us-st-ny:mypassword@proxy.example.com:8080"
proxies = {"http": proxy, "https": proxy}

response = requests.get("https://ipinfo.io/json", proxies=proxies, timeout=30)
print(response.text)
```

**Node.js** (undici)

```javascript
// npm install undici
import { ProxyAgent, fetch } from 'undici'

const dispatcher = new ProxyAgent('http://myuser-cc-us-st-ny:mypassword@proxy.example.com:8080')
const response = await fetch('https://ipinfo.io/json', { dispatcher })
console.log(await response.text())
```

**Playwright** (Python)

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(proxy={
        "server": "http://proxy.example.com:8080",
        "username": "myuser-cc-us-st-ny",
        "password": "mypassword",
    })
    page = browser.new_page()
    page.goto("https://ipinfo.io/json")
    print(page.inner_text("body"))
    browser.close()
```

**Environment variables**

```bash
export https_proxy='http://myuser-cc-us-st-ny:mypassword@proxy.example.com:8080'
export http_proxy="$https_proxy"
curl 'https://ipinfo.io/json'
```

In the URL form the username and password are percent-encoded: a password containing `@`, `:` or `/` would otherwise end the credentials early. The Connect page does this for you; curl's `--proxy-user` and Playwright's separate fields need no encoding. Many scraping tools also import the `host:port:username:password` line the page offers.

### With TLS interception

When the project's [TLS interception]({{ site.baseurl }}/tls-interception) is on, clients must trust the Octoprox CA. Download it from the Connect page (or `GET /api/v1/projects/ca-certificate`) as `octoprox-ca.crt`, then pass `--cacert octoprox-ca.crt` to curl, `verify="octoprox-ca.crt"` to requests, or `requestTls: { ca: readFileSync('octoprox-ca.crt') }` to undici's `ProxyAgent`. The Connect page adds these to its examples when interception is on.

Behind an HTTPS proxy there are two certificates: the load balancer's, which clients check against their usual roots, and the one interception presents for the site, signed by the Octoprox CA. curl (`--cacert` covers the site only), undici (`requestTls` likewise) and Playwright keep them apart. Python requests does not: it checks both against the one `verify` bundle, so `verify="octoprox-ca.crt"` alone fails on the balancer's certificate. Give it a bundle with both:

```bash
cat "$(python -m certifi)" octoprox-ca.crt > octoprox-bundle.pem
```

and pass `verify="octoprox-bundle.pem"` (or set `REQUESTS_CA_BUNDLE` to it). The Connect page's Python example does this when the endpoint is HTTPS.

## Where a username goes

Below the credentials the Connect page lists the connectors a request with the username would reach right now and each one's expected share by [weight]({{ site.baseurl }}/routing-strategies#connector-weights). It uses the same eligibility as a real request, so it shows, for example, that a city is served only by a dynamic-sessions connector whose vendor can name it and by fixed exits known to be there, or that an "All countries" pool would create sessions for a new country on the first request. A connector with a [domain filter]({{ site.baseurl }}/domain-filtering) is listed as well, since the page does not know the destination. The same answer is available from the API: [`GET /api/v1/projects/{project_id}/connect`]({{ site.baseurl }}/api#connect).

## Troubleshooting

| Response | Meaning |
|----------|---------|
| `407 Proxy Authentication Required` | No credentials, or the username (without its parameters) and password match no project |
| `400 Bad Request` | A parameter cannot be read: a country that is not a two-letter code, a state or city without `-cc-`. The body says which |
| `502 Bad Gateway` | Nothing in the project serves the place asked for, or the exit failed the [preflight check]({{ site.baseurl }}/location-routing). A request is never widened to the country |
| `509 Bandwidth Limit Exceeded` | Every connector that could serve the request is over its [traffic limit]({{ site.baseurl }}/traffic-limits); the status is the connector's `limit_status`, 509 by default |
| `429 Too Many Requests` | Every proxy that could serve the request is [rate limited]({{ site.baseurl }}/rate-limiting) |
