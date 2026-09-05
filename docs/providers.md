---
layout: docs
title: Proxy Providers & the Provider SDK
nav_id: providers
---

# Proxy Providers & the Provider SDK

<p class="subtitle">Add any proxy vendor to Octoprox as a declarative descriptor — from the admin UI, a mounted YAML file, or a Python plugin.</p>

## Overview

A **provider** is anything a credential's `type` can point at. Octoprox ships four code-implemented providers (a static proxy list and the AWS, GCP and Azure cloud providers) and a set of **descriptor** providers. A descriptor is data, not code: it declares which fields a credential and a connector need, how those fields turn into proxy endpoints, and which vendor API calls discover options (zones, entry nodes, sub-users) or validate a credential.

Because most residential and datacenter vendors expose the same shape — a gateway host, a username or password with encoded targeting parameters, and a REST API for account management — one engine executes every descriptor. Adding a vendor means writing a descriptor, not a provider class.

### Shipped descriptors

| Provider | Proxy types | Vendor API used for |
|----------|-------------|---------------------|
| **Oxylabs** | residential, mobile (sessions); ISP, dedicated ISP, datacenter, dedicated datacenter (sequential gateway ports with IP discovery) | — |
| **Bright Data** | residential, mobile (global sessions); ISP, datacenter (exit IPs pinned per slot) | zone discovery with passwords and owned IPs, credential validation |
| **Decodo** *(beta)* | residential, mobile (sessions); ISP, datacenter (sticky gateway ports 10001+ with IP discovery, pay-per-GB and pay-per-IP plans) | optional API key validation |
| **Webshare** *(beta)* | proxy list (mirrors the account's list, direct or backbone) | proxy list, credential validation |
| **IPRoyal** *(beta)* | residential (sessions, parameters in the password). ISP, datacenter and mobile orders are delivered as per-order IP lists whose API shape is undocumented; import them with the static provider | entry-node discovery, optional token validation |
| **NetNut** *(beta)* | residential, static residential (ISP), mobile, datacenter (sessions; product token in the username) | — |

> **Licensing:** the engine, the SDK and the UI are Apache 2.0. The shipped descriptor files in `api/providers/builtin/` are proprietary (all rights reserved) and may only be used as part of an Octoprox installation; see the `LICENSE` file in that directory. Descriptors you author yourself are your own.

Beta descriptors follow the vendor's public documentation but have not yet been exercised against a live account. Existing Oxylabs and Bright Data credentials, connectors and proxies created before the SDK are adopted in place: the built-in descriptors keep the same `type` ids and config keys.

## Adding a provider from the UI

Admins open **Settings → Providers**. Shipped providers are read-only; any of them can be exported as YAML or duplicated as a starting point.

1. **General** — id (becomes the credential type), name, description, documentation link, logo.
2. **Credential fields** — what a user enters once per account. Mark API keys and passwords as *secret* so they are never written into proxy rows.
3. **Connector fields** — per-connector settings such as proxy counts, countries or zones. Fields can be shown conditionally (`show_when`) and selects can load their options from the vendor API.
4. **Proxy types** — one entry per product line, each with a *mode* (see below), gateway host and port, and username/password templates.
5. **Discovery** — credential validation, options sources and two-step auth flows, all expressed as HTTP calls with JMESPath extraction.
6. **Test** — run the descriptor's vendor calls with throwaway config before saving. Requests are shown with secrets redacted.
7. **YAML** — preview the normalised document, paste one to import, or export it.

Saving asks you to confirm the vendor hosts that will receive credential material. Changes take effect immediately on every instance, are versioned, and are written to an audit log with the actor and the host list.

### Security model

A descriptor tells Octoprox to send credentials to a URL, so descriptors are treated as sensitive configuration:

- Only **admins** can create, edit, import or delete descriptors. Editors use them; viewers see them.
- Vendor calls are **HTTPS only** and every hostname is resolved and checked against private, loopback, link-local and cloud-metadata ranges before connecting. The connection is pinned to the vetted address. Redirects are not followed and pagination may not leave the original host.
- Templates are plain `{placeholder}` substitution; extraction is JMESPath. Neither can reach anything outside the values Octoprox explicitly provides.
- Every egress host must be **confirmed** on save, and again whenever a new host is added. The credential form shows users where their secret is sent.
- Every mutation is **audited** (`provider_audit_log`) with the actor, action and host list.
- Python is never accepted from the UI. Providers that genuinely need code are installed by the operator (see below).

Response size and time limits are operator settings (`provider_http_max_response_bytes`, default 50 MB, `0` disables; `provider_http_timeout_seconds`, default 60).

## Operator-installed providers

Two more tiers exist for operators who deploy Octoprox:

- **Mounted YAML** — set `OCTOPROX_PROVIDERS_DIR` (or `providers.dir` in the YAML config) to a directory of descriptor files. They are loaded at startup, appear in the catalog as source `file`, and are read-only in the UI.
- **Python entry points** — a package can register providers under the `octoprox.providers` entry-point group. An entry point may resolve to a `ProviderDescriptor`, a descriptor dict, a path to a YAML file, or a class implementing the `SyncableProvider` protocol with a `descriptor` attribute (for vendors that need code, e.g. signed requests). Plugins run in-process with full trust; install only what you would install as part of the image.

```toml
[project.entry-points."octoprox.providers"]
acme = "acme_octoprox:AcmeProvider"
```

## Descriptor reference

A descriptor is a YAML (or JSON) document. Export any shipped provider for a complete example; the Bright Data descriptor exercises almost every feature.

```yaml
id: acme                      # lowercase letters, digits, - and _; becomes the credential type
name: Acme Proxies
description: Residential proxies
docs_url: https://docs.acme.example
beta: false

credential_fields:
  - key: api_key
    label: API key
    type: password
    secret: true
    required: true
  - key: username
    label: Proxy username
    required: true
  - key: password
    label: Proxy password
    type: password
    secret: true
    required: true

connector_fields:
  - key: num_proxies
    label: Number of proxies
    type: number
    required: true
    default: 1
    min: 1
    max: 1000
  - key: country_code
    label: Country
    type: select
    options_preset: countries   # built-in country list
  - key: region
    label: Region
    type: select
    options_from: regions       # dynamic, see options below
    fill: { region_password: password }   # copy option extras into other fields
    show_when: { field: credential.api_key }

proxy_types:
  - key: residential
    label: Residential
    mode: session
    host: gw.acme.example
    port: 9000
    protocol: http
    username:
      separator: "-"
      parts:
        - text: "{credential.username}"
        - text: "cc-{connector.country_code|lower}"
          when: { field: connector.country_code }
        - text: "sid-{session_id}"
    password: "{credential.password}"
    tags: [acme, residential]
    metadata:
      country_code: "{connector.country_code}"

validation:
  call:
    url: https://api.acme.example/me
    headers: { Authorization: "Bearer {credential.api_key}" }
  success: "active"
  capture: { account_id: id }
  error_message: Invalid Acme API key

options:
  regions:
    call:
      url: https://api.acme.example/regions
      headers: { Authorization: "Bearer {credential.api_key}" }
    items: regions
    value: code
    label: name
    extra: { password: pass }
```

### Fields

| Key | Description |
|-----|-------------|
| `key`, `label`, `type` | `type` is one of `text`, `password`, `number`, `select`, `boolean`, `textarea`, `url`, `country`. |
| `required`, `secret`, `readonly`, `default`, `placeholder`, `help` | Form behaviour. Secret values are stored on the credential/connector but only substituted into proxy credentials at request time. `readonly` fields are shown but not editable — use them for values derived from another field via `fill` (Bright Data's proxy type follows the zone). |
| `group` | Connector fields are grouped into tabs (`general` by default). |
| `options`, `options_preset`, `options_from` | Static options, the built-in `countries` list, or a named options source. `options_from_when` switches to the remote source only when a condition holds and falls back to the static list otherwise (Bright Data lists only the countries an ISP zone has IPs in, but the full list for residential zones). `empty_label` names the "no value" choice of an optional dynamic select. |
| `fill` | For remote selects: `{ target_field: option_extra }` copied when an option is chosen. |
| `min`, `max`, `max_from_option`, `pattern`, `transform` | Validation; `transform` is `upper`, `lower` or `strip`. `max_from_option` caps a number field with an extra carried by a sibling's selected option, first match wins (proxy count ≤ IPs in the selected country, else IPs in the zone). |
| `depends_on` | Computed when served: connector keys an options source reads, so the UI refetches options when they change. |
| `show_when` | Condition: `{ field: credential.proxy_type, in: [residential, mobile] }`, `equals`, or truthiness; `negate: true` inverts. |

### Templates

Templates appear in hosts, usernames, passwords, metadata and every HTTP call. Placeholders: `{credential.<key>}`, `{connector.<key>}`, `{session_id}`, `{index}`, `{port}`, `{discovered_ip}`, `{auth.token}`, `{item.<key>}`. Filters: `|lower`, `|upper`, `|urlencode`, `|or:fallback`. A composed template (`separator` + `parts`) drops parts whose `when` condition is false and any part that renders empty.

Secret fields render as `{key}` runtime placeholders inside stored proxies; everything else is baked in.

### Proxy type modes

| Mode | What it does | Key settings |
|------|--------------|--------------|
| `session` | One gateway; each slot gets a fresh session id and the vendor rotates the exit IP. | `host`, `port`, `username`, `password`, `session_id` (`length`, `alphabet`, `prefix`) |
| `port` | One exit IP per slot. `port_strategy: sequential` uses `port + index` (Oxylabs); `fixed` keeps one port and pins the IP with `{discovered_ip}` in the username (Bright Data). IPs come from `known_ips` (vendor API) when configured, otherwise from `discovery` (a request made *through* the proxy). | `port_strategy`, `discovery` (`url`, `ip_path`, retry limits), `known_ips` |
| `list` | The vendor API returns concrete `host:port` entries which Octoprox mirrors, including per-proxy credentials. | `list` (`call`, `items`, `host`, `port`, `username`, `password`, `protocol`, `country`, `identity`, `filter`) |

`count_field` (default `connector.num_proxies`) names the variable holding the desired slot count; in list mode it is an optional cap. `proxy_type_field` selects the proxy type from a select field when a descriptor has more than one.

### HTTP calls, auth flows, options and validation

- **Call**: `method`, `url`, `headers`, `params` (empty rendered values are dropped), `body` (JSON, strings templated), `auth` (name of an auth flow), `paginate: { next_url: <JMESPath> }`.
- **Auth flow** (`auth.<name>`): a call plus `token_path`; the token is cached for `ttl_seconds` and exposed as `{auth.token}`.
- **Options source** (`options.<name>`): a call plus `items`, `value`, `label`, `description`, `extra` (named JMESPath or value mappings), `enrich` (per-option follow-up calls with `when` and `merge`), `filter` and `cache_seconds`. `label` and `description` are evaluated over the enriched option, so they can mention values from follow-up calls. `group_by_value: true` collapses items sharing a value into one option with a `count` extra — how Bright Data turns a list of exit IPs into countries with IP counts.
- **Validation**: a call plus optional `success` predicate, `capture` (stored on the credential), `error_message` and `when`.

Value mappings turn API values into yours: `{ path: type, map: [{ starts_with: res, to: residential }], default: unknown }`.

## Configuration

| Setting | Env variable | Default |
|---------|--------------|---------|
| Descriptor directory | `OCTOPROX_PROVIDERS_DIR` | unset |
| Allow plain-http vendor APIs | `OCTOPROX_PROVIDER_EGRESS_ALLOW_HTTP` | `false` |
| Allow private/loopback vendor hosts | `OCTOPROX_PROVIDER_EGRESS_ALLOW_PRIVATE` | `false` |
| Request timeout (seconds) | `OCTOPROX_PROVIDER_HTTP_TIMEOUT_SECONDS` | `60` |
| Max response size (bytes, 0 = unlimited) | `OCTOPROX_PROVIDER_HTTP_MAX_RESPONSE_BYTES` | `52428800` |

The two egress overrides exist for development against local mocks; leave them off in production.

## API

See the [Providers section of the API reference]({{ site.baseurl }}/api#providers).
