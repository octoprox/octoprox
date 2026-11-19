---
layout: docs
title: "REST API Reference"
nav_id: api
description: "REST API reference for Octoprox: manage projects, credentials, connectors and proxies, with authentication, request IDs and curl examples for every endpoint."
---

# API Reference

<p class="subtitle">Complete REST API documentation for managing projects, credentials, connectors, and proxies.</p>

## Base URL

All API endpoints are prefixed with `/api/v1`. For example:

```
http://localhost:8000/api/v1/projects
```

Request bodies are JSON, and a request that sends one must say so with `Content-Type: application/json`; a JSON body without it is refused with `422`. File uploads (IP databases, proxy lists, backups) are `multipart/form-data`.

## Authentication

All API endpoints except `/api/v1/auth/*`, `/api/v1/edition` ([Edition](#edition)) and the ops endpoints (`/live`, `/ready`, `/health`, `/metrics`, see [Health Check](#health-check)) require a bearer token: either a session token from a login, or a personal access token. Mutation endpoints require at least Editor role; user management requires Admin role. Exit reports are the exception: any signed-in role may send them, a viewer included, so a scraper can report with the token of a viewer [service account](#users-and-service-accounts) that can change nothing else (see [Exit reputation]({{ site.baseurl }}/exit-reputation)).

The API is served by the api role (or a single instance); any api process answers any request, a write included, since every one reads the entities from Postgres on each request, and the provider catalog (`GET /api/v1/providers`, `GET /api/v1/providers/{provider_id}`) and the attribution settings (`GET /api/v1/geo/settings`) as well. Each api also keeps the custom provider descriptors, the attribution settings and the IP database list in memory for its other routes (credential checks, previews, lookups); those follow a change made through another api as soon as the change stream announces it.

### Login

```bash
POST /api/v1/auth/login
Content-Type: application/json

{
  "username": "admin",
  "password": "your-password"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 86400
}
```

### Using the Token

Include the token in the `Authorization` header:

```bash
curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..." \
  http://localhost:8000/api/v1/projects
```

### Check Auth Status

```bash
GET /api/v1/auth/status
```

### Personal Access Tokens

A token (`octo_...`) is sent exactly like a session token and acts as its user with the user's current role; revoking it, or disabling the user, takes effect on the next call. Only a hash is stored, so the secret appears in the create response and nowhere else. See [Single Sign-On and API Tokens]({{ site.baseurl }}/sso#personal-access-tokens).

| Method | Path | Description |
|---|---|---|
| GET | `/users/me/tokens` | The caller's tokens: `id`, `name`, `token_prefix`, `expires_at`, `last_used_at`, `expired` |
| POST | `/users/me/tokens` | Create one from `{name, expires_in_days}` (`expires_in_days` null for no expiry). Returns `{token, api_token}`. Refused (403) when the caller is itself a token |
| DELETE | `/users/me/tokens/{id}` | Revoke |
| GET | `/users/{user_id}/tokens` | A user's tokens (admin) |
| POST | `/users/{user_id}/tokens` | Issue a token on a user's behalf (admin); how a [service account](#users-and-service-accounts) gets one |
| DELETE | `/users/{user_id}/tokens/{id}` | Revoke (admin) |

```bash
curl -X POST -H "Authorization: Bearer <session token>" -H "Content-Type: application/json" \
  -d '{"name": "ci", "expires_in_days": 90}' http://localhost:8000/api/v1/users/me/tokens
# {"token": "octo_...", "api_token": {"id": "...", "name": "ci", "token_prefix": "octo_abcdefg", ...}}

curl -H "Authorization: Bearer octo_..." http://localhost:8000/api/v1/projects
```

`last_used_at` is set by the token's first authenticated call, even one that is then refused (a viewer's token trying a write gets 403 and is still used), and moves at most once a minute after that. A change made with a token is recorded in the [audit log](#audit-log) with the token's name and ID, as `auth_token` and `auth_token_id` in the entry's `details`.

### Users and Service Accounts

Users are managed by admins, except a user's own profile under `/users/me`. A user is a person (`kind` `person`), who signs in with a password, an invite link or a provider, or a service account (`kind` `service`), which a program uses through the API tokens an admin issues it and which never signs in. See [Service accounts]({{ site.baseurl }}/sso#service-accounts).

| Method | Path | Description |
|---|---|---|
| GET | `/users` | Every user, people and service accounts alike: `{total, users}` |
| GET | `/users/{user_id}` | One user |
| POST | `/users` | Add a person with a password: `{username, email, password, role}` |
| POST | `/users/invite` | Add a person with a set-password link instead: `{username, email, role}`. Returns `{user, invite_url}` |
| POST | `/users/service-accounts` | Add a service account: `{username, role}`, `role` `viewer` when left out. Answers `201` with the user. An admin's token may call it |
| POST | `/users/{user_id}/reinvite` | A new set-password link for a person who has no password. Needs a session |
| PATCH | `/users/{user_id}` | Change `username`, `email`, `password` (needs a session), `role` or `is_active` |
| DELETE | `/users/{user_id}` | Delete a user with their tokens and linked sign-ins; not yourself |
| GET | `/users/me` | The caller, whatever their role |
| PATCH | `/users/me` | The caller's own `email`, `password` (needs a session, and `current_password` once one is set) and `theme_preference` |

A user is `{id, username, email, role, kind, is_active, has_password, sign_in_providers, token_count, token_last_used_at, theme_preference, last_login_at, created_at, updated_at}`. `token_count` counts the user's tokens, expired ones included, and `token_last_used_at` is the latest use of any of them. A duplicate username or email is a `400`.

A service account has no email, password or invite link, and keeps none: setting a password, by an admin or with its own token, and asking for a set-password link answer `400` `A service account has no password: it uses API tokens`; setting an email answers `400` `A service account has no email: nobody signs in as it`. A password sign-in with its name answers `401` `Invalid username or password`. `kind` is set when the user is added and never changes. Its tokens are issued, listed and revoked with the admin [token endpoints](#personal-access-tokens):

```bash
curl -X POST -H "Authorization: Bearer <session token>" -H "Content-Type: application/json" \
  -d '{"username": "scraper-eu", "role": "viewer"}' http://localhost:8000/api/v1/users/service-accounts
# {"id": "6f1c...", "username": "scraper-eu", "email": "", "role": "viewer", "kind": "service", "has_password": false, "token_count": 0, ...}

curl -X POST -H "Authorization: Bearer <session token>" -H "Content-Type: application/json" \
  -d '{"name": "scraper-eu", "expires_in_days": 365}' http://localhost:8000/api/v1/users/6f1c.../tokens
# {"token": "octo_...", "api_token": {"id": "...", "name": "scraper-eu", "created_by": "admin", ...}}
```

### Sign in with a Provider

The round trip through Google, Microsoft or GitHub is driven by the browser, not by API clients; scripts use a personal access token. These endpoints are public. See [Single Sign-On and API Tokens]({{ site.baseurl }}/sso).

| Method | Path | Description |
|---|---|---|
| GET | `/auth/sso/providers` | The enabled providers, `[{kind, label}]`, for the login page |
| GET | `/auth/sso/{kind}/start` | Redirects the browser to the provider (`kind`: `google`, `microsoft`, `github`) |
| GET | `/auth/sso/{kind}/callback` | Where the provider sends the browser back. Ends at `/sso/complete?code=...` on success, or at `/?sso_error=...` with a reason |
| POST | `/auth/sso/exchange` | Trade the one-time `{code}` from the callback for `{access_token, token_type, expires_in}`. Single use, valid for a minute |

The providers themselves are set up by an admin:

| Method | Path | Description |
|---|---|---|
| GET | `/sso/providers` | Every provider with `enabled`, `client_id`, `has_client_secret`, `tenant_id`, `configured` and the `redirect_uri` to register |
| PUT | `/sso/providers/{kind}` | Save `{enabled, client_id, client_secret, tenant_id}`; a null `client_secret` keeps the stored one. Microsoft requires a single tenant ID |

Linked provider accounts are listed and removed under `/users/me/identities` and `/users/{user_id}/identities` (admin), each with `provider`, `email`, `created_at` and `last_login_at`. User responses carry `sign_in_providers`, the kinds linked to the user.

### Edition

```bash
GET /api/v1/edition
```

Public, since the login page asks before anyone signs in. Which edition the server runs, and on Octoprox Enterprise the organisation its license names while the license is active or in its grace period:

```json
{"name": "free", "organisation": null}
```

The public image always answers `free`, the Octoprox Enterprise image `enterprise`. Ask before calling the [Octoprox Enterprise endpoints](#octoprox-enterprise-endpoints): the public image has none of them, and answers a `GET` under `/api/v1` that no route serves with the web UI's page rather than a 404.

---


### Request IDs

Every response carries an `X-Request-ID` header. Send your own value in the same request header (up to 128 characters of letters, digits, `.`, `_`, `:` and `-`) to have it used and echoed back; otherwise one is generated. Quote the ID when reporting a failed call, as it appears on every server log line written while handling that request.

```bash
curl -i -H "X-Request-ID: deploy-42-step-3" http://localhost:8000/api/v1/projects
```

### When Postgres or Redis cannot be reached

A route that needs Postgres or Redis while it cannot be reached answers `503` with `Retry-After: 5` and `{"detail": "Postgres cannot be reached; try again shortly"}` (or `Redis`), not a `500`. Retry after the delay.

## Projects

Projects provide multi-tenancy support. Each project has its own credentials, connectors, and proxy pools. A route under `/projects/{project_id}` answers `404` for an id that belongs to another project, and a duplicate name or an unknown `routing_strategy` is a `400`. Changing a project's location rules (`location_contradicted`, `location_unknown`, `location_conflict_rule`, `location_city_radius_km`) queues a re-attribution of its proxies.

`exit_headers` (boolean, default `false`) tells the project's clients which exit IP served each request, in `X-Octoprox-*` headers on the `CONNECT` answer and on plain HTTP responses; see [Exit headers]({{ site.baseurl }}/connecting#exit-headers).

`observation_log` (boolean, default `true`) keeps a row in the [observation log]({{ site.baseurl }}/ip-attribution#unique-exits-per-connector) for each sighting of the project's exits; off, the sightings still count in its exit IPs and provider accuracy. `observation_retention_days` (optional, up to 3650; `0` keeps forever) is how many days the project's rows are kept; left out it follows the install's `observation_retention_days`, and on update an empty string clears it.

`exit_reputation` (`off`, `record` or `avoid`; default `off`) decides what the project's [exit reputation]({{ site.baseurl }}/exit-reputation) does with exit reports: nothing, keep them, or also route by them, keeping exits avoided for a site away from it and weighing dynamic connectors by their record there. Five numbers tune the decision: `exit_reputation_avoid_minutes` ("Avoid for", 1 to 10080, default 60) is how long a first avoid lasts, doubled for each recent one up to 7 days; `exit_reputation_half_life_hours` ("Remember for", 1 to 168, default 24) is how many hours an exit IP's reports and avoids take to count half; `exit_reputation_connector_half_life_requests` ("Connector memory", 20 to 10000, default 100) is how many more requests a dynamic connector's blocked reports and requests for a site and place take to count half, an hour at most; `exit_reputation_bad_reports` (1 to 10, default 1) is how many fresh bad reports avoid an exit for a site; and `exit_reputation_bad_share` (1 to 99, default 50) is the least percentage of the IP's recent reports for the site that must be bad. A value out of range is a `422`. See [Settings]({{ site.baseurl }}/exit-reputation#settings).

### List Projects

```bash
GET /api/v1/projects
```

### Project Load

```bash
GET /api/v1/projects/load?range=24h
```

Every project's load, oldest project first, for the project list: `requests_per_minute` (over the last five minutes, rejected requests included, the same figure as the project's Overview), then the requests, successes, failures and bytes each way over `range` (`1h`, `6h`, `24h` default, `7d` or `30d`), and `trend`, the requests per bucket of `bucket_seconds`, oldest first. The totals and the trend come from the history, so like the Overview's charts they trail live traffic by up to one flush interval. Any signed-in user can read it.

### Create Project

```bash
POST /api/v1/projects
Content-Type: application/json

{
  "name": "My Project",
  "description": "Production proxy pool",
  "username": "proxy-user",
  "password": "proxy-password",
  "routing_strategy": "round_robin"
}
```

### Get Project

```bash
GET /api/v1/projects/{project_id}
```

### Update Project

```bash
PATCH /api/v1/projects/{project_id}
Content-Type: application/json

{
  "name": "Updated Name",
  "routing_strategy": "least_used"
}
```

### Delete Project

```bash
DELETE /api/v1/projects/{project_id}
Content-Type: application/json

{"confirmation": "permanently delete"}
```

Deletes the project with its credentials, connectors and proxies. Every connector is removed as [Delete Connector](#delete-connector) removes it, so cloud instances are terminated before their rows go:

- `204`: the project held no cloud connector and is gone.
- `202`: the project stays, with `pending_deletion` set, until a worker has terminated its cloud instances and deleted its last cloud connector, then it goes too. Meanwhile its proxy clients get `407` as for a deleted project, each new connection of its tunnel devices is closed, and creating a connector in it is a `400`; its other connectors are already gone.

A cloud connector whose credential no longer works holds the deletion, with the error on the connector, until the credential is fixed. Asking again is safe: it carries on from where it stopped, and deletes the project at once when no connector is left.

### Connect

What a client sends to use the project's proxy for a place and session, and where it would go. This is what the [Connect page]({{ site.baseurl }}/connecting) shows. Every query parameter is optional:

```bash
GET /api/v1/projects/{project_id}/connect?country=us&state=ny&city=New%20York&sessid=job-1
```

The username is composed from the project's and the parameters, then read by the parser the proxy listener uses, so the normalised form and any refusal are exactly what a client would get:

```json
{
  "endpoint": {"scheme": "https", "host": "proxy.example.com", "port": null},
  "username": "myuser-cc-us-st-ny-city-new_york-sessid-job-1",
  "sessid": "job-1",
  "location": {"country": "US", "state": "NY", "city": "new_york"},
  "error": null,
  "routes": [
    {"connector_id": "...", "connector_name": "ISP static", "dynamic": false, "on_demand": false, "proxies": 2, "share": 0.75},
    {"connector_id": "...", "connector_name": "Residential", "dynamic": true, "on_demand": false, "proxies": 1, "share": 0.25}
  ],
  "countries": ["DE", "US"],
  "any_country": false,
  "places": [{"country": "US", "state": "NY", "city": "new_york", "proxies": 2}]
}
```

| Field | Meaning |
|-------|---------|
| `endpoint` | Where clients reach the proxy, from `OCTOPROX_PROXY_PUBLIC_URL`: `scheme` (`http` or `https`), `host`, and `port`, which is `null` when the URL names none (clients use the scheme's default: 443 for `https`, which URLs then leave out, and 80 for `http`, which they should name, since curl reads a bare `http://` proxy as port 1080). Without the setting: `http`, `host` `null` (the host the web UI is reached at) and `server.proxy_port` |
| `username` | The username to send, normalised; as composed when it cannot be read |
| `error` | The proxy's `400` message for the username, or `null`. A value containing a reserved delimiter (or forming one with the dash before or after it), or a session ID with a colon, is refused here too |
| `routes` | The enabled connectors a request with the username would reach now, with the routable proxies of each and the expected share of requests by weight. `dynamic` marks a dynamic-sessions gateway, `on_demand` an "All countries" pool that would create sessions for the country on the first request. Empty means the proxy would refuse the request: `502`, or `429` while the proxies that could serve it are rate limited, or the traffic-limit status (`509` by default) while their connectors are over their limit. No destination is known, so domain filters are not applied |
| `countries` | Countries the project serves: every connector's declared list and the known country of every healthy exit |
| `any_country` | A country outside `countries` is served too: a dynamic-sessions connector or residential pool lists no countries |
| `places` | States and cities healthy fixed exits are known to be in, busiest first, at most 200 |

### Countries

The project's connectors with their country settings, and where a request naming each country would go now. This is what the Countries page shows:

```bash
GET /api/v1/projects/{project_id}/countries
```

```json
{
  "connectors": [
    {"id": "...", "name": "GB residential", "credential_type": "oxylabs", "enabled": true, "weight": 1,
     "countries": ["GB"], "provisions_per_country": false, "reserved_countries": ["GB"], "excluded_countries": [], "dynamic": true},
    {"id": "...", "name": "Residential", "credential_type": "brightdata", "enabled": true, "weight": 1,
     "countries": [], "provisions_per_country": false, "reserved_countries": [], "excluded_countries": [], "dynamic": true}
  ],
  "countries": [
    {"country": "GB", "routes": [{"connector_id": "...", "connector_name": "GB residential", "dynamic": true, "on_demand": false, "proxies": 1, "share": 1.0}]}
  ],
  "other": [{"connector_id": "...", "connector_name": "Residential", "dynamic": true, "on_demand": false, "proxies": 1, "share": 1.0}],
  "untargeted": [{"connector_id": "...", "connector_name": "Residential", "dynamic": true, "on_demand": false, "proxies": 1, "share": 1.0}]
}
```

| Field | Meaning |
|-------|---------|
| `connectors` | Every connector of the project: `countries` it lists (empty: all countries it does not exclude), `provisions_per_country` whether its proxies are held per listed country (a pool or port type, so taking a country on or off its list creates or drops proxies), its reserved and excluded countries, and whether it is a dynamic-sessions connector |
| `countries` | Every country a connector lists, reserves or excludes, or has an exit in, with its `routes` as on [Connect](#connect). No routes means requests naming it fail |
| `other` | Where a request for any other country would go |
| `untargeted` | Where a request naming no country would go |

To set how connectors stand to one country, in one write:

```bash
PUT /api/v1/projects/{project_id}/countries/{country}
Content-Type: application/json

{"connectors": {"<id of the GB connector>": "reserved", "<id of another connector>": "off"}}
```

Each role is `off`, `serves` or `reserved`; connectors left out keep theirs. For a connector that lists countries, `off` takes the country off its list and `serves` or `reserved` adds it, which provisions or drops a pool's proxies there; for one listing none, `off` excludes the country. Every connector is saved, or none: a refused role answers `422`, for instance turning off the only country a connector lists, since an empty list means every country. Answers with the new overview. Editors only.

### Exit Reports

Tell Octoprox what a site did with your requests, for the project's [exit reputation]({{ site.baseurl }}/exit-reputation). Tag each request you might report with `-tag-<id>` in its proxy username (it changes nothing about the routing; one id per request, or one a session reuses; a dynamic connector's block rate counts connections, so report at most one block per connection): the gateway notes what the request went through within about 0.1 seconds, and keeps the note 1 to 2 minutes after the tag's last request, so report after the request's response. Any signed-in role may send reports, a viewer included. See [Reporting exits]({{ site.baseurl }}/exit-reputation#reporting-exits).

```bash
POST /api/v1/projects/{project_id}/exit-reports
Content-Type: application/json

{"reports": [
  {"username": "acme-cc-us-tag-job42", "site": "www.google.com", "verdict": "bad", "reason": "captcha"},
  {"username": "acme-tag-job43", "site": "https://example.com/search", "verdict": "good"},
  {"username": "acme-cc-gb-tag-job44", "site": "www.google.co.uk", "verdict": "bad",
   "connector_id": "8f14e45f-ceea-467a-9cbb-5f4d2c3a1b2e", "ip": "198.51.100.23"}
]}
```

Between 1 and 1000 reports. Each has the `username` the request used; a `site` (the host the request went to, or its URL; it counts for that exact host alone, so `google.com` and `www.google.com` are two sites); a `verdict` (`good` or `bad`); and an optional `reason` (up to 64 characters, no control characters). A report that carries `connector_id` is named by the [exit headers]({{ site.baseurl }}/connecting#exit-headers) of its response, as sent: `connector_id` (`X-Octoprox-Connector-Id`) alone for a dynamic connector, or with `ip` (`X-Octoprox-Exit-Ip`) for a connector with IPs, where it must be the exit IP one of the connector's rows was last seen exiting from (`connector_id` up to 36 characters, `ip` up to 45). The headers win, and the username's `-tag-` is the fallback: when the workers find the headers name no exit of the project's rows, the exit the tag's note names counts instead. Without `connector_id`, the `-tag-` names the exit, and a report naming its exit neither way is refused. A session id never names an exit. A report counts for what the request went through: the exit IP and the connector of a proxy row with IPs, or a dynamic connector alone, at the place the username's `-cc-`, `-st-` and `-city-` name, since its exits are per request.

**Response** (`202`), each report's result in the order sent:

```json
{
  "accepted": 2,
  "rejected": 1,
  "results": [
    {"index": 0, "status": "accepted", "ip": "203.0.113.7", "site": "www.google.com", "connector_id": "...", "place": "", "error": null},
    {"index": 1, "status": "rejected", "ip": null, "site": "example.com", "connector_id": null, "place": null, "error": "no recent request with this tag (a note is kept 1 to 2 minutes after the tag's last request)"},
    {"index": 2, "status": "accepted", "ip": "198.51.100.23", "site": "www.google.co.uk", "connector_id": "8f14e45f-ceea-467a-9cbb-5f4d2c3a1b2e", "place": "GB", "error": null}
  ]
}
```

An accepted report's `ip`, `connector_id` and `place` say what it names. For a report its tag named, they are what it counts for: `ip` null for a dynamic connector, `place` empty for a connector with IPs. For one its exit headers named, they are what was sent, the `ip` in canonical form, with the username's place, and the workers decide the rest: report 2 above counts on `198.51.100.23` at no place when its connector has IPs, and on the connector at `GB`, without the IP, when it is dynamic.

A refused report does not hold back the others, and its `error` says why. The api refuses only what it can see in the report itself. Exit headers that can name no exit (an `ip` without `connector_id`, an `ip` that is not an IP, a `connector_id` that is not printable ASCII) leave the report to its tag, and when that names none either, the `error` gives both reasons, such as `no recent request with this tag (...); exit headers: ip needs connector_id`; [Reporting exits]({{ site.baseurl }}/exit-reputation#reporting-exits) lists every message. The workers check the exit headers against the project's rows, count a report they do not fit where its tag's note says when it had one, and drop it otherwise: a connector that is gone or another project's, a connector with IPs without its `ip`, or an `ip` none of the connector's rows was last seen exiting from. They also drop a report whose project was deleted or turned `off` after the `202`. A dropped report changes no record, and is counted by reason in `octoprox_worker_exit_reports_dropped_total` ([Metrics]({{ site.baseurl }}/metrics#prometheus)).

`409` while the project's `exit_reputation` is `off`, `404` for an unknown project, `422` for a body that breaks the format, and `503` when Redis cannot be reached: send the reports again. An api reads the project from a copy it keeps current from the change stream, so just after a project is created or its `exit_reputation` changed, it can answer `404` or `409` for a few milliseconds. The workers apply a report on an exit IP within about a second; a report on a connector counts at the next minute's flush of request counts, when its factor is decided ([how fast]({{ site.baseurl }}/exit-reputation#how-fast-a-dynamic-connector-reacts)).

### Exit Reputation Records

What a project's [exit reputation]({{ site.baseurl }}/exit-reputation#views) holds, whatever the mode: one record per exit IP and site, as the Exit locations page shows them, and its connectors on its busiest sites. Any signed-in role may read them. Times are UTC, written with a `Z`; `404` for an unknown project.

| Method | Path | Description |
|---|---|---|
| GET | `/projects/{project_id}/exit-reputation` | One page of records. Filters: `ip` (a whole IP matches exactly, anything shorter as its start, such as `203.0.113.`), `site` (any part of the site, such as `google`), `status` (`avoided`, `none`, or `any`, the default). `sort`: `last_seen` (the default, newest first) or `reports` (most good and bad reports first). Paging with `limit` (1 to 1000, default 100) and `offset`. `total` is counted no further than 10,000 records past the page: `total_exact` false makes it a lower bound |
| GET | `/projects/{project_id}/exit-reputation/ips/{ip}` | One IP: how many sites it has a record for (`sites`) and how many avoid it now (`avoided_sites`); up to 100 of those records as `records`, those avoiding it now first, each the one seen last first; and the project's connectors that handed it out, from their exit IPs, as `connectors` (`connector_id`, `connector_name`, `first_seen`, `last_seen`, `sightings`). Both lists are empty for an IP the project has nothing on; `400` for text that is not an IP |
| POST | `/projects/{project_id}/exit-reputation/summary` | Per IP, its records over every site: `{"ips": [...]}`, up to 1000, answers `{"ips": [{ip, good_reports, bad_reports, sites, avoided_sites, avoided_until}]}` for the IPs with a record, in the order asked, each under the text it was asked by. A read, sent as a `POST` so a page of IPs fits |
| GET | `/projects/{project_id}/exit-reputation/connectors` | Every connector of the project on each of its busiest sites, for a request naming `place` (a place as `-cc-`, `-st-` and `-city-` name it: `US`, `US/NY`, `US/NY/new_york`, `US//austin`; left out, a request naming none). See below |

A record, read half an hour after the report that began its second avoid:

```json
{
  "ip": "203.0.113.7",
  "site": "google.com",
  "status": "avoided",
  "avoided_until": "2026-11-14T10:59:40Z",
  "good_reports": 1,
  "bad_reports": 3,
  "weighted_good": 0.49,
  "weighted_bad": 1.94,
  "weighted_avoids": 1.9,
  "avoid_until": "2026-11-14T10:59:40Z",
  "first_seen_at": "2026-11-02T14:03:11Z",
  "last_seen_at": "2026-11-14T08:59:40Z",
  "last_report_at": "2026-11-14T08:59:40Z",
  "last_reason": "captcha"
}
```

`status` is `avoided` while the avoid is ahead, with `avoided_until` its end, else `none`. The counts are for the record's life; the `weighted_` ones are the recent counts that decide, faded by the project's "Remember for" to the time of the answer, `weighted_avoids` the recent avoids the next one doubles by. `avoid_until` is the last avoid's end, so it may be past.

**Connectors by site.** `GET /projects/{project_id}/exit-reputation/connectors?place=US/NY` answers, for the up to 8 sites with the most reports:

```json
{
  "place": "US/NY",
  "places": [{"place": "US", "label": "United States", "reports": 41}, {"place": "US/NY", "label": "US, NY", "reports": 17}],
  "sites": ["www.google.com", "www.bing.com"],
  "reputation_applied": true,
  "connectors": [
    {
      "connector_id": "…", "name": "Oxylabs residential", "credential_type": "oxylabs", "dynamic": true, "weight": 3,
      "cells": {
        "www.google.com": {"reputation": 0.65, "reputation_kind": "block_rate", "reputation_place": "US/NY", "blocked": 70,
                           "requests": 200, "usable_ips": null, "eligible_ips": null, "effective_share": 79.59},
        "www.bing.com": {"reputation": 1.0, "reputation_kind": "block_rate", "reputation_place": null, "blocked": null,
                         "requests": null, "usable_ips": null, "eligible_ips": null, "effective_share": 75.0}
      }
    },
    {
      "connector_id": "…", "name": "DC pool", "credential_type": "static_proxy_provider", "dynamic": false, "weight": 1,
      "cells": {
        "www.google.com": {"reputation": 0.5, "reputation_kind": "usable_ips", "reputation_place": null, "blocked": 10,
                           "requests": 380, "usable_ips": 10, "eligible_ips": 20, "effective_share": 20.41},
        "www.bing.com": {"reputation": 1.0, "reputation_kind": "usable_ips", "reputation_place": null, "blocked": 0,
                         "requests": 95, "usable_ips": 20, "eligible_ips": 20, "effective_share": 25.0}
      }
    }
  ]
}
```

Each cell is the connector on that site as the [traffic split](#traffic-split) shows one site: its `reputation` and what measures it, the counts behind it, and its `effective_share` of the site's requests. `places` lists the requested places with connector records on any site (the most reported first, at most 20; requests naming no place are the `place` left out), and `reputation_applied` says whether routing weighs the connectors by it: the project's `exit_reputation` is `avoid`. `400` for a `place` that names none.

---

## Credentials

Credentials store cloud provider authentication details.

### List Credentials

```bash
GET /api/v1/projects/{project_id}/credentials
```

### Create Credential

`type` is any provider id from the catalog. The config is validated against the provider's field schema; descriptor providers with a validation call also verify the credential with the vendor and may store captured values (for example Bright Data's customer id). Names are unique per project, ignoring case: a duplicate returns `400`.

```bash
POST /api/v1/projects/{project_id}/credentials
Content-Type: application/json

{
  "name": "AWS Production",
  "type": "aws",
  "config": {
    "auth_mode": "access_keys",
    "access_key": "AKIAIOSFODNN7EXAMPLE",
    "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
  }
}
```

An AWS credential can also carry no secrets at all. With `"auth_mode": "host_role"` Octoprox uses the identity of the host it runs on (EC2 instance profile, ECS task role, EKS pod identity or `AWS_*` environment variables). Either mode may add `role_arn` and an optional `external_id` to assume a role with `sts:AssumeRole` before touching EC2. See the [AWS setup guide](aws-setup) for the IAM side.

```json
{
  "name": "AWS Customer Account",
  "type": "aws",
  "config": {
    "auth_mode": "host_role",
    "role_arn": "arn:aws:iam::123456789012:role/octoprox-proxies",
    "external_id": "acme-7f3b9c"
  }
}
```

GCP and Azure credentials have the same shape. `"auth_mode": "host_identity"` uses the service account, managed identity or workload identity of the host and stores nothing; the defaults `service_account_key` and `client_secret` keep the stored-secret behaviour. GCP may add `impersonate_service_account` in either mode; Azure host mode may add `target_tenant_id` and `target_client_id` for an app registration federated to the host identity. See the [GCP](gcp-setup) and [Azure](azure-setup) setup guides.

**Credential Types:** `aws`, `gcp`, `azure`, `static`

### Get Credential

```bash
GET /api/v1/projects/{project_id}/credentials/{credential_id}
```

### Update Credential

```bash
PATCH /api/v1/projects/{project_id}/credentials/{credential_id}
```

### Delete Credential

```bash
DELETE /api/v1/projects/{project_id}/credentials/{credential_id}
```

---

## Connectors

Connectors define how proxies are provisioned (cloud instances or static).

### List Connectors

```bash
GET /api/v1/projects/{project_id}/connectors
```

### Create Connector

Connector names are unique per project, ignoring case: a duplicate returns `400`.

```bash
POST /api/v1/projects/{project_id}/connectors
Content-Type: application/json

{
  "name": "AWS US-East Proxies",
  "credential_id": "credential-uuid",
  "config": {
    "instance_name": "octoprox-proxy",
    "region": "us-east-1",
    "instance_type": "t3.micro",
    "security_group": "sg-0123456789abcdef0",
    "min_proxies": 1,
    "max_proxies": 10
  },
  "routing_config": {
    "domain_whitelist": ["example.com", "api.example.com"]
  },
  "rate_limit_config": {
    "max_requests": 100,
    "window_seconds": 60,
    "quarantine_seconds_min": 120,
    "quarantine_seconds_max": 300
  }
}
```

#### Routing weight (routing_config.weight)

`routing_config.weight` is the connector's relative share of the project's traffic against the other connectors that can serve a request: an integer from 1 to 100, default 1. Weights 1 and 3 split requests 25/75. The share does not depend on how many proxies the connector holds. The default is not stored, so a connector at weight 1 shows no `weight` key. See [Connector weights]({{ site.baseurl }}/routing-strategies#connector-weights).

```json
{
  "routing_config": {
    "weight": 3,
    "domain_blacklist": ["blocked.com"]
  }
}
```

#### Domain Filtering (routing_config)

Connectors support optional domain-based filtering to control which target domains their proxies serve. The `routing_config` field accepts:

- **`domain_whitelist`** - Only route requests for these domains through this connector's proxies.
- **`domain_blacklist`** - Route all requests *except* these domains through this connector's proxies.

Whitelist and blacklist are mutually exclusive - you can set one or the other, but not both.

Domain matching is hierarchical: entering `bing.com` matches `bing.com` and all subdomains (`www.bing.com`, `images.bing.com`, etc.).

**Examples:**

Whitelist - only allow specific domains:
```json
{
  "routing_config": {
    "domain_whitelist": ["example.com", "api.example.com"]
  }
}
```

Blacklist - block specific domains:
```json
{
  "routing_config": {
    "domain_blacklist": ["blocked.com", "ads.tracker.net"]
  }
}
```

No restrictions (default):
```json
{
  "routing_config": {}
}
```

#### Reserved and excluded countries (routing_config)

`routing_config.reserved_countries` holds countries for the connector: requests naming one go only to the project's connectors reserving it (for a host their domain filters allow), split by weight, and fail when none of them can serve it. `routing_config.excluded_countries` lists the countries a connector listing none never serves. Codes are normalised like the country list; a country cannot be both, a connector listing countries reserves only listed ones and excludes none, and other values are answered with `422`. Empty lists are not stored. See [Reserved and excluded countries]({{ site.baseurl }}/routing-strategies#reserved-and-excluded-countries).

```json
{
  "config": {"countries": ["GB"]},
  "routing_config": {"reserved_countries": ["GB"]}
}
```

#### Countries (config)

Connectors declare the countries their proxies exit from inside `config`, so clients can pick them with a `-cc-<code>` suffix on the proxy username (see [Country Routing]({{ site.baseurl }}/routing-strategies#country-routing)). Codes are ISO 3166-1 alpha-2 and are normalised to upper case, with `UK` stored as `GB`; an empty list is dropped.

- Static and cloud connectors: `config.countries`, a list of codes.
- Provider connectors: the provider's country field (`config.country_code` for the built-in descriptors). It accepts one code, a list, or a comma-separated string and is always returned as a list. Listing several countries provisions `num_proxies` slots per country.

```json
{
  "name": "EU residential",
  "credential_id": "…",
  "config": { "num_proxies": 5, "country_code": ["DE", "FR", "NL"] }
}
```

Proxy responses include `country`, the exit country when the vendor reported it or the slot was provisioned for one, and `state_code` and `city`, each with a `state_source` or `city_source` (`manual` when pinned by hand, else the attribution source that resolved it), the place `-st-` and `-city-` requests match the proxy on (see [State and City Routing]({{ site.baseurl }}/routing-strategies#state-and-city-routing)). Connector responses include `target`: the intended pool size (`total`, or null when the connector has none) and how it is derived, `per_country` times the `countries` that have a slot group, with the ones created on demand listed in `on_demand`.

Manually added proxies (static connectors) get their exit location looked up by a worker within seconds of being created: one request goes through the proxy to the `proxy.geo_lookup` endpoint and the discovered IP becomes `display_host` while the country lands in `country`. Pass `country` on `POST .../proxies` to set it yourself and skip the lookup, set `country` to `""` on `PATCH .../proxies/{id}` to clear it, or call `POST .../proxies/{id}/locate` to re-run the lookup and get the updated proxy back (the api makes the request itself, so it needs network access to the proxy; 502 when the request through the proxy fails). `state` (an ISO 3166-2 subdivision part such as `NY`, `ON` or `ENG`, or the full `US-NY`) and `city` (any spelling; stored as a slug) pin the state and city by hand on create and update; an empty string clears each. Pinned values win over the databases for routing and are what attribution verifies the exit against.

#### Rate Limiting (rate_limit_config)

Connectors support optional per-proxy rate limiting. When a proxy exceeds `max_requests` within `window_seconds`, it is quarantined (excluded from selection) for a random duration between `quarantine_seconds_min` and `quarantine_seconds_max`. See the [Rate Limiting]({{ site.baseurl }}/rate-limiting) guide for details.

| Field | Type | Description |
|-------|------|-------------|
| `max_requests` | integer | Max requests per proxy in the window |
| `window_seconds` | integer | Sliding window duration (1-86400) |
| `quarantine_seconds_min` | integer | Min quarantine duration (1-86400) |
| `quarantine_seconds_max` | integer | Max quarantine duration (1-86400) |
| `sticky_quarantine` | boolean | Block sticky session fallback (default: `false`) |

**Example:**
```json
{
  "rate_limit_config": {
    "max_requests": 100,
    "window_seconds": 60,
    "quarantine_seconds_min": 120,
    "quarantine_seconds_max": 300,
    "sticky_quarantine": false
  }
}
```

Disabled (default):
```json
{
  "rate_limit_config": {}
}
```

#### Health Checks (health_check_config)

How often the connector's proxies are health-checked. Empty, they are checked every `OCTOPROX_HEALTH_CHECK_INTERVAL` of the install (60 seconds by default). Each check is a request through the proxy, so a large list of fixed exits can be checked less often than a residential pool. An update replaces the whole object; `{}` goes back to the install's interval.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `interval_seconds` | integer | the install's | Seconds between two checks of each proxy, 10 to 86400 |

```json
{
  "health_check_config": { "interval_seconds": 600 }
}
```

Every connector response also carries `health_check_interval`, the seconds its proxies are checked at now (its own interval or the install's), and `default_health_check_interval`, the install's. A proxy still initializing (a new cloud instance) is checked at the install's interval until a check finds it healthy, whatever its connector sets.

#### Traffic Limits (traffic_config)

Connectors meter the bytes through their proxies per billing period and can cap them. `traffic_config` holds the period, the limit, what happens at the limit and the price per GB; see the [Traffic Limits & Billing]({{ site.baseurl }}/traffic-limits) guide. Only explicit choices are stored, a key at its default is dropped.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `limit_bytes` | integer | none | Bytes per period before `action` applies; omit to meter only |
| `period` | `day`, `week`, `month` | `month` | How often usage starts over (UTC) |
| `reset_day` | integer | `1` | Day of month (1-28) for `month`, weekday (1 = Monday) for `week` |
| `action` | `alert`, `block`, `interrupt` | `alert` | Flag only, stop new requests, or also cut running transfers |
| `warn_percent` | integer | `80` | Share of the limit at which the connector is flagged |
| `limit_status` | `509` or `429` | `509` | Status when every eligible connector is over its limit |
| `price_per_gb` | number | none | Price per decimal GB (1 GB = 1,000,000,000 bytes) |
| `currency` | string | `USD` | ISO 4217 code |

**Example:**
```json
{
  "traffic_config": {
    "limit_bytes": 500000000000,
    "period": "month",
    "reset_day": 15,
    "action": "block",
    "price_per_gb": 8
  }
}
```

Every connector response carries `traffic_usage`, the usage in the current period: the connector's flushed history since the period started plus what the gateways counted since the last flush, judged with the same rules the traffic job blocks connectors with:

```json
{
  "traffic_usage": {
    "period": "month",
    "period_start": "2026-09-15T00:00:00",
    "period_end": "2026-10-15T00:00:00",
    "bytes_sent": 12000000000,
    "bytes_received": 230000000000,
    "total_bytes": 242000000000,
    "limit_bytes": 500000000000,
    "percent": 48.4,
    "action": "block",
    "status": "ok",
    "blocked": false,
    "pending": false,
    "price_per_gb": 8.0,
    "currency": "USD",
    "cost": 1936.0,
    "reset_at": null
  }
}
```

`status` is `ok`, `warning` (past `warn_percent`) or `exceeded`, judged with the rule the traffic job decides by. `blocked` is the block the traffic job stored, the one the gateways enforce: true while the connector takes no new requests. After a usage reset or a raised limit it stays true until a worker decides again, normally within a second and at most a minute later, and it reads `false` while Redis cannot be read. `pending` is true from a usage reset, or an edit of the limit, the action, the period or the reset day, until a worker stores the decision it asked for; it too reads `false` while Redis cannot be read. `percent` and `cost` are `null` without a limit or a price. `reset_at` is set when usage was reset by hand inside the period; `traffic_reset_at` on the connector holds the same timestamp.

Clients whose request finds every eligible connector over its limit get the configured status (509 by default) with a `Proxy-Status: octoprox; error=bandwidth_limit_exceeded` header.

#### Reset Traffic Usage

Start the connector's usage over from now (editor role). History is kept; the count against the limit restarts, and a traffic decision is asked for, which lifts a block the limit raised:

```bash
POST /api/v1/projects/{project_id}/connectors/{connector_id}/traffic/reset
```

Returns the updated connector. Its usage counts from now, while `traffic_usage.blocked` still shows the stored block, with `pending` true, until a worker has decided (normally within a second; the gateways keep the block until then).

#### Connector Metrics History

The connector's own metrics history, which survives its proxies being rotated or re-synced:

```bash
GET /api/v1/projects/{project_id}/connectors/{connector_id}/metrics/history?range=24h
```

Same `range` values and response shape as [project metrics history](#metrics-history).

#### Unquarantine Proxy

Forcefully remove a proxy from quarantine:

```bash
POST /api/v1/projects/{project_id}/proxies/{proxy_id}/unquarantine
```

Returns `200` with `{"status": "ok", "proxy_id": "..."}` on success, `400` if the proxy is not quarantined. Every gateway routes to the proxy again within moments, through the routing feed.

### Get Connector Options

Get available regions, instance types, the country list and other static options:

```bash
GET /api/v1/connector-options
```

Provider-specific dynamic options (zones, entry nodes, sub-users) are served by `POST /api/v1/providers/{provider_id}/options/{option_name}`, which asks the vendor's API through the provider's descriptor (see [Providers & SDK]({{ site.baseurl }}/providers)).

### Get Connector

```bash
GET /api/v1/projects/{project_id}/connectors/{connector_id}
```

### Update Connector

```bash
PATCH /api/v1/projects/{project_id}/connectors/{connector_id}
Content-Type: application/json

{
  "name": "Updated Name",
  "routing_config": {
    "domain_whitelist": ["new-domain.com"]
  }
}
```

Returns the updated connector. A connector being deleted takes no edit (`400`). After the edit is saved, it asks for the work that follows, for a worker to run within seconds:

- a reconcile (`sync`) for a connector of a syncing provider, and a scaling check (`scale`) for a cloud connector;
- a traffic decision when the edit changes the limit, the action, the period or the reset day of `traffic_config`; until it is stored, the answer shows the stored block with `traffic_usage.pending` true. An edit of the price, the currency, `warn_percent` or `limit_status` applies at once and asks for nothing;
- a change of `rate_limit_config` lifts the quarantines of the connector's proxies, tripped under limits that no longer apply.

### Delete Connector

```bash
DELETE /api/v1/projects/{project_id}/connectors/{connector_id}
```

A cloud connector is not deleted at once: it is disabled, its proxies are marked terminating, and a worker terminates their instances and then deletes the connector. Creating or editing a connector of a syncing provider queues a sync, and a cloud connector a scaling check, for a worker to run within seconds.

---

## Proxies

Proxies are the actual proxy servers managed by Octoprox.

### List Proxies

```bash
GET /api/v1/projects/{project_id}/proxies
```

### Create Proxy (Static)

```bash
POST /api/v1/projects/{project_id}/proxies
Content-Type: application/json

{
  "connector_id": "connector-uuid",
  "host": "192.168.1.100",
  "port": 3128,
  "protocol": "http",
  "username": "proxy-user",
  "password": "proxy-pass"
}
```

**Protocols:** `http`, `https`, `socks4`, `socks5`

### Upload Proxies (Bulk)

Upload multiple proxies from a CSV file:

```bash
POST /api/v1/projects/{project_id}/proxies/upload
Content-Type: multipart/form-data

file: proxies.csv
connector_id: connector-uuid
```

Lines that cannot be read are reported and skipped; the rest are added in one transaction, so they are all added or, when the write fails, none is.

CSV format (one proxy per line):
```
http://192.168.1.1:8080
socks5://user:pass@10.0.0.1:1080
```

### Get Proxy

```bash
GET /api/v1/projects/{project_id}/proxies/{proxy_id}
```

### Delete Proxy

```bash
DELETE /api/v1/projects/{project_id}/proxies/{proxy_id}
```

A cloud proxy is marked terminating and a worker terminates its instance before the row goes; a slot of a provider connector is deleted and a sync is queued, which provisions a replacement when the connector wants one.

### Proxy location fields

Every proxy response carries the outcome of [IP attribution]({{ site.baseurl }}/ip-attribution): `country` (what routing uses), `country_source` (`database`, `vendor`, `endpoint` or `manual`), `vendor_country` (what the vendor claimed), `location_conflict` (the claim is contradicted), `location_verdict` (`confirmed`, `contradicted` or `open`; null when nothing was claimed or the exit was not judged yet), `location` (region, `state_code`, city, coordinates, ASN and anonymity flags from the databases, when any covers the IP), `state_code`, `state_source`, `city` and `city_source` (the state and city routing matches on: pinned by hand, or resolved from the databases, then the echo endpoint, then the vendor's word, like the country), and `state_verdict` and `city_verdict` (how a claimed state or city fared, with the same values as `location_verdict`). Setting `country` by hand pins it as `manual`: attribution then verifies the exit against it instead of overwriting it.

`exit_ip` is the IP the proxy's [exit reputation]({{ site.baseurl }}/exit-reputation#views) is kept under: its last seen exit, as one canonical text (an IPv4-mapped IPv6 address as the IPv4). It is null on a dynamic gateway, whose every session exits somewhere new, and until the exit is first seen.

---

## Metrics

### Get Project Metrics

```bash
GET /api/v1/projects/{project_id}/metrics
```

**Response:**
```json
{
  "pool": {
    "total_proxies": 10,
    "healthy_proxies": 8,
    "unhealthy_proxies": 2,
    "quarantined_proxies": 1,
    "draining_proxies": 0,
    "terminating_proxies": 0,
    "total_requests": 15420,
    "total_successes": 15112,
    "total_failures": 308,
    "overall_success_rate": 98.0,
    "avg_latency_ms": 145.2,
    "total_bytes_sent": 91000000,
    "total_bytes_received": 4830000000
  },
  "strategy": {
    "current_strategy": "round_robin",
    "available_strategies": ["round_robin", "least_used", "random", "sticky", "health_based"]
  }
}
```

The pool is the proxies of the project's enabled connectors that no traffic limit holds back; `healthy_proxies` are healthy and not quarantined, and `unhealthy_proxies` is everything else that is not draining or terminating, quarantined proxies included. The counters are the project's all-time totals plus what the gateways counted since the last metrics flush, so they are within seconds of live traffic and read the same on every api process. `overall_success_rate` is a percentage, and `avg_latency_ms` is averaged over the successful requests. The strategy is the project's `routing_strategy`; change it with [Update Project](#update-project).

### Metrics History

The project's counters over time, as the workers' metrics flushes wrote them:

```bash
GET /api/v1/projects/{project_id}/metrics/history?range=24h
```

`range` is one of `1h`, `6h`, `24h` (default), `7d`, `30d`. Up to a day the points are the one-minute rows; longer ranges are summed into hourly (`7d`) or 6-hourly (`30d`) buckets.

**Response:**
```json
{
  "snapshots": [
    {
      "timestamp": "2026-10-28T11:42:00",
      "request_count": 1200,
      "success_count": 1130,
      "failure_count": 70,
      "avg_latency_ms": 1412.6,
      "step_latency_ms": { "route": 0.4, "preflight": 612.0, "connect": 38.1, "handshake": 750.3, "response": 11.8 },
      "bytes_sent": 2400000,
      "bytes_received": 96000000
    }
  ]
}
```

Oldest first. `avg_latency_ms` is averaged over the point's successful requests, `null` when none succeeded. `step_latency_ms` splits it into the [steps]({{ site.baseurl }}/metrics#latency-where-the-time-goes) the requests went through, each averaged over the same requests (a step a request skipped counts as 0), so they add up to `avg_latency_ms`; it is `null` when none succeeded and for history recorded before steps were kept.

### Traffic Split

How the project's traffic divides between its connectors under the current [weights]({{ site.baseurl }}/routing-strategies#connector-weights), expected and observed, and under [exit reputation]({{ site.baseurl }}/exit-reputation#routing) for one site.

```bash
GET /api/v1/projects/{project_id}/metrics/traffic-split?range=1h
GET /api/v1/projects/{project_id}/metrics/traffic-split?range=1h&site=www.google.com&place=US/NY
```

`range` is one of `1h` (default), `6h`, `24h`, `7d`, `30d` and bounds the observed counts. `site` (a host, or a URL whose host is taken) asks for each connector's reputation there and the [effective share](#effective-shares) it makes; `place`, with a site, is the place requests to it name, as `-cc-`, `-st-` and `-city-` name it (`US`, `US/NY`, `US/NY/new_york`, `US//austin`; left out, a request naming none). `400` for a site or place that names none.

**Response:**
```json
{
  "strategy": "round_robin",
  "range": "1h",
  "total_weight": 4,
  "observed_requests": 1200,
  "connectors": [
    {
      "connector_id": "…",
      "name": "Oxylabs residential",
      "credential_type": "oxylabs",
      "enabled": true,
      "weight": 3,
      "dynamic": true,
      "total_proxies": 1,
      "eligible_proxies": 1,
      "expected_share": 75.0,
      "excluded_reason": null,
      "observed_requests": 880,
      "observed_share": 73.33,
      "observed_bytes": 18400000000,
      "cost": 147.2,
      "currency": "USD"
    },
    {
      "connector_id": "…",
      "name": "DC pool",
      "credential_type": "static_proxy_provider",
      "enabled": true,
      "weight": 1,
      "dynamic": false,
      "total_proxies": 20,
      "eligible_proxies": 18,
      "expected_share": 25.0,
      "excluded_reason": null,
      "observed_requests": 320,
      "observed_share": 26.67,
      "observed_bytes": 6100000000,
      "cost": null,
      "currency": null
    }
  ],
  "site": null,
  "place": null,
  "reputation_applied": false,
  "reputation_sites": [{"site": "www.google.com", "reports": 58}, {"site": "www.bing.com", "reports": 12}],
  "reputation_places": []
}
```

`expected_share` (0-100) is what an untargeted request sees right now: every enabled connector with at least one eligible proxy takes `weight / total_weight`. A connector taking nothing has `excluded_reason` set to `disabled`, `traffic_limit` (blocked by its [traffic limit]({{ site.baseurl }}/traffic-limits)), `reserved_only` (it serves only requests naming a country it [reserves]({{ site.baseurl }}/routing-strategies#reserved-and-excluded-countries), so no untargeted ones) or `no_eligible_proxies`. `observed_share` is the connector's part of the requests in the window, `null` when there were none; `observed_bytes` is its traffic in the window and `cost` what that traffic costs at the connector's price per GB (`null` when unpriced). The observed figures come from the connector's own metrics history, so they survive proxy rotation. The response also carries `observed_bytes` for the whole project.

#### Effective shares

`reputation_sites` lists the sites with exit reputation records for the project, the most reported first, at most 20, each report counted once; with a `site`, `reputation_places` lists the places requests to it named in reports through a connector, the same way (`place`, a `label` such as `United States`, `US, NY` or `US, NY, new_york`, and `reports`; requests naming no place are the `place` left out). With a `site`, the response names the `site` and `place` it was weighed for, and every connector gains what [exit reputation]({{ site.baseurl }}/exit-reputation#routing) says of it there, read from the project's records:

```json
{
  "name": "Oxylabs residential",
  "weight": 3,
  "expected_share": 75.0,
  "reputation": 0.65,
  "reputation_kind": "block_rate",
  "reputation_place": "US",
  "blocked": 70,
  "requests": 200,
  "usable_ips": null,
  "eligible_ips": null,
  "effective_weight": 1.95,
  "effective_share": 79.59
}
```

- `reputation` is what the connector's weight is multiplied by for the site, 1 when nothing says otherwise. A dynamic connector's (`reputation_kind` `block_rate`) is one less its recent blocked reports per recent request there, at least 0.1, in steps of 0.05, taken from the finest level of the place with 20 recent requests or more (`reputation_place`: the city, else its state, else its country; `""` for requests naming no place, whose figure is its own); `blocked` and `requests` are the recent counts behind it, faded by the project's "Connector memory" (those of the finest level with a record while no level has enough requests, `null` with none). The factor is decided once a minute and moves only once the block rate has moved a full step from it, so it can sit less than a step from what `blocked` and `requests` give. A connector with IPs (`usable_ips`) is weighed by the share of its rows eligible for requests to the site from the place (domain filters, reservations and the place applied as routing applies them) whose exit IP is not avoided for the site: `usable_ips` of `eligible_ips`, 0 when none is usable; its `blocked` and `requests` are its record for the site, for reading only.
- `effective_weight` is the weight times the reputation of a connector serving requests to the site from the place (0 for one that serves none of them), and `effective_share` the connector's part of those requests by these weights. `reputation_applied` is true when they move routing: a site was given and the project's `exit_reputation` is `avoid`. Otherwise `effective_weight` is the weight of a connector serving the site from the place, 0 for one that does not, and `effective_share` its share by weight among them, and the reputation is shown for what it would do. `excluded_reason` stays the reason for requests naming no site and no place, so a connector reserving the place's country can have one and still take every request. Without a site, `reputation` is 1, the other reputation fields `null`, and the effective figures the configured ones.

### Hosts

Where the project's traffic went in a window, by destination host and by the connector that carried it. The numbers come from the flushed host history (see [Metrics]({{ site.baseurl }}/metrics#hosts-where-the-traffic-goes)), so they lag live traffic by up to one flush interval.

```bash
GET /api/v1/projects/{project_id}/metrics/hosts?range=24h&connector_id=…&search=example&limit=100
```

`range` is one of `1h`, `6h`, `24h` (default), `7d`, `30d`. `connector_id` keeps only the traffic that connector carried, `search` only hosts containing the text (case-insensitive), and `limit` (1-500, default 100) caps how many hosts come back, busiest first; `totals.host_count` says how many matched in all.

**Response:**
```json
{
  "range": "24h",
  "since": "2026-10-09T10:00:00",
  "overflow_host": "(other)",
  "enabled": true,
  "limit": 100,
  "totals": {
    "host_count": 42,
    "request_count": 18200,
    "success_count": 17900,
    "failure_count": 300,
    "avg_latency_ms": 212.4,
    "bytes_sent": 91000000,
    "bytes_received": 4830000000
  },
  "connectors": [
    { "connector_id": "…", "name": "Oxylabs residential", "credential_type": "oxylabs", "enabled": true }
  ],
  "hosts": [
    {
      "host": "www.example.com",
      "request_count": 9100,
      "success_count": 9050,
      "failure_count": 50,
      "avg_latency_ms": 180.2,
      "bytes_sent": 40000000,
      "bytes_received": 2900000000,
      "request_share": 50.0,
      "bytes_share": 59.73,
      "connectors": [
        {
          "connector_id": "…", "name": "Oxylabs residential", "credential_type": "oxylabs",
          "request_count": 9100, "success_count": 9050, "failure_count": 50,
          "avg_latency_ms": 180.2, "bytes_sent": 40000000, "bytes_received": 2900000000
        }
      ]
    }
  ]
}
```

`request_share` and `bytes_share` (0-100) are the host's part of the window's requests and of its bytes both ways. A host named `overflow_host` is where requests to hosts beyond a configured ceiling were folded (see `metrics.hosts.max_hosts` in [Configuration]({{ site.baseurl }}/configuration#host-metrics); it never appears with the default of no ceiling); `enabled` is `false` when per-host counting is switched off, in which case nothing new is recorded. A connector entry whose `name` is `null` was deleted after its rows were written; its rows cascade with it on the next flush.

The busiest hosts over time, for a chart:

```bash
GET /api/v1/projects/{project_id}/metrics/hosts/history?range=24h&top=8&connector_id=…
GET /api/v1/projects/{project_id}/metrics/hosts/history?range=24h&hosts=www.example.com,api.example.com
```

The `top` hosts by requests (1-12, default 8), or the `hosts` named (comma-separated, up to 12), get a series each; every other host is folded into one series under `overflow_host`. Buckets are sized to the range: 1 minute for `1h`, 5 minutes for `6h`, 15 minutes for `24h`, 1 hour for `7d`, 6 hours for `30d`. Every series carries every bucket of the window, with zeros where the host was quiet, so they stack.

**Response:**
```json
{
  "range": "24h",
  "since": "2026-10-09T10:00:00",
  "bucket_seconds": 900,
  "overflow_host": "(other)",
  "series": [
    {
      "host": "www.example.com",
      "points": [
        { "timestamp": "2026-10-09T10:00:00", "request_count": 120, "success_count": 119, "failure_count": 1,
          "avg_latency_ms": 171.0, "bytes_sent": 500000, "bytes_received": 38000000 }
      ]
    },
    { "host": "(other)", "points": [ { "timestamp": "2026-10-09T10:00:00", "request_count": 31, "…": "…" } ] }
  ]
}
```

### Failure log

Why the project's requests failed or were refused (see [Metrics]({{ site.baseurl }}/metrics#failures-why-requests-failed)). The summary counts every failure from the metrics history; the examples, kept per kind, list each distinct failure once and are updated within about 5 seconds of it happening.

#### Summary

```bash
GET /api/v1/projects/{project_id}/failures/summary?range=24h
```

`range` is one of `1h`, `6h`, `24h` (default), `7d`, `30d`. The numbers come from the flushed history, so they trail live traffic by up to one flush interval.

**Response:**
```json
{
  "range": "24h",
  "since": "2026-10-14T11:00:00",
  "bucket_seconds": 900,
  "total": 3750,
  "kinds": [
    { "kind": "upstream_rejected", "count": 3097,
      "upstream_statuses": [ { "status": 407, "count": 2055 }, { "status": 502, "count": 917 } ] },
    { "kind": "upstream_timeout", "count": 463, "upstream_statuses": [] },
    { "kind": "no_proxy_available", "count": 190, "upstream_statuses": [] }
  ],
  "connectors": [
    { "connector_id": "a2b9…", "name": "Bright Data residential", "count": 3560 },
    { "connector_id": null, "name": null, "count": 190 }
  ],
  "series": [
    { "kind": "upstream_rejected", "points": [ { "timestamp": "2026-10-14T11:00:00", "count": 12 } ] }
  ],
  "enabled": true
}
```

`kinds` and `connectors` are ordered most first; a connector of `null` stands for requests refused before a proxy was chosen. `series` has one entry per kind in the same order, each with every bucket of the window (zeros where it was quiet), so they stack.

#### Examples

```bash
GET /api/v1/projects/{project_id}/failures?kind=upstream_rejected&count=200&cursor=…
```

The examples seen last first. `kind` keeps one kind (leave it out for all of them), `count` is 1-1000 (default 200), and `next_cursor` is set when older examples remain; pass it as `cursor` for the next page.

**Response:**
```json
{
  "records": [
    {
      "id": "upstream_rejected:3f9a0c1b2d4e5f60",
      "kind": "upstream_rejected",
      "status": 502,
      "message": "407 Proxy Authentication Required",
      "error_type": "TunnelRefusedError",
      "method": "CONNECT",
      "target": "www.example.com:443",
      "session_id": "order-1",
      "location": "US/NY",
      "peer_id": null,
      "peer_name": null,
      "proxy_id": "6f1c…",
      "connector_id": "a2b9…",
      "connector_name": "Bright Data residential",
      "proxy": "brd.superproxy.io:33335",
      "upstream_status": 407,
      "upstream_message": "x-brd-error: Auth failed",
      "latency_ms": 212.4,
      "step": "handshake",
      "steps": { "route": 0.3, "connect": 41.2, "handshake": 170.9 },
      "count": 37,
      "first_at": "2026-10-15T10:59:58.120000",
      "last_at": "2026-10-15T11:00:02.870000"
    }
  ],
  "next_cursor": null,
  "total": 1,
  "newest": 1760526004211,
  "enabled": true,
  "max_entries": 1000,
  "retention_hours": 168
}
```

`kind` is one of `upstream_timeout`, `upstream_unreachable`, `upstream_rejected`, `upstream_error`, `client_closed`, `interrupted`, `preflight_mismatch` (the failures the request metrics count), `no_proxy_available`, `all_proxies_quarantined`, `exits_avoided` (every exit that could serve the request is avoided for its site by [exit reputation]({{ site.baseurl }}/exit-reputation#routing), or a sticky session's exit is), `traffic_limit_exceeded`, `invalid_location` (refused before a proxy was used), or `intercepted_request_failed` (one request inside a TLS-intercepted connection). `status` is what the client was answered; `upstream_status` is the vendor's HTTP status when an HTTP upstream refused the request, and `upstream_message` what it said about it besides: its error headers (such as `x-brd-error`) and the text of its error page, `null` when it said nothing more. `latency_ms` is the average time until the failure. `step` is the [step]({{ site.baseurl }}/metrics#latency-where-the-time-goes) of the request it failed in (`route`, `preflight`, `connect`, `handshake` or `response`), and `steps` how long each step it went through took; both are `null` for a request refused before a proxy was chosen and inside an intercepted connection. `count` is how many identical failures the example stands for, between `first_at` and `last_at`, from every gateway; `session_id` and `steps` are the first one's. `id` stays the same while the failure recurs. `total` is how many examples are kept and `max_entries` how many are kept per kind.

`newest` marks where the log stood when the first page was read. To tell how many examples are new or have happened again since, poll the head with it:

```bash
GET /api/v1/projects/{project_id}/failures/head?since=1760526004211&kind=upstream_rejected
```

```json
{ "changed": 3, "total": 1000 }
```

#### Clearing

```bash
DELETE /api/v1/projects/{project_id}/failures
```

Discards the examples (editors and admins). The counts are part of the metrics history and stay, and new failures keep being recorded.

### Prometheus Metrics

Export a project's metrics as Prometheus text (`text/plain; version=0.0.4`):

```bash
GET /api/v1/projects/{project_id}/metrics/prometheus
```

The pool and the project's counters, as [Get Project Metrics](#get-project-metrics) reports them, with a `project` label: `octoprox_proxies_total`, `octoprox_proxies_healthy`, `octoprox_proxies_quarantined` (gauges), `octoprox_requests_total`, `octoprox_requests_success_total`, `octoprox_requests_failure_total`, `octoprox_bytes_sent_total` and `octoprox_bytes_received_total` (counters). Each connector's traffic this period is exported with `project`, `connector` and `name` labels:

| Metric | Type | Description |
|--------|------|-------------|
| `octoprox_connector_traffic_bytes` | gauge | Bytes through the connector in its current traffic period |
| `octoprox_connector_traffic_limit_bytes` | gauge | The period's limit, for connectors that have one |
| `octoprox_connector_traffic_cost` | gauge | Spend at the connector's price per GB, with a `currency` label |
| `octoprox_connector_traffic_blocked` | gauge | 1 while the connector takes no requests because of its limit |

This export needs a sign-in, like the rest of the API. How each process is doing (gateway traffic, the controller, job runs) is exported separately, without sign-in, at `/metrics` on port 8000 of every process; see [Metrics]({{ site.baseurl }}/metrics#prometheus).

### Intercepted Requests

With [TLS interception]({{ site.baseurl }}/tls-interception) on, the requests seen inside a project's intercepted connections are kept for its inspector:

```bash
GET    /api/v1/projects/{project_id}/mitm/requests
DELETE /api/v1/projects/{project_id}/mitm/requests
```

Any signed-in user reads them; clearing them needs an editor (`403` for viewers).

---

## Needs attention

What needs someone now, as the [Needs attention]({{ site.baseurl }}/metrics#needs-attention) card on a project's Overview and the Projects page show it. Every line is read from the [alerts](#alerts) Octoprox's background checks store: they check every project once a minute, and a project again within seconds of a change made through the API or the web UI (a connector, its countries, the project, its proxies or its alert settings), or of a connector's traffic limit starting or stopping to turn requests away. No request computes a line. Times are UTC without an offset, as everywhere in the API.

| Method | Path | Who | Description |
|---|---|---|---|
| GET | `/projects/{project_id}/attention` | anyone signed in | The project's lines, muted or not, their counts, and when they were checked |
| PUT | `/projects/{project_id}/attention/mutes` | editor or admin | Mute one line for everyone in the project |
| DELETE | `/projects/{project_id}/attention/mutes/{key}` | editor or admin | Unmute a line |
| GET | `/attention/projects` | anyone signed in | Every project's problem count |
| GET | `/attention/install` | anyone signed in | The install-wide lines of the Projects page |

### A project's lines

```bash
GET /api/v1/projects/{project_id}/attention
```

**Response:**
```json
{
  "generated_at": "2026-11-18T14:32:10.123456",
  "lines": [
    {
      "key": "vendor_login:3f2c9a1e-0000-4000-8000-000000000001",
      "kind": "vendor_login",
      "severity": "problem",
      "lead": "",
      "subject": "Webshare UK",
      "text": ": the vendor is rejecting its login (41% of requests in the last 15 minutes).",
      "detail": "The vendor answered 407 to 123 of 300 requests.",
      "when": {"kind": "last_at", "at": "2026-11-18T14:32:00"},
      "link": "connectors?open=3f2c9a1e-0000-4000-8000-000000000001",
      "action": "Open connector",
      "mute": null,
      "alert_id": "8d0c41f2-6b7e-4f3a-9c55-2e1d0a7b9f10",
      "since": "2026-11-18T14:20:05.311200",
      "opened_at": "2026-11-18T13:58:11.120331",
      "reopened": 1
    }
  ],
  "problems": 1,
  "warnings": 0,
  "muted": 0,
  "checked_at": "2026-11-18T14:32:02.004112",
  "pending": false,
  "stale": false
}
```

A `problem` means requests fail now; a `warning` that they are degraded or about to be. `lines` are in display order: problems first, then warnings, each by kind and then by name, so a refresh changes words in place rather than moving rows. `problems` and `warnings` count the lines whose mute does not hide them, and `muted` those it does. A line that a wider line covers (every connector turned off covering the countries nothing can serve, say) is left out of `lines` and the counts; it stays open in the project's [alerts](#alerts), where `hidden_by` names the line that covers it. A project being deleted answers no lines, and an unknown one `404`.

| Field | Meaning |
|---|---|
| `key` | Stable: the kind alone for a line about the project, or `<kind>:<id>` for one about a connector, a kind of refused request or a site |
| `kind` | What the line is about, such as `connectors_off`, `pool_no_routable`, `countries_unserved`, `vendor_login`, `connector_failing`, `refusals`, `success_rate`, `connector_traffic_blocked` or `reputation_site_blocked`; [Alerts](#alerts) lists them all |
| `lead`, `subject`, `text` | The sentence is `lead + subject + text`; the subject, such as a connector's name, is the part to show in bold |
| `detail` | Extra plain words, such as the vendor's status and the counts behind the line, or `null` |
| `when` | The one time the line shows, or `null`: `last_at` (its newest failure), `last_failed` (a connector's last failed vendor sync or cloud call) or `resets` (when a traffic period starts over) |
| `link`, `action` | The page that fixes it, relative to `/projects/{project_id}/`, and the link's label |
| `mute` | The line's mute, or `null` when it has none or a timed one has ended |
| `alert_id` | The alert behind the line, as [one alert](#one-alert) reads it |
| `since` | When the line's current spell began: the last time it came back, else when it opened. Show it for a line whose `when` is `null` |
| `opened_at` | When the alert first opened, kept when the line comes back |
| `reopened` | How many times the line came back within 15 minutes of clearing |

| Field | Meaning |
|---|---|
| `checked_at` | When the background checks last checked the project (the time of the data they read), or `null` before the first check, when empty `lines` say nothing yet |
| `pending` | A check a change asked for is still waiting. Read again every couple of seconds while it is `true` to see the change land; it turns `false` once the check is stored, or a minute after it was asked for |
| `stale` | The last check is more than three minutes old, so the lines may be out of date |

A `mute` holds the `severity` the line had when it was muted, the `duration`, `until` (its end, `null` until the line clears), `muted_by` (a username), `muted_at`, and `active`: whether it hides the line now. A line muted as a warning that has become a problem shows again, with `active` false, until it is a warning again or the mute ends.

Lines about requests cover the last 15 minutes of the flushed history, about a minute behind live traffic, and go five minutes after their last failure. A line clears as soon as a check no longer finds it.

### Muting a line

```bash
PUT /api/v1/projects/{project_id}/attention/mutes
```

```json
{"key": "vendor_login:3f2c9a1e-0000-4000-8000-000000000001", "duration": "week"}
```

`duration` is `day` (24 hours), `week` (7 days), `month` (30 days) or `until_clear`, which ends once the line has stayed gone for 15 minutes, so a line that comes and goes stays muted. The mute is shared by everyone in the project and replaces the line's earlier mute, if any. The key is that of an open alert of the project, shown on the card or covered. Answers `204`; `403` for a viewer; `404` for an unknown project, or `{"detail": "Nothing with that key needs attention now"}` for a key that is not one of the project's open alerts; `422` for a bad body. The project's [History]({{ site.baseurl }}/audit-log) records a `muted` entry with the line's sentence (`line`) and the mute's end (`muted_until`, or `it clears`).

```bash
DELETE /api/v1/projects/{project_id}/attention/mutes/{key}
```

URL-encode the key. Answers `204` whether or not the line was muted, and History records an `unmuted` entry with the line only when a mute was removed; `403` for a viewer, `404` for an unknown project. A mute until the line clears is ended by the background checks once the line has stayed gone for 15 minutes, with no History entry, since no one ended it.

### Every project's problems

```bash
GET /api/v1/attention/projects
```

```json
{"projects": [{"project_id": "3c49da3c-a997-45a0-9a26-dc3b3ae6c920", "problems": 2}]}
```

Every project not being deleted, oldest first, with the problem lines its card counts: the same stored alerts, with muted and covered lines left out. Warnings are not counted here.

### Install-wide lines

```bash
GET /api/v1/attention/install
```

```json
{
  "generated_at": "2026-11-18T14:32:10.123456",
  "lines": [
    {
      "key": "changes_not_applied",
      "severity": "warning",
      "text": "Changes since {time} have not taken effect yet. Requests still follow the earlier setup.",
      "at": "2026-11-18T14:02:11.402000",
      "admin_page": "system",
      "since": "2026-11-18T14:03:40.118204"
    }
  ],
  "checked_at": "2026-11-18T14:32:01.551730"
}
```

Problems with the install itself, in plain words, the same for every role and without deployment detail: no process names, hosts, releases, sizes or error text. Always `200`: a store that cannot be reached is itself a line. A `text` holding `{time}` has `at` set; show it in the reader's time. `admin_page` names where an admin sees more: `system` (Administration, System) or `geo` (IP attribution). These lines cannot be muted. `checked_at` is when the background checks last checked the install, or `null`.

The lines found by the checks come first, problems first, then those the api adds:

- **Found by the background checks**, with `since` set to when the line last opened or came back: `no_gateway_serving` (a problem), `changes_not_applied`, `background_behind`, `ip_databases` and `store_unreachable:database` (a process cannot reach the database). Each is an install alert: it opens once two checks about a minute apart have found it, so 1-2 minutes after it starts, and admins can read its history under [Alerts](#install-alerts).
- **Added by the api as it answers**, with `since` `null`: `store_unreachable:database` (the api could not read the alerts; then the only line), `store_unreachable:live_data` (the api could not read the live data store), `checks_behind` (the install was last checked more than three minutes ago; `at` is when) and `checks_not_running` (the install was never checked and the checks' work has waited three minutes: no worker runs the `alerts` job). One api's read can fail where another's works, so the web UI shows such a line only once two reads in a row have it.

---

## Alerts

Every line of a [Needs attention](#needs-attention) card, a project's or the install's, is kept as an alert from when it opened until it resolved, and these endpoints read them, with a project's alert settings and the install alerts' retention. The background checks store them; nothing here computes one. See [Alerts]({{ site.baseurl }}/metrics#alerts) for what the pages show.

| Method | Path | Who | Description |
|---|---|---|---|
| GET | `/projects/{project_id}/alerts` | anyone signed in | A page of the project's alerts, newest first |
| GET | `/projects/{project_id}/alerts/{alert_id}` | anyone signed in | One of the project's alerts |
| GET | `/projects/{project_id}/alert-settings` | anyone signed in | The project's alert settings in force, and the defaults |
| PUT | `/projects/{project_id}/alert-settings` | editor or admin | Replace the project's alert settings |
| GET | `/alerts/install` | admin | A page of the install's alerts, newest first |
| GET | `/alerts/install/retention` | admin | How many days the install's resolved alerts are kept |
| PUT | `/alerts/install/retention` | admin | Set how many days they are kept |

An alert opens when a check finds its line, and resolves as soon as a check no longer does. A line back within 15 minutes of resolving reopens the same alert (`reopened` counts the returns, `opened_at` stays) rather than adding one. A resolved alert is kept for the project's alert setting `keep_days`, its metrics retention when that is unset (0 keeps it forever); the install's for the install retention below. Open alerts are never deleted.

### A project's alerts

```bash
GET /api/v1/projects/{project_id}/alerts?status=all&since=2026-11-17T14:00:00&limit=50
```

| Parameter | Meaning |
|---|---|
| `status` | `open` (muted and covered alerts included), `resolved` or `all` (the default) |
| `severity` | `problem` or `warning`: an open alert's severity now, a resolved one's worst |
| `kind` | One kind, 1 to 48 characters: `connectors_off`, `pool_no_routable`, `countries_unserved`, `vendor_login`, `vendor_rate_limited`, `vendor_forbidden`, `vendor_cannot_serve`, `vendor_refused_other`, `connector_failing`, `refusals`, `success_rate`, `connector_sync_failing`, `connector_cloud_error`, `connector_no_healthy_proxy`, `connector_traffic_blocked`, `connector_traffic_near`, `connector_traffic_over_alert`, `pool_unhealthy_share`, `exits_contradicted`, `exits_outside_countries`, `reputation_site_blocked` or `connector_stuck_deleting` |
| `q` | Text in the subject, such as a connector's name, in any case; 1 to 100 characters |
| `since` | ISO time: only alerts open at any point since then (still open, or resolved at or after it) |
| `until` | ISO time: only alerts opened at or before it; now by default. Keep the first page's `until` while refreshing and paging, so alerts opened meanwhile never shift the rows; `newer` says how many wait |
| `limit` | 1 to 200, default 50 |
| `before` | The `id` of the last alert already shown, for the next page |

A time without an offset is UTC.

**Response:**
```json
{
  "alerts": [
    {
      "id": "8d0c41f2-6b7e-4f3a-9c55-2e1d0a7b9f10",
      "key": "vendor_rate_limited:3f2c9a1e-0000-4000-8000-000000000001",
      "kind": "vendor_rate_limited",
      "severity": "warning",
      "worst_severity": "problem",
      "lead": "",
      "subject": "Plainproxies AU",
      "text": ": the vendor says the account sends too many requests (12% refused in the last 15 minutes).",
      "detail": "The vendor answered 429 to 61 of 508 requests.",
      "when": {"kind": "last_at", "at": "2026-11-18T09:41:00"},
      "link": "connectors?open=3f2c9a1e-0000-4000-8000-000000000001",
      "action": "Open connector",
      "hidden_by": null,
      "opened_at": "2026-11-18T08:02:11.120331",
      "reopened_at": "2026-11-18T09:40:05.311200",
      "last_seen_at": "2026-11-18T09:42:10.004112",
      "resolved_at": null,
      "reopened": 2,
      "mute": null
    }
  ],
  "more": false,
  "open": 3,
  "resolved": 41,
  "until": "2026-11-18T09:42:30.000000",
  "newer": 0
}
```

| Field | Meaning |
|---|---|
| `id` | The alert's ID |
| `key`, `kind`, `lead`, `subject`, `text`, `detail`, `when`, `link`, `action` | As on the [card's lines](#a-projects-lines), as the last check that found the line wrote them |
| `severity` | `problem` or `warning`: now while open, when it resolved once resolved |
| `worst_severity` | The worst it reached |
| `hidden_by` | The key of the line that stands for it on the card while it is open and covered (a **Covered** alert); `null` otherwise |
| `opened_at` | When it first opened |
| `reopened_at` | When it last came back, or `null` |
| `last_seen_at` | When a check last found it |
| `resolved_at` | When it resolved, or `null` while open |
| `reopened` | How many times it came back within 15 minutes of resolving |
| `mute` | On an open alert, its mute as on the card, or `null` |

`more` says older alerts remain. `open` and `resolved` count the alerts under every filter but `status`, `until` and `before`; `newer` counts those under every filter opened after `until`. `until` is the value the page used: send it back with the next page and the refreshes. `404` for an unknown project; `422` for a bad parameter.

### One alert

```bash
GET /api/v1/projects/{project_id}/alerts/{alert_id}
```

One alert as the list shows it, also when it is not on a page already read, for a link to it. `404` with `{"detail": "Alert not found"}` for an ID that is not one of the project's alerts, and `{"detail": "Project not found"}` for an unknown project.

### Alert settings

Where each of the project's lines starts, as the **Alert settings** tab of Project settings edits them (see [Thresholds]({{ site.baseurl }}/metrics#thresholds)).

```bash
GET /api/v1/projects/{project_id}/alert-settings
```

```json
{
  "settings": {"success_rate": true, "success_warning_below": 70, "vendor_min": 20, "keep_days": null},
  "defaults": {"success_rate": true, "success_warning_below": 80, "vendor_min": 10, "keep_days": null}
}
```

Shortened: `settings` holds every field of the table below as in force, and `defaults` every default beside it. `404` for an unknown project.

```bash
PUT /api/v1/projects/{project_id}/alert-settings
```

```json
{"success_warning_below": 70, "vendor_min": 20}
```

The body replaces the project's settings: a field left out is back at its default, so `{}` resets them all. Only the values that differ from the defaults are kept, so the others follow the defaults. Answers `200` with the new `settings` and the `defaults`; `403` for a viewer, `404` for an unknown project. The project is checked again at once, and its [History]({{ site.baseurl }}/audit-log) records an `updated` entry with each changed setting as `alerts.<field>` (`alerts.success_warning_below: [80, 70]`); a save that changes nothing records nothing. Nothing here changes how requests are routed.

Percent fields are whole percent.

| Field | Default | Range | What it decides |
|---|---|---|---|
| `success_rate` | `true` | boolean | Whether a low success rate is a line at all |
| `success_warning_below` | 80 | 1-100 | A warning when the share of requests that reached a proxy and succeeded in the last 15 minutes is below this |
| `success_problem_below` | 50 | 0-99 | A problem below this; 0 never makes it a problem |
| `success_min_requests` | 100 | 1-1,000,000 | The requests in the last 15 minutes before the success rate counts |
| `refusals_min` | 10 | 1-1,000,000 | The requests of one kind turned away before a proxy before they make a line |
| `refusals_warning_percent` | 5 | 1-100 | A warning from this share of the project's requests turned away |
| `refusals_problem_percent` | 25 | 1-100 | A problem from this share |
| `invalid_location_min` | 10 | 1-1,000,000 | The requests asking for a location that could not be read before they make a line (a warning) |
| `vendor_min` | 10 | 1-1,000,000 | The requests a vendor refused through a connector before they make a line |
| `vendor_login_problem_percent` | 20 | 1-100 | A problem from this share of a connector's requests refused with 407 |
| `vendor_rate_limited_warning_percent` | 5 | 1-100 | A warning from this share refused with 429 |
| `vendor_rate_limited_problem_percent` | 50 | 1-100 | A problem from this share refused with 429 |
| `vendor_forbidden_warning_percent` | 5 | 1-100 | A warning from this share refused with 403 |
| `vendor_forbidden_problem_percent` | 50 | 1-100 | A problem from this share refused with 403 |
| `vendor_cannot_serve_warning_percent` | 30 | 1-100 | A warning from this share refused with 400, 502 or 503 |
| `vendor_other_warning_percent` | 30 | 1-100 | A warning from this share refused otherwise |
| `connector_failing_min_requests` | 50 | 1-1,000,000 | A connector's requests in the last 15 minutes before its failures count |
| `connector_failing_warning_percent` | 30 | 1-100 | A warning from this share of a connector's requests failing |
| `connector_failing_problem_percent` | 80 | 1-100 | A problem from this share |
| `connector_errors_min` | 1 | 1-100 | Failed vendor list syncs or cloud calls in a row before a connector's line shows |
| `stuck_deleting_minutes` | 30 | 5-10,080 | Minutes a connector may take to finish deleting before its line shows |
| `unhealthy_share_percent` | 25 | 1-100 | A warning from this share of the project's proxies failing their health checks |
| `unhealthy_min` | 3 | 1-1,000,000 | And at least this many |
| `exits_outside_percent` | 10 | 1-100 | A warning from this share of a connector's proxies exiting outside its listed countries |
| `exits_outside_min` | 3 | 1-1,000,000 | And at least this many |
| `exits_contradicted_percent` | 25 | 1-100 | A warning from this share of a connector's checked proxies exiting in another country than its vendor says |
| `exits_contradicted_min` | 3 | 1-1,000,000 | And at least this many |
| `site_blocked_percent` | 50 | 1-100 | A warning once a site blocks at least this share of the requests on every connector that serves it, by [exit reputation]({{ site.baseurl }}/exit-reputation) |
| `keep_days` | `null` | 0-36,500 or `null` | Days resolved alerts are kept; `null` follows the project's `metrics_retention_days`, 0 keeps them forever |

A field the body does not know (`Extra inputs are not permitted`), a value out of range, or two values that disagree is refused with `422`, each with its message in `detail[].msg`:

- `Value error, <warning field> cannot be above <problem field>`, for `refusals_*`, `vendor_rate_limited_*`, `vendor_forbidden_*` and `connector_failing_*`
- `Value error, success_problem_below must be below success_warning_below`

### Install alerts

The install-wide lines of the Projects page over time, for admins (`403` for anyone else): the ones the background checks found, not the ones the api adds as it answers.

```bash
GET /api/v1/alerts/install?status=resolved&since=2026-11-11T00:00:00
```

The parameters and the answer are a project list's, without `q`; `kind` is one of `no_gateway_serving`, `store_unreachable`, `changes_not_applied`, `background_behind` and `ip_databases`. An install alert has an empty `lead`, `subject` and `action`, `link` set to the admin page (`system` or `geo`), and no `mute`; a `text` holding `{time}` has `when` set, of kind `at`, to show in the reader's time.

```bash
GET /api/v1/alerts/install/retention
```

```json
{"days": 90}
```

How many days the install's alerts are kept after they resolve: 90 until an admin sets it, 0 keeping them forever.

```bash
PUT /api/v1/alerts/install/retention
```

```json
{"days": 30}
```

`days` from 0 to 36500. Answers `200` with the value saved, `422` out of range. The change is recorded itself, on the system settings, and applies within the hour, when older alerts are deleted.

---

## Backup & Migration

Admin-only endpoints for exporting the entire Octoprox setup to a single
encrypted file and restoring it on the same or another install. Useful for
disaster recovery and for migrating between deployments.

### Export Backup

```bash
POST /api/v1/backup/export
```

**Request:**
```json
{
  "passphrase": "correct horse battery staple",
  "include_metrics": false,
  "include_database_files": false
}
```

| Field | Type | Notes |
|-------|------|-------|
| `passphrase` | string | Minimum 8 characters. Required again to import - it cannot be recovered. |
| `include_metrics` | boolean | Include history: proxy/project metrics, exit IP observations, per-connector exit IPs, [exit reputation]({{ site.baseurl }}/exit-reputation) records, and the [alerts](#alerts): each project's, and the install's resolved ones. Default `false` (smaller file). |
| `include_database_files` | boolean | Include the bytes of uploaded and downloaded IP databases. Default `false`; a city database is tens of megabytes. Their rows are always included, so without the files the restored instance lists them without a file, ready to re-upload or refresh. |

**Response:** the backup file as `application/octet-stream` with a
`Content-Disposition: attachment; filename="octoprox-backup-YYYY-MM-DD.opbak"`
header.

The file covers users (including password hashes), projects, credentials,
connectors, proxies, custom provider descriptors, the IP attribution settings
and database records, the [audit log](#audit-log), and the install's system
settings (the audit retention, the install alert retention and the Octoprox
Enterprise license key), under the payload keys `audit_log` and
`system_settings`. The audit log travels whether or not history is included.
Optionally the file also carries the history (metrics, attribution
observations, exit reputation records, each project's alerts, and the
install's resolved alerts) and the IP database files. A project's alert
settings travel with the project.

### Import Backup

```bash
POST /api/v1/backup/import
Content-Type: multipart/form-data
```

| Form field | Type | Notes |
|------------|------|-------|
| `file` | file | The `.opbak` file produced by export. |
| `passphrase` | string | The passphrase used when exporting. |
| `mode` | string | Only `replace` is supported (default). |
| `keep_current_user` | boolean | Default `false`. See below. |

**Import replaces all existing data** on the install, except its audit history,
its install alerts and its license key. The backup's audit entries and resolved
install alerts are added to the install's own, one the install already has left
as it is; a project's alerts are restored with the project, and its open ones
are brought up to date by the next check. The backup's system settings
are taken only when the install has never saved its own, and a missing license
key is filled in from the backup's. The import itself is recorded last, in the
import's transaction: one `imported` entry on `backup` `1` naming the admin, with
the backup's `backup_created_at`, `app_version`, `includes_metrics` and
`kept_current_user` in its details. The wipe and restore run
in a single transaction, so a failure leaves the existing data untouched; the
last step rebuilds the running metric totals from the imported history (the
totals themselves are never in the file). Afterwards the Redis keys of the
projects the import removed (MITM captures, failure examples) are deleted, and
the api that answered reads the imported provider descriptors and attribution
settings at once. A cloud connector the install had that the backup does not
carry is deleted as [Delete Connector](#delete-connector) deletes it, so its
instances are terminated first: the import moves it, with a copy of its
credential, into a project named `<project> (deleting)` (a number is added if
the backup has that name), which is pending deletion and goes with its last
connector, as after [Delete Project](#delete-project). The answer counts them in
`deleting_cloud_connectors`. A connector the backup carries runs as its
restored rows say, and its instances without a restored row are terminated
within the hour. Every other process follows the change entry the import
appends: the controller compiles the imported definitions and the gateways
receive them through the routing feed, and the other api and worker processes
read them again, within moments.

With `keep_current_user=true` the calling admin's own account survives the
wipe, so an admin importing a backup taken from another instance is not locked
out. Imported users that would collide with the kept account are adjusted:

- same `id` → the imported user receives a fresh id
- same `username` → the imported user is renamed to `<username>-imported`
  (then `-imported-2`, `-imported-3`, … if needed)
- same non-empty `email` → the imported user's email is cleared

With `keep_current_user=false` (the default) users are restored exactly as they
are in the backup; the current session's user may no longer exist, so log in
again with credentials that are valid in the backup.

**Response:**
```json
{
  "users": 3,
  "projects": 2,
  "credentials": 1,
  "connectors": 2,
  "proxies": 14,
  "proxy_metrics": 0,
  "project_metrics": 0,
  "alerts": 0,
  "provider_descriptors": 0,
  "geo_settings": 1,
  "geo_databases": 2,
  "geo_database_blobs": 0,
  "ip_observations": 0,
  "connector_exit_ips": 0,
  "exit_ip_reputation": 0,
  "system_settings": 0,
  "audit_log": 48,
  "install_alerts": 0,
  "kept_current_user": true,
  "user_conflicts": [
    {
      "original_username": "admin",
      "new_username": "admin-imported",
      "new_id": true,
      "email_cleared": false
    }
  ]
}
```

`users` counts imported rows only; the kept account is not included.
`audit_log` counts the backup's entries added to the install's, `alerts` the
projects' alerts restored and `install_alerts` the backup's install alerts added
to the install's, and `system_settings` is 1 when the backup's settings were taken or filled in the
install's missing license key.

**Errors (HTTP 400):**

- Incorrect passphrase or corrupt file.
- File is not an Octoprox backup.
- Backup was created by a newer Octoprox (`format_version` too high).
- Schema mismatch: the backup's Alembic revision differs from this install's.
  Upgrade both installs to the same Octoprox version, then retry. This check
  runs before decryption, so it does not require the passphrase.
- `keep_current_user=true` but the caller's account has no database row.

### Backup File Format

An `.opbak` file is a small JSON envelope. The metadata is plain text so an
importer can check compatibility before asking for the passphrase; the data is
encrypted:

```json
{
  "format": "octoprox-backup",
  "format_version": 1,
  "created_at": "2026-09-04T10:15:00+00:00",
  "app_version": "1.0.1",
  "schema_version": "<alembic revision>",
  "includes_metrics": false,
  "kdf": { "algo": "pbkdf2-sha256", "iterations": 600000, "salt": "<base64>" },
  "ciphertext": "<base64 Fernet token>"
}
```

The ciphertext is gzipped JSON encrypted with Fernet (AES-128-CBC + HMAC-SHA256).
The key is derived from the passphrase with PBKDF2-HMAC-SHA256 and a random
per-file salt. Treat the file as sensitive: it contains password hashes,
provider credentials and the license key, protected only by the passphrase.

---

## Audit Log

Every change a person makes is recorded with who made it, when, in which request, and the fields it changed, secrets masked. See [Audit Log]({{ site.baseurl }}/audit-log) for what is recorded and what is not. Each entity's history is read on its own; the install-wide list, its export and the license are [Octoprox Enterprise endpoints](#octoprox-enterprise-endpoints).

| Method | Path | Description |
|---|---|---|
| GET | `/audit/{type}/{id}` | One entity's entries, newest first, as `entries`, and `more` while older ones remain. `limit` 1 to 200, default 50; `before`, the `id` of the oldest entry already shown, reads the next page |
| GET | `/audit/retention` | How many `days` entries are kept, 0 for ever (admin), as the `PUT` below answers |
| PUT | `/audit/retention` | Set the `days` (admin), from 0 to 36500, 0 keeping entries forever. The change is recorded itself, on the system settings, and applies within the hour, when older entries are deleted |

The type is one of the project's, whose history every signed-in user may read, as they may read the entities: `project`, `connector`, `credential`, `proxy`, `wireguard_device`, `openvpn_device`. Or one of the install's, which answer 403 to anyone but an admin: `provider`, `user`, `sign_in_provider`, `geo_settings`, `geo_database`, `wireguard_settings`, `openvpn_settings`, `system_settings`, `backup`. Any other type is a 422. The id is the entity's ID; a sign-in provider's is its kind (`google`, `microsoft`, `github`), and the install's settings and its restores use `1`. A deleted entity's history is still read by its ID.

```bash
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8000/api/v1/audit/connector/5f6eef8a-4603-4cad-8c57-dd6a1b9e351b?limit=50"
# The next page: add &before=<id of the last entry returned>

curl -X PUT -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
  -d '{"days": 365}' http://localhost:8000/api/v1/audit/retention
# {"days": 365}
```

**An entry:**
```json
{
  "id": "0bed08cd-9ab7-40a6-bc71-1cdf0f56b95b",
  "created_at": "2026-11-10T14:03:22.123456",
  "action": "updated",
  "entity_type": "connector",
  "entity_id": "5f6eef8a-4603-4cad-8c57-dd6a1b9e351b",
  "entity_name": "brightdata-eu",
  "project_id": "3c49da3c-a997-45a0-9a26-dc3b3ae6c920",
  "actor_id": "9d59c651-da69-42f4-8222-7b5a766d0188",
  "actor": "ivan",
  "request_id": "5c7db5e7ad1f4e56bb90ef86bcfb38ab",
  "changes": {"routing_config.weight": [null, 3], "config.zone_password": ["********", "********"]},
  "details": {}
}
```

- `id`: the entry's ID, kept by backups.
- `created_at`: when, in UTC.
- `action`: `created`, `updated`, `deleted`, `imported`, `traffic_reset`, `invited`, `invite_reissued`, `rotated`, `muted` or `unmuted`; on Octoprox Enterprise also `signed_in`, `sign_in_failed`, `token_issued`, `token_first_used`, `token_revoked` and `identity_unlinked`.
- `entity_type`, `entity_id`: what the entry is about. `entity_id` is empty for a sign-in under a name no user has.
- `entity_name`: its name at the time.
- `project_id`: the project, for a project's entities; null otherwise.
- `actor_id`, `actor`: the user who acted, by ID and by username at the time. `actor` is empty when nobody was signed in (a sign-in refused under an unknown name), and `actor_id` is null then, and for provider entries carried over from releases before the audit log, which kept only the username.
- `request_id`: the request's `X-Request-ID` (see [Request IDs](#request-ids)).
- `changes`: each changed field as `[before, after]`: a created entity's fields as `[null, value]`, a deleted one's as `[value, null]`, settings kept as a set of keys one key down (`routing_config.weight`), and a secret as `"********"` while set and `null` while empty.
- `details`: what else the action carries: for an entry made with a personal access token, the token's name and ID as `auth_token` and `auth_token_id`; how an entity was created as `via` (`upload`, `invite`); a provider change's `hosts_added` and `hosts_removed`, the Countries page's `country`, an OpenVPN CA rotation's `devices_reissued`, and a restore's `backup_created_at`, `app_version`, `includes_metrics` and `kept_current_user`. On Octoprox Enterprise also a sign-in's `method`, `provider`, `linked`, `reason`, `client_ip` and `user_agent`, a token event's `token_id`, `token_name`, `token_prefix` and `expires_at` (the token issued, used or revoked; `auth_token` names the token a revocation was made with), and an unlinked account's `provider` and `email`.

The former `GET /api/v1/providers/{id}/audit` is gone: a provider's history is `GET /api/v1/audit/provider/{id}`, with field changes in place of descriptor snapshots.

### Octoprox Enterprise endpoints

These exist only on the Octoprox Enterprise image (ask [Edition](#edition) first), and all are for admins:

| Method | Path | Description |
|---|---|---|
| GET | `/license` | The license as the License card shows it: `state` (`none`, `invalid`, `active`, `grace`, `expired`), `organisation`, `license_id`, `expires` and `grace_ends` (dates, UTC), `seats`, `seats_used`, `features`, `banner` (the sentence admins see on every page, or null), `refusal` (why a stored key is refused) |
| PUT | `/license` | Store the `key` sent once it checks out (whitespace in it is ignored), answering the status as `GET` does. A key that does not check out, or a license whose grace period has already ended, is a 400 whose `detail` gives the reason, and nothing is saved. Recorded on the system settings, the key masked |
| GET | `/audit` | Every entry of the install, newest first, as `entries`, with the exact `total`. Filters: `entity_type`, `action`, `actor` (a username, in any case), `project_id`, and `until` (ISO time: entries up to it; without an offset it is UTC). Paging with `limit` (1 to 500, default 100) and `offset` |
| GET | `/audit/export` | Every entry under the same filters, oldest first, as JSON lines: a line is an entry as above, its `created_at` ending in `Z` |

The export is `application/x-ndjson`, sent as an attachment named `octoprox-audit-<UTC time>.jsonl`. It reads the log as it streams, so entries recorded meanwhile can follow at its end; an `until` already past gives a set that cannot grow. `GET /audit` and the export answer 403 `An Octoprox Enterprise license is needed` unless a genuine license with the audit log is stored; one past its grace period still reads. See [Audit Log]({{ site.baseurl }}/audit-log#octoprox-enterprise).

```bash
curl -H "Authorization: Bearer <admin token>" -o audit.jsonl \
  "http://localhost:8000/api/v1/audit/export?action=sign_in_failed&until=2026-11-10T00:00:00Z"
```

---

## System Statistics

Admin-only. One endpoint describing the install as a cluster: every process and what it is doing, the controller, the routing feed and each gateway's copy of it, the work streams, and what the install holds and costs in Postgres and Redis. This is what the **Administration → System** page renders.

```bash
GET /api/v1/system/stats
```

Editors and viewers receive `403`.

**Response sections:**

| Section | Contents |
|---------|----------|
| `instances` | Every live process, grouped by the role it registered with (`all`, `api`, `gateway`, `controller`, `worker`; a role with no live process is absent), each list sorted by instance id. An entry carries `instance_id`, `role`, the process's last heartbeat `snapshot` (below) and `age_seconds`, how old that snapshot is. `{}` when Redis cannot be read. Echo processes do not heartbeat and are never listed. |
| `controller` | `leader`: the instance id holding the controller lease (`null` while none does, during a failover); `changes` and `compiled_at`: how far the leader's last compile read the change stream (an entry ID) and when it was made, from its heartbeat; `changes_head`: the change stream's newest entry ID, read now, ahead of `changes` while the leader has changes to compile (`null` while there is no stream); `standby_poll_ms` and `lease_ttl_ms`: how often a standby asks for the lease and how long a failed leader holds it, the numbers the page words the takeover from; `error` when Redis cannot be read. |
| `feed` | The routing feed in Redis: `digest`, `changes`, `compiled_at` and `sections` of its head (all `null` while there is none), and `gateways`: per gateway the `digest` and `changes` it applied, `in_sync` (it applied the current head) and `age_seconds` since it last applied an update, which grows while nothing changes. `error` when Redis cannot be read or the head was published by another release. |
| `streams` | `by_name`: every work stream by name, `jobs.<kind>` for each of the fifteen job kinds, then `sightings`, `observations` and `exit_reports`, each with `length` (entries queued and running, since workers delete each entry once handled), `pending` (delivered to a worker and not acknowledged yet: running, or waiting to be reclaimed), `queued` (`length` less `pending`, never below zero: the backlog), `consumers` (worker processes in the `workers` group, a stopped one until index hygiene removes it, within the hour) and `oldest_seconds` (the age of the oldest entry, queued or running, by Redis's clock; `null` when the stream is empty). A stream nobody created yet reads as zeros. `error` when Redis cannot be read. |
| `inventory` | Exact counts of projects, credentials, connectors, proxies, users and providers, and `proxies_by_status` from the proxies' status column. |
| `projects` | Per-project credential, connector and proxy counts, busiest first. |
| `database` | Database size, per-table size (indexes included) and server connections. Each api process's own pool is in its snapshot. |
| `redis` | Memory, throughput, hit rate and a breakdown of the keyspace by purpose; keys without the `o6:` prefix, left by 5.x, are grouped as *Earlier releases*. |

The answer is the same whichever api process the load balancer picked: every process publishes its own state, and the api reads them all. Every section read from Redis reports its own `error`, and the Postgres sections still render without Redis, so the page stays useful during the outage it should explain. Redis's own consumer-group `lag` is never used: it is unknown whenever an entry deleted out of order lies past the group's position, which is when a backlog builds.

**A process's snapshot.** Every process except echo publishes a snapshot every 5 seconds, under a key that expires 10 seconds later; a process that stops withdraws it at once.

| Field | Notes |
|-------|-------|
| `runtime` | `version`, `role`, `instance_id`, `pid`, `started_at`, `uptime_seconds`, and `loop_lag_max_ms` / `loop_lag_mean_ms`: how late the event loop ran timers over the last minute (null before the first minute). A worst case over a second is also logged as `Event loop running behind`. |
| `ready`, `reasons` | Whether the process answers `/ready` with 200, and why not, as `/ready` says it (`"gateway: feed not applied yet"`). |
| `tasks` | The process's background loops and their run counters (below). |
| `streams` | Each event stream the process follows (`changes`, `runtime`, `feed`): `position` (the last entry it read, or the newest at its last full read; `0-0` when there was no stream), `lag_seconds` (how far behind the newest entry its last read found it), `entries` read, `full_reads` by reason (`start`, `gap`, `reconnect`, `reconcile`) and `last_full_read_at`. In role `all`, `changes` is the worker's definitions refresher's. |
| `api` | An api process's Postgres pool (`pool_size`, `pool_checked_out`, `pool_overflow`). |
| `gateway` | A gateway's proxy listener (`listener_port`, `listening`, `active_connections`); the feed it applied (`feed_digest`, `feed_changes`, `feed_applied_at`, `feed_age_seconds`); what it keeps out of routing on its own (`retired_slots`, `local_quarantines`); `in_flight` transfers; its traffic since it started (`requests`, `successes`, `failures`, `bytes_sent`, `bytes_received`, `failures_by_kind`); its buffers (`pending_metric_deltas`, `metric_flush_failures`, `rate_limit_errors`, `observations_pending`, `observations_dropped`, `sightings_pending`, `sightings_dropped`, `failure_log_dropped`, `mitm_captures_dropped`); `preflight_checks` and `preflight_rejections`; its IP databases (`geo_databases_loaded`, `geo_database_errors`, `geo_databases_missing`); and `tunnels`, one entry per tunnel protocol it terminates. |
| `controller` | `is_leader` and `lease_terms`; the last compile (`changes`, `compiled_at`, `compiles`, `compile_ms`, `compile_error`) and publish (`publishes`, `published_at`, `publish_ms`, `publish_error`, `sections`, `sections_written`, `sections_removed`); `quarantined` proxies and `blocked_connectors` as published. A standby reports what it last saw while it led. |
| `worker` | `jobs`: per job kind it takes, `runs`, `failures`, `consecutive_failures` (the failures in a row up to now, as for a loop), `duration_seconds` and `in_flight`; `sightings_consumed` and `sightings_dropped`, `observations_consumed` and `observations_dropped`, `exit_reports_consumed` and `exit_reports_dropped` (dropped: given up after repeated deliveries); `exit_reports_dropped_by_reason`, the exit reports its checks dropped, by reason (`project`, `connector`, `ip_needed`, `ip_not_seen`; `{}` from a worker of an earlier release), which the page lists under the worker's **Exit reports** row. |

A process of role `all` carries all four role sections. `snapshot` is `null` for a process that could not build one, or that runs another release; it is listed anyway.

**Cost.** Entity counts are exact. Table row counts are planner estimates
(`pg_class.reltuples`), so they stay cheap on metric tables with millions of
rows, drift until the next `ANALYZE`, and are `null` on a table autovacuum has
not reached yet. The work streams are read in one round trip (`XLEN`,
`XINFO GROUPS` and the oldest entry of each, and Redis's `TIME`). The Redis
keyspace breakdown is a bounded `SCAN`; past 50,000 keys it reports a sample
and sets `"truncated": true`.

**Example (abridged):**
```json
{
  "generated_at": "2026-10-18T09:12:35.740343",
  "instances": {
    "api": [
      { "instance_id": "api-1", "role": "api", "age_seconds": 2.1,
        "snapshot": {
          "runtime": { "version": "6.0.0", "role": "api", "instance_id": "api-1", "uptime_seconds": 3841.2 },
          "ready": true, "reasons": [], "tasks": [],
          "streams": { "changes": { "position": "1760778750114-0", "lag_seconds": 0.0, "entries": 412,
                                    "full_reads": { "start": 1, "reconcile": 1 }, "last_full_read_at": "2026-10-18T08:12:30.114221" } },
          "api": { "pool_size": 5, "pool_checked_out": 1, "pool_overflow": 0 },
          "gateway": null, "controller": null, "worker": null } }
    ],
    "gateway": [ { "instance_id": "gateway-1", "role": "gateway", "age_seconds": 3.4, "snapshot": { "…": "…" } } ],
    "controller": [ { "instance_id": "controller-1", "role": "controller", "age_seconds": 1.0, "snapshot": { "…": "…" } } ],
    "worker": [ { "instance_id": "worker-1", "role": "worker", "age_seconds": 4.2, "snapshot": { "…": "…" } } ]
  },
  "controller": { "leader": "controller-1", "changes": "1760778750114-0", "compiled_at": "2026-10-18T09:12:30.114221",
                  "changes_head": "1760778750114-0", "standby_poll_ms": 1000, "lease_ttl_ms": 30000, "error": null },
  "feed": {
    "digest": "9c1f…", "changes": "1760778750114-0", "compiled_at": "2026-10-18T09:12:30.114221", "sections": 10,
    "gateways": [ { "instance_id": "gateway-1", "digest": "9c1f…", "changes": "1760778750114-0", "in_sync": true, "age_seconds": 5.2 } ],
    "error": null
  },
  "streams": {
    "by_name": {
      "jobs.health": { "length": 220, "pending": 200, "queued": 20, "consumers": 2, "oldest_seconds": 1.4 },
      "jobs.sync": { "length": 0, "pending": 0, "queued": 0, "consumers": 2, "oldest_seconds": null },
      "sightings": { "length": 3, "pending": 0, "queued": 3, "consumers": 2, "oldest_seconds": 0.2 }
    },
    "error": null
  },
  "inventory": {
    "projects": 2, "credentials": 8, "connectors": 9, "connectors_enabled": 2,
    "proxies": 29, "users": 1, "users_by_role": { "admin": 1 }, "users_by_kind": { "person": 1 },
    "proxies_by_status": { "healthy": 19, "unhealthy": 10 }
  },
  "database": {
    "name": "octoprox",
    "size_bytes": 8985623,
    "tables": [
      { "name": "proxies", "row_estimate": 29, "total_bytes": 294912,
        "table_bytes": 65536, "index_bytes": 229376,
        "dead_rows": 4, "last_vacuum_at": "2026-10-10T06:12:40Z" }
    ],
    "backends": 9, "error": null
  },
  "redis": {
    "used_memory_bytes": 1558312, "total_keys": 33, "ops_per_sec": 4,
    "groups": [ { "label": "Routing feed", "keys": 3 } ],
    "truncated": false
  }
}
```

A task in `tasks` looks like this:

```json
{ "name": "feed_builder", "scope": "singleton", "lease": "controller",
  "state": "running", "error": null, "interval_seconds": 1,
  "runs": 412, "idle_runs": 0, "failures": 1, "consecutive_failures": 0,
  "overruns": 3, "consecutive_overruns": 0,
  "last_overrun_at": "2026-10-18T07:31:55.417002",
  "last_run_at": "2026-10-18T09:12:30.114221", "last_duration_ms": 84.2,
  "avg_duration_ms": 61.9, "max_duration_ms": 512.7,
  "last_error": "TimeoutError: ", "last_error_at": "2026-10-18T06:04:11.002913" }
```

**Two kinds of loop health.** `state` and `error` describe the asyncio task:
not `running` means the loop in that process has stopped, and `error` carries
the exception that ended it. The run counters describe the cycles *inside* the
loop - one compile, one batch of a job kind, one metering window - so a loop
that raises every cycle and recovers is still `running`, and only
`consecutive_failures` and `last_error` say it is broken. Counters are
per-process and reset with it; a cycle cancelled at shutdown is not counted.
A job runner's runs are the batches it ran, and its failures the batches whose
run raised.

**Runs that had nothing to do.** `idle_runs` is the subset of `runs` where the
loop ticked and returned early - a feed read that found the head already
applied, a metering window with nothing to write, a stream read that timed out
with no entry. Subtract it from `runs` for the cycles that did something. The
split matters because the two numbers answer different questions: a gateway
taking no traffic wakes its loops all day without doing anything, and without
`idle_runs` that reads as busy loops. The durations describe the working cycles
only - an early return is not a measurement of the work it skipped. A loop with
no idle path (the heartbeat) reports `0`.

**Cadence and overruns.** `interval_seconds` is the interval the loop was
started with, declared by the loop itself so it is the value actually slept on
rather than a config setting read elsewhere - worth knowing, because a process
configured differently from the rest will say so here. A loop that acts on
stream entries rather than a timer declares none. `overruns` counts cycles that
took longer than that interval: a loop cannot start its next cycle until the
current one returns, so those cycles pushed it off its cadence, and its work
happened less often than configured.

`overruns` is a lifetime count, so read it with `consecutive_overruns`, which
the first cycle back inside the cadence resets: that pair distinguishes "slow
right now" from "hit one slow cycle during a restart and has been fine since",
and `last_overrun_at` says when the most recent one was. `failures` /
`consecutive_failures` work the same way. A climbing `consecutive_overruns`
with `failures` at zero is the signature of a loop that needs a longer
interval or less to do, rather than one that is broken.

**Which loop runs where.** `scope` is `singleton` for the controller's loops,
the feed builder (`feed_builder`) and one job producer per periodic kind
(`job_producer_health`, `job_producer_metrics_flush`, ...), which act only in
the leading controller, under the one `controller` lease their `lease` names;
it is `instance` for the loops every process of a role runs. A standby
controller's singleton loops are legitimately at `"runs": 0`. The job runners
of a worker are named after their kind (`job_runner_health`), one per kind it
takes, beside `sighting_ingest`, `observation_ingest` and `exit_report_ingest`.

**How the System page reads it.**

- **The work streams table** shows one row per stream: length, pending, queued, consumers and the oldest entry's age. Two hints read the numbers: *queued with no live worker: no worker takes this kind* shows when entries are queued and either the group has no consumer or none of the live workers, by their heartbeats (`worker.jobs`; every worker takes the sightings, the observations and the exit reports), takes the stream, so it shows within a heartbeat after the last such worker stops, while the group still lists it as a consumer until index hygiene removes it. *Queued stays above zero: too few workers for this kind* shows once every one of the page's own readings has found a backlog for a minute, since a single reading can land in a burst of periodic jobs that drains in milliseconds.
- **Positions are times.** Every "how far" is an entry ID of a stream, shown as the time it carries: the leader's compile, the feed head's, each gateway's applied feed, and each api's and worker's definitions (their **Definitions** rows: how far the process has read the change stream, and how often it read in full instead, by reason). A position at or past `changes_head` is *in sync*; one behind it reads *behind, at <time>*, where the process has read through, coloured as late only once the newest change is 30 seconds older than the process's snapshot, so the moment between a save and the next heartbeat is not flagged. `0-0` reads *before any change*. IDs compare as two numbers, milliseconds then sequence, never as text.
- **The takeover** is worded from `standby_poll_ms` and `lease_ttl_ms`: within a poll of the leader stopping cleanly, or within the TTL and a poll of it failing, each rounded up to whole seconds.

### System Topology

```bash
GET /api/v1/system/topology
```

Admin-only. The cluster sections of `/system/stats` alone, `generated_at`, `instances`, `controller`, `feed` and `streams`, in the same shape. It reads Redis only, without the keyspace breakdown and the table sizes, so it is cheap enough to poll every 5 seconds, the heartbeats' cadence. The System page's **Topology** view polls it while the view is open.

**How the Topology view reads it.** Every live process sits in the column of each runtime its snapshot carries, so a process of role `all` shows in all four. The arrows are the streams between the roles: the change stream from the api to the leading controller, the routing feed from the controller to the gateways, and the work streams from the api, the controller and the gateways to the workers. Rates (requests and bytes per second for a gateway, job runs per second for a worker) are the difference between two of a process's snapshots over the difference of their `uptime_seconds`, so they are timed on the process's own clock and a snapshot read twice counts once.

### System Trends

```bash
GET /api/v1/system/stats/history?range=24h
```

Admin-only. Returns the gauge history behind the **Trends** charts on the
System page. Ranges: `1h`, `24h` (raw snapshots) and `7d`, `30d`, `90d`
(averaged into buckets).

Snapshots are written by the `snapshot` job, which the controller publishes
every `system.metrics_interval` and a worker runs, so the series reads the same
from every api process. The interval is reported from the api's settings, so
set `system.metrics_interval` alike on the controller, the workers and the api.

**Gauges, not counters.** These are readings like "how big is the database",
so downsampling a range **averages** them. Summing, as the proxy and project
metrics pipelines do for request counts, would invent a number that was never
true at any instant. `bucket_seconds` is `null` for raw ranges and set when
points are averages, so a chart can say which it is showing.

| Field | Notes |
|-------|-------|
| `snapshots` | Points oldest-first: database and Redis size, Redis keys, entity counts, proxy totals by health. |
| `table_growth` | Per-table size at both edges of the window plus the delta, biggest mover first. A difference between two points, so no bucketing is involved. |
| `bucket_seconds` | Bucket width, or `null` when points are raw. |
| `interval_seconds` | The configured snapshot cadence, so a client can tell a real gap from an expected one. |

**Configuration** (`config/*.yaml`, or `OCTOPROX_SYSTEM_METRICS_*` env vars):

```yaml
system:
  metrics_interval: 300        # seconds between snapshots; 0 takes none
  metrics_retention_days: 90   # 0 keeps snapshots forever
```

The interval is floored at 60 seconds. At the default cadence this is one row
every five minutes for the whole install - about 26k rows over the 90-day
window - so retention alone keeps it bounded and there are no compaction tiers
like the ones `proxy_metrics` and `project_metrics` use.

**Example:**
```json
{
  "range": "7d",
  "bucket_seconds": 3600,
  "interval_seconds": 300,
  "snapshots": [
    {
      "timestamp": "2026-09-19T21:00:00",
      "database_size_bytes": 8985623, "redis_memory_bytes": 1558312, "redis_keys": 33,
      "projects": 2, "credentials": 8, "connectors": 9, "connectors_enabled": 2, "users": 1,
      "proxies_total": 29, "proxies_healthy": 19, "proxies_unhealthy": 10
    }
  ],
  "table_growth": [
    { "name": "project_metrics", "first_bytes": 131072, "last_bytes": 212992, "delta_bytes": 81920 }
  ]
}
```

---

## IP Attribution

All endpoints live under `/api/v1/geo`. Reads need any authenticated user; writes need an admin. See [IP Attribution]({{ site.baseurl }}/ip-attribution) for the concepts.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/geo/settings` | Install-wide settings (default conflict rule, `default_city_radius_km`, echo endpoint, preflight, retention), whether an admin saved them, and the databases the api process that answered has open |
| PUT | `/geo/settings` | Replace the settings (body: the `settings` object). Applies everywhere within moments, as its change entry reaches each process; a change of the default conflict rule or city radius also queues a re-attribution of every proxy |
| GET | `/geo/databases` | Every database: stored rows plus config-file entries, with `loaded_here` and `load_error` (the api process that answered), `gateways_loaded` out of `gateways_total` (how many live gateways have it open, from their heartbeats) and `gateway_errors` (why not, by gateway instance id), and, for a scheduled download, `last_update_at` (the last check, whether or not the file changed) and `next_update_at` (when the schedule is next due; null without a schedule, while `update_paused`, or while the database is disabled) |
| POST | `/geo/databases` | Upload a file (`multipart/form-data`: `file`, optional `name`, `priority`, `enabled`): an mmdb, an IP2Location BIN, or a GeoNames table (zipped or `.txt`, stored with `vendor: geonames`, `kind: places`, `format: geonames`). Validated before it is stored; an enabled upload queues a re-attribution |
| POST | `/geo/databases/from-url` | Register a vendor download URL (`name`, `update_url` which may carry `{YYYY}` and `{MM}` placeholders filled per run, `update_interval_hours`, `update_auth`, `priority`) and download it now, on the api. `update_auth` takes `{username, password}` for basic auth or `{token}` / `{header, token}`; the list never returns it, only `has_update_auth` |
| POST | `/geo/databases/inspect` | Open an uploaded file and report its metadata without storing it |
| PATCH | `/geo/databases/{id}` | Change `name`, `enabled`, `priority`, `update_url`, `update_interval_hours`, `update_auth`, `update_paused` (true stops the schedule and keeps the file loaded and the URL, credentials and interval; a refresh still downloads) |
| POST | `/geo/databases/{id}/refresh` | Download a scheduled database now, on the api; a changed file of an enabled database queues a re-attribution |
| DELETE | `/geo/databases/{id}` | Remove a stored database everywhere; an enabled one queues a re-attribution |
| POST | `/geo/lookup` | `{ip, claimed_country?, claimed_state?, claimed_city?, project_id?}`: each database's answer and how the IP resolves at every level, judging the optional claim as a vendor's would be under the default conflict rule and city radius, or the project's. `resolution.city_distance_km` is how far the observed city is from the claimed one when both could be placed |
| POST | `/geo/reattribute` | `{connector_id?}`: queue a re-run of attribution, offline, for every proxy with a known exit IP (or the connector's). Answers `202` with `{"kind": "reattribute", "key": "all"}` (or `"connector:<id>"`) and a worker runs it. Each press is a job of its own; presses a worker reads together run once, `all` covering the connectors among them. `404` for an unknown connector, `503` when Redis did not take the job |
| GET | `/geo/observations` | One page of observations, newest first, with `total` for paging. Filters apply on the server and mirror the table's columns: `connector_id`, `proxy_id`, `project_id`, `source`, `ip` (exact), `claimed_country`, `resolved_country`, `verdict` (`contradicted`, `open`, `confirmed`, `no_claim`: the country verdict; the state and city verdicts come back per row and do not move the label), `until` (ISO time: start the log at that moment; without an offset it is UTC); paging with `limit` (max 1000) and `offset`. `total` is counted no further than 10,000 rows past the page: `total_exact` false makes it a lower bound. Each row also carries the place below the country: `claimed_state`, `claimed_city`, `resolved_state`, `resolved_city`, and one verdict per level, `country_conflict`, `state_conflict`, `city_conflict` (true contradicted, false confirmed, null no verdict) |
| GET | `/geo/accuracy` | Per connector, over the distinct exit IPs last seen in the window: `exits`, then `country`, `state` and `city`, each `{claimed, confirmed, contradicted, open, accuracy}`. `open` is a claim without a verdict: for the country one that independent sources disagreed about, counted against `accuracy` (confirmed over claimed); for the state and city one no city-level database could judge, counted in neither (`accuracy` is confirmed over confirmed plus contradicted). `breakdown` lists the contradicted pairs as `{level, claimed, observed, exits}` (`project_id`, `connector_id`, `days`) |
| GET | `/geo/exits` | Per connector: distinct exit IPs first seen in the window and ever, sightings, reuse (`project_id`, `connector_id`, `days`). Dynamic-sessions connectors carry `coverage` (`preflight_on`, `sampled_percent`): below 100 the figures for session-less traffic are undercounts |
| GET | `/geo/exits/sharing` | How one project's connectors share exit IPs seen in the window (`project_id`, required, and `days`). Per connector: `exits` (distinct exits last seen in the window), `reused` (of those, handed out more than once, ever), `shared` (of those, also handed out by another connector of the project in the window), `provider` (the credential type) and `coverage` as for `/geo/exits`. `pairs` lists every two connectors with exits in common as `{connector_id, other_id, shared}`, and the top-level `exits` and `shared` count distinct exits across the project and those behind more than one connector. `404` for an unknown project; past 30 s it answers 504 |
| GET | `/geo/observations/count` | `{"total"}`: every observation under the same filters, counted outright. Quick under `project_id`, `connector_id`, `proxy_id` or `ip`; under the other filters alone it reads the table, and past 30 s it answers 504 |
| GET | `/geo/exits/ips` | One page of distinct exit IPs, most recently seen first, each with first and last sighting, hand-out count and the latest observation's state (proxy, source, vendor claim, resolved country and source, verdict flags, and the same place fields as an observation). Filters: `project_id`, `connector_id`, `ip`, `proxy_id`, `country`, `claimed_country`, `verdict` (the country verdict, as for observations); paging with `limit` (max 1000) and `offset`; `total` and `total_exact` in the response, counted as for observations |
| GET | `/geo/exits/ips/count` | `{"total"}`: every exit IP under the same filters, counted outright, as for observations |
| GET | `/geo/status` | Pipeline health: the databases the answering api process has open and its own observation buffer (Detect records into it), queued (`queued_observations`: published to Redis, not written yet) and stored observations (`stored_observations`: the database's row estimate, within a few percent), preflight counters summed over the live gateways, and `gateways`: per gateway, from its heartbeat, `databases_loaded`, `databases_missing`, `load_errors`, `preflight_checks`, `preflight_rejections`, and its pending and dropped observations and sightings |

Projects carry `location_contradicted` (`evidence`, `vendor`, `refuse`: what to do when the independent sources agree against the vendor's claim), `location_unknown` (`vendor`, `refuse`: what to do when nothing independent places the exit), `location_preflight` (boolean, whether a session's exit is echoed before its first request), `location_session_move` (boolean, whether a client session preflight refused gets a new exit it keeps instead of a 502), and the optional overrides `location_conflict_rule` (`consensus`, `first`, `any`) and `location_city_radius_km` (0 to 500 km; 0 compares city names only) on create, update and in responses. On update an empty string clears an override so the project inherits the install default again. There is no source order: the routing value is always the databases, then the echo endpoint, then the vendor.

### Echo

```bash
GET /echo?nonce=<anything>
```

Public. Returns the caller's IP and, with a database loaded, its attribution:

```json
{"ip": "203.0.113.7", "country": "GB", "region": "England", "city": "London", "asn": 12345, "organization": "Example Ltd", "databases": ["<database id>"], "nonce": "…", "timestamp": "2026-09-21T12:00:00Z"}
```

Requested through a proxy it reports the proxy's exit. Served by the api (and a single instance), and by a process of role `echo` on its own. Behind a load balancer, list the balancer in `geo.echo.trusted_proxies` so the address comes from `X-Forwarded-For`.

---

## WireGuard

Devices that cannot use a proxy join a project through a WireGuard tunnel
(see [WireGuard Devices](wireguard)). Server settings are install-wide and
need an admin to change; devices (peers) are project entities editors manage.

### Server settings

```http
GET /api/v1/wireguard/settings
```

Returns the public key, endpoint, subnet, gateway and the tunnel's status
across the install: every gateway terminating it, from their heartbeats, and
the devices:

```json
{
  "public_key": "HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw=",
  "endpoint_host": "vpn.example.com",
  "endpoint_port": 51820,
  "subnet": "10.66.0.0/16",
  "gateway": "10.66.0.1",
  "persistent_keepalive": 25,
  "client_mtu": null,
  "configured": true,
  "updated_at": "2026-10-03T09:00:00",
  "status": {
    "gateways": [
      {
        "protocol": "wireguard",
        "instance_id": "gateway-1",
        "state": "running",
        "error": null,
        "backend": "kernel",
        "listen_port": 51820,
        "active_connections": 3,
        "restarts": 0,
        "denied": 0,
        "daemon_version": null,
        "interface": "wg0",
        "fake_ip_range": "198.18.0.0/15",
        "block_encrypted_dns": true
      }
    ],
    "peers_total": 4,
    "peers_enabled": 4,
    "peers_online": 2,
    "connections_by_address": 0,
    "encrypted_dns_blocked": 1
  }
}
```

`status.gateways` has one entry per live gateway with the tunnel enabled
(`state` is `starting`, `running`, `failed` or `stopped`; `error` says why it
is not up); it is empty when no gateway terminates the tunnel.

```http
PUT /api/v1/wireguard/settings
Content-Type: application/json

{
  "endpoint_host": "vpn.example.com",
  "endpoint_port": 51820,
  "subnet": "10.66.0.0/16",
  "persistent_keepalive": 25,
  "client_mtu": null
}
```

Admin only. The subnet must still contain every device's address. Gateways
apply a change live, re-addressing their interface for a new subnet; devices
learn the new DNS address with their next config.

```http
POST /api/v1/wireguard/settings/rotate-key
```

Admin only. Issues a new server key pair; every device config becomes invalid.

### Devices (peers)

```http
GET /api/v1/projects/{project_id}/wireguard/peers
```

```json
{
  "total": 1,
  "server_configured": true,
  "server_public_key": "HIgo...",
  "peers": [
    {
      "id": "…",
      "project_id": "…",
      "name": "Living room TV",
      "public_key": "…",
      "has_private_key": true,
      "has_preshared_key": true,
      "address": "10.66.0.2",
      "enabled": true,
      "session_id": "sofa",
      "country": "US",
      "state": "NY",
      "city": "new_york",
      "created_at": "…",
      "updated_at": "…",
      "status": {
        "online": true,
        "last_handshake_at": "2026-10-03T09:04:12",
        "endpoint": "203.0.113.9:40123",
        "live": true,
        "rx_bytes": 1048576,
        "tx_bytes": 23068672
      },
      "metrics": {
        "request_count": 318,
        "success_count": 316,
        "failure_count": 2,
        "avg_latency_ms": 212.4,
        "bytes_sent": 904211,
        "bytes_received": 21874322,
        "connections_by_address": 0,
        "encrypted_dns_blocked": 1
      }
    }
  ]
}
```

`status` is where the device stands. With `live` true it is what a gateway
carrying the tunnel published in the last half minute (every ten seconds
while it runs), including the interface's own byte counters since that
gateway's interface came up; with `live` false it is the last sighting on
record, which a worker's `tunnel_last_seen` job writes to the device's row
whenever a newer handshake appears, and the byte counters are zero. `metrics` is what
the pool relayed for the device: the same counters a connector has, install-wide
and across restarts, plus the two name-resolution signals (see
[WireGuard Devices](wireguard#encrypted-dns-on-devices)).

```http
POST /api/v1/projects/{project_id}/wireguard/peers
Content-Type: application/json

{
  "name": "Living room TV",
  "public_key": null,
  "preshared": true,
  "enabled": true,
  "session_id": "sofa",
  "country": "us",
  "state": "ny",
  "city": "New York"
}
```

Editor or admin. With `public_key` null Octoprox generates the key pair and
keeps the private key so the config can be shown again; with a device's own
public key only the public half is stored. The device gets the next free
address of the subnet. A state or city needs a country. Names are unique per
project, ignoring case.

```http
GET    /api/v1/projects/{project_id}/wireguard/peers/{peer_id}
PATCH  /api/v1/projects/{project_id}/wireguard/peers/{peer_id}
DELETE /api/v1/projects/{project_id}/wireguard/peers/{peer_id}
POST   /api/v1/projects/{project_id}/wireguard/peers/{peer_id}/rotate-keys
```

`PATCH` takes any of `name`, `enabled`, `session_id`, `country`, `state`,
`city`; an empty string clears the last four. `rotate-keys` issues a new key
pair (and preshared key, if the device had one).

```http
GET /api/v1/projects/{project_id}/wireguard/peers/{peer_id}/metrics/history?range=24h
```

The device's own history, in the shape of `metrics` above plus a
`timestamp` per snapshot, wrapped as `{"snapshots": [...]}`. Same `range`
values and bucketing as the project and connector history endpoints
(`1h`, `6h`, `24h` raw minutes; `7d` hourly; `30d` 6-hourly).

```http
GET /api/v1/projects/{project_id}/wireguard/peers/{peer_id}/config
```

```json
{
  "filename": "Living-room-TV.conf",
  "config": "[Interface]\nPrivateKey = …\nAddress = 10.66.0.2/32\nDNS = 10.66.0.1\n\n[Peer]\nPublicKey = …\nPresharedKey = …\nAllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = vpn.example.com:51820\nPersistentKeepalive = 25\n",
  "complete": true,
  "server_configured": true
}
```

`complete` is false when the device holds its own private key (the file
carries a placeholder) or the endpoint host is not set yet. The config
carries the device's private key, so downloading it needs an editor
(`403` for viewers).

## OpenVPN

The second tunnel protocol (see [OpenVPN Devices](openvpn)). The same
shape as the WireGuard endpoints with the credential swapped: the install
has a CA instead of a key pair, a device a certificate instead of keys.

### Server settings

```http
GET /api/v1/openvpn/settings
```

Returns the CA fingerprint and expiry, endpoint, transport, subnet, gateway
and the tunnel's status across the install, in the shape WireGuard's has:
one entry in `status.gateways` per gateway running the daemon, with its
`daemon_version`, `restarts`, `denied` and `error`. Private material (the CA
key, the server key, the tls-crypt key) is never returned.

```json
{
  "ca_fingerprint": "A1:B2:…",
  "ca_expires_at": "2036-10-05T09:00:00",
  "endpoint_host": "vpn.example.com",
  "endpoint_port": 1194,
  "protocol": "udp",
  "subnet": "10.67.0.0/16",
  "gateway": "10.67.0.1",
  "keepalive_interval": 10,
  "keepalive_timeout": 60,
  "client_mtu": null,
  "configured": true,
  "updated_at": "2026-10-05T09:00:00",
  "status": {
    "gateways": [
      {
        "protocol": "openvpn",
        "instance_id": "gateway-1",
        "state": "running",
        "error": null,
        "backend": null,
        "listen_port": 1194,
        "active_connections": 3,
        "restarts": 0,
        "denied": 1,
        "daemon_version": "OpenVPN 2.6.3 x86_64-pc-linux-gnu",
        "interface": "ovpn0",
        "fake_ip_range": "198.18.0.0/15",
        "block_encrypted_dns": true
      }
    ],
    "peers_total": 2,
    "peers_enabled": 2,
    "peers_online": 1,
    "connections_by_address": 0,
    "encrypted_dns_blocked": 0
  }
}
```

```http
PUT /api/v1/openvpn/settings
Content-Type: application/json

{
  "endpoint_host": "vpn.example.com",
  "endpoint_port": 1194,
  "protocol": "udp",
  "subnet": "10.67.0.0/16",
  "keepalive_interval": 10,
  "keepalive_timeout": 60,
  "client_mtu": null
}
```

Admin only. `protocol` is `udp` or `tcp`; `keepalive_timeout` must exceed
`keepalive_interval`; the subnet must still contain every device's address.
A change to the port, transport, subnet or keepalive restarts the daemon on
every gateway running it.

```http
POST /api/v1/openvpn/settings/rotate-identity
```

Admin only. Issues a new CA, server certificate and tls-crypt key, and
reissues every device's certificate under the new CA in one transaction;
every device profile becomes invalid.

### Devices (peers)

```http
GET /api/v1/projects/{project_id}/openvpn/peers
```

```json
{
  "total": 1,
  "server_configured": true,
  "ca_fingerprint": "A1:B2:…",
  "peers": [
    {
      "id": "…",
      "project_id": "…",
      "name": "Living room router",
      "serial": "3891…",
      "certificate_expires_at": "2036-10-05T09:00:00",
      "address": "10.67.0.2",
      "enabled": true,
      "session_id": null,
      "country": "US",
      "state": null,
      "city": null,
      "created_at": "…",
      "updated_at": "…",
      "status": {
        "online": true,
        "connected_since": "2026-10-05T09:04:12",
        "last_seen_at": "2026-10-05T09:04:12",
        "endpoint": "203.0.113.9:40123",
        "live": true,
        "rx_bytes": 1048576,
        "tx_bytes": 23068672
      },
      "metrics": { "request_count": 318, "…": "as for WireGuard devices" }
    }
  ]
}
```

`status.online` means a gateway running the daemon currently carries a
session for the device; `last_seen_at` is that session's start, or the
persisted start of the last one. `metrics` is the same `TunnelPeerMetrics`
shape WireGuard devices carry.

```http
POST /api/v1/projects/{project_id}/openvpn/peers
Content-Type: application/json

{ "name": "Living room router", "session_id": null, "country": "US", "state": null, "city": null, "enabled": true }
```

Editor. Issues a certificate from the install's CA (the device id as common
name) and takes the next free address. There is no bring-your-own key.

```http
GET    /api/v1/projects/{project_id}/openvpn/peers/{peer_id}
PATCH  /api/v1/projects/{project_id}/openvpn/peers/{peer_id}
DELETE /api/v1/projects/{project_id}/openvpn/peers/{peer_id}
POST   /api/v1/projects/{project_id}/openvpn/peers/{peer_id}/rotate-certificate
GET    /api/v1/projects/{project_id}/openvpn/peers/{peer_id}/metrics/history?range=24h
GET    /api/v1/projects/{project_id}/openvpn/peers/{peer_id}/config
```

`PATCH` takes the same partial document as a WireGuard device (`name`,
`enabled`, `session_id`, `country`, `state`, `city`; an empty string
clears). Disabling, rotating or deleting a device ends any session it has,
on every gateway, once the routing feed brings the change (about two
seconds). `rotate-certificate` issues a new certificate and key; the serial
changes and the old profile stops working. `config` returns the `.ovpn`
profile, which carries the device's key, so downloading it needs an editor
(`403` for viewers):

```json
{
  "filename": "Living-room-router.ovpn",
  "config": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n…<ca>…</ca>\n<cert>…</cert>\n<key>…</key>\n<tls-crypt>…</tls-crypt>\n",
  "complete": true,
  "server_configured": true
}
```

## Release Notes

What's New in the web UI: the release notes of the running version and those before it, and how many the signed-in user has not opened yet. See [Changelog]({{ site.baseurl }}/changelog).

```bash
GET /api/v1/releases
POST /api/v1/releases/seen
```

Any signed-in user. `POST /releases/seen` records that the user has seen the notes up to the running version; both return the same body:

```json
{
  "current_version": "6.1.4",
  "last_seen_version": "6.1.3",
  "unseen": 1,
  "notes": [
    {
      "version": "6.1.4",
      "from": "6.1.0",
      "date": "2026-10-27",
      "title": "IP sharing and exit headers",
      "changes": [{"text": "...", "docs": "connecting#exit-headers"}]
    }
  ],
  "update": null
}
```

- `notes`: newest first. `from` is set on the summaries that cover several releases (those up to 6.1.4); `docs` is a page on octoprox.com.
- `last_seen_version`: null until the user first opens What's New, and `unseen` is then 1, for the newest notes.
- `update`: for admins only, while `OCTOPROX_UPDATE_CHECK` is on and the last daily check found a newer release: its `version`, `checked_at` and the `notes` of every release newer than the running one. Null otherwise.

## Health Check

Every process, whatever its role, answers four paths on port 8000 for probes,
load balancers and Prometheus. None needs a sign-in, and none touches Postgres
or Redis: they read the process's own state.

```bash
GET /live      # 200 while the process serves
GET /ready     # 200 while it can do its work, else 503
GET /health    # the same check, with who answered
GET /metrics   # Prometheus text of this process
```

**`/ready`**:
```json
{ "status": "ready", "role": "gateway", "reasons": [] }
```
or, with `503`:
```json
{ "status": "unready", "role": "gateway", "reasons": ["gateway: feed not applied yet"] }
```

**`/health`** runs the same check and names the process; `reasons` appears only
with `503`:
```json
{ "status": "healthy", "role": "api", "instance": "api-1", "version": "6.0.0" }
```

**`/live`** answers `{"status": "alive"}`. What ready means for each role is
in [Deployment & Scaling]({{ site.baseurl }}/deployment#health-endpoints), and
the metric families of `/metrics` in [Metrics]({{ site.baseurl }}/metrics#prometheus).

## Internal Endpoints

`/internal/v1` is for the cluster's own processes and is not part of the
public API. A gateway fetches the IP database files the routing feed lists
from the api:

```bash
GET /internal/v1/geo/databases/{database_id}/blob?sha256=<checksum>
Authorization: Bearer <internal token>
```

The token is derived from `OCTOPROX_INTERNAL_SECRET`; a sign-in token is
refused with `401`. The answer is the file, or `404` when the api has no file
of that database at that checksum. The bundled nginx config refuses
`/internal/` on its public port.
