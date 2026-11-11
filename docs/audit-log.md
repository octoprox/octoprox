---
layout: docs
title: "Audit Log"
nav_id: audit-log
description: "Every change made in Octoprox is recorded with who made it, when and the fields it changed, secrets masked: History in each inspector, retention and backups, and with Octoprox Enterprise every sign-in and token, install-wide, exportable to a SIEM."
---

# Audit Log

<p class="subtitle">Every change a person makes in Octoprox is recorded with who made it, when, and the fields it changed. Secrets are never stored, only whether they were set, changed or cleared. Octoprox Enterprise adds every sign-in and API token, and one page to read and export it all.</p>

An entry says who acted, when, in which request, what they changed, and the value of each changed field before and after. It is written in the same database transaction as the change, so a change is never saved without its entry, and a change that fails leaves none.

Every edition records changes. [Octoprox Enterprise](#octoprox-enterprise) also records sign-ins and token events with the address they came from, lists the whole install's log on one page, and exports it.

## What is recorded

Every change a person makes, in the web UI or through the API:

| What | Recorded when a person |
|---|---|
| Project | creates, edits or deletes it |
| Connector | creates, edits or deletes it, changes its countries on the Countries page (one entry per connector changed, with the country), or resets its traffic usage |
| Credential | creates, edits or deletes it |
| Proxy | adds it by hand or in an upload (one entry per proxy), edits or removes it |
| WireGuard or OpenVPN device | adds, edits or removes it, or rotates its keys (WireGuard) or certificate (OpenVPN) |
| WireGuard or OpenVPN settings | saves them, or rotates the server key (WireGuard) or the CA (OpenVPN, with the number of device certificates reissued) |
| Custom provider | creates, imports, edits (enabling and disabling too) or deletes it, with the vendor hosts each change added or removed |
| User | adds, invites, edits or deletes one, or sends a new invite link; changes their own email or password under Account; sets their password through an invite link |
| Sign-in provider | saves its settings under Administration → Sign-in |
| IP attribution | saves the settings; uploads, adds from a URL, edits (pausing and resuming too) or removes a database |
| System settings | changes the audit log's retention, or on Octoprox Enterprise the license key |
| Backup | restores one |

Each entry's action is one of **Created**, **Updated**, **Deleted**, **Imported** (a provider imported, a backup restored), **Traffic reset**, **Invited**, **Invite reissued** and **Rotated**; Octoprox Enterprise adds the [sign-in and token events](#sign-ins).

A delete is recorded when the person asks for it, even when instances are terminated afterwards, as for a cloud connector or proxy, or a project that holds one. Asking again records nothing new.

## What an entry says

- **Who**: the user, by ID and by their username at the time. A change made with a [personal access token]({{ site.baseurl }}/sso#personal-access-tokens) also names the token, by its name (`auth_token`) and its ID (`auth_token_id`); the History shows such an entry as *Updated by ivan with token deploy*.
- **When**: the time, in UTC.
- **The request**: its `X-Request-ID`, which every server log line written while handling it carries (see [Request IDs]({{ site.baseurl }}/configuration#request-ids)).
- **What**: the entity's type, ID and name at the time, and its project. The entries of something deleted stay readable.
- **The changes**: each changed field with its value before and after.
- **Details**: what else the action carries: proxies added by an upload (`via: upload`), a password set through an invite link (`via: invite`), the hosts a provider change added or removed, the country set from the Countries page, how many devices an OpenVPN CA rotation reissued, and for a restore the backup's date and release.

### How changes are written

- **Created**: every recorded field that has a value, as `[null, value]`.
- **Deleted**: every recorded field that had a value, as `[value, null]`, so the last state outlives the entity.
- **Updated**: only the fields whose value changed. A save that changes nothing adds no entry.
- **Settings kept as a set of keys** are compared key by key: a connector's provider settings and its routing, rate limit, traffic and health check settings, a credential's provider settings, and a provider's descriptor. The History names such a key under its group, as Weight under Routing for `routing_config.weight`. An absent key is at its default and shows as not set.
- **Only what people set.** What Octoprox writes on an entity itself is left out: a proxy's health, status and exit, a connector's last error and proxy count, a device's last sighting, a user's last sign-in. A proxy entry records the address, protocol, credentials and tags a person sets and a location pinned by hand. A user's theme and the release notes they have seen are set by the person and stored on their user, but left out too.

### Secrets

Secret values are never stored. These are recorded only as `********` while set and `null` while empty: the passwords of users, projects and proxies, the secret fields of a credential or connector, a tunnel device's private key (and a WireGuard device's preshared key), the tunnel servers' keys (WireGuard's private key; OpenVPN's CA, server and tls-crypt keys), a sign-in provider's client secret, an invite link, an IP database's download URL and download credentials, and the license key. A change to a secret is still recorded, so the History says whether it was set, changed or cleared:

| Change | Recorded as |
|---|---|
| Set | `[null, "********"]` |
| Changed | `["********", "********"]` |
| Cleared | `["********", null]` |

Which fields of a credential or connector are secret is what its provider says at the time of the change; for a provider no longer installed, every provider setting is masked. Values too large to keep in every entry, a provider's logos, are written the same way. A device certificate is recorded as its serial and expiry, and the OpenVPN server's certificates as the CA's fingerprint and expiry.

### Example

A connector's routing weight set and its zone password changed by a script with a personal access token:

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
  "changes": {
    "routing_config.weight": [null, 3],
    "config.zone_password": ["********", "********"]
  },
  "details": {"auth_token": "deploy", "auth_token_id": "e2a7c9d4-5b1f-4c3e-8a06-7d9b2f41c835"}
}
```

## Where to read it

### History tabs

The inspector of each of these has a **History** tab, newest entry first, with **Load older** for more:

- projects, in the project settings;
- connectors, credentials and proxies;
- WireGuard and OpenVPN devices;
- users, under **Administration → Users**, where Octoprox Enterprise also lists each user's sign-ins and token events;
- custom providers, under **Administration → Providers**.

### History buttons

What has no inspector of its own has a **History** button on its card:

- **Administration → IP attribution**: the settings, beside *Save settings* on the Policy & echo tab, and each database in the Databases list.
- **Administration → WireGuard** and **Administration → OpenVPN**: the *Endpoint and addressing* card, for the server settings and their rotations.
- **Administration → Sign-in**: each provider's card.
- **Administration → Activity**: the *Retention* card.
- **Administration → System**: the *License* card, on Octoprox Enterprise. It shares its History with the Retention card: both are the install's system settings.
- **Administration → Backup & Migration**: the *Restore* card, listing every restore.

### Who can read what

Everyone signed in, viewers included, can read the history of projects and of their connectors, credentials, proxies and devices, as they can read those; in the web UI a project's own History is in its settings, which editors and admins open. The history of users, providers, sign-in providers, IP attribution, tunnel settings, system settings and restores is for admins.

### Administration → Activity

Admins set the [retention](#retention) on this page. On the free edition it also says where each History lives; on [Octoprox Enterprise](#the-activity-page) it lists every entry of the install.

## What is not recorded

- **System work**: health checks, provider syncs, cloud provisioning and autoscaling (including the termination that completes a delete a person asked for, and cloud errors), IP attribution (exit lookups, re-attribution, and the location and slot rotation Detect causes), IP database downloads (scheduled and *Download now*), tunnel devices' last sightings, settings rows created at start or on a first read, the first admin, migrations, and the deletes of compaction and retention.
- **Actions that store nothing**: lifting a quarantine, clearing MITM captures or the failure log, a re-attribution request, Detect, a backup export, device config downloads, a provider's option lookups, validation and tests, inspecting an IP database file and looking up an IP. A provider sign-in's refresh of the linked account's email and last sign-in is not recorded either.
- **What a person sets for themselves alone**: their theme and the What's New releases they have seen, which are stored on their user.
- **Rows deleted with their parent**: a project's credentials and devices, a connector's proxies, a user's tokens and linked sign-ins. The parent's entry covers them.
- **On the free edition**: sign-ins, token events, linked sign-ins removed, and client addresses. The audit log of a free install never records sign-ins or client addresses. The server's access log lines still name the address each request came from, as they always have.

An IP database added from a URL is recorded when it is added, before its first download, so its Created entry shows its vendor, kind and format as `other`, `unknown` and `mmdb`; the download that fills them in is system work.

## Retention

Entries are kept for 365 days until an admin changes it on the *Retention* card under **Administration → Activity**: any whole number of days up to 36,500, or 0 to keep them forever. The hourly compaction deletes older entries. The retention is stored in the database, not in a configuration file, so every process follows it and a backup carries it, and a change to it is itself recorded.

A custom provider's history from releases before the audit log was carried over on upgrade, each saved version becoming an entry with the fields it changed. That history was kept forever; it now follows the retention like every other entry.

## Backups

A [backup]({{ site.baseurl }}/api#backup--migration) carries the audit log and the install's system settings, its retention and license key. A restore replaces everything else, but keeps the install's audit history and license key and adds the backup's entries; an entry the install already has stays as it is. The backup's settings are taken only when the install has never saved its own, and a missing license key is filled in from the backup's.

The restore itself is recorded last, as an **Imported** entry naming the admin, with the date the backup was made, the release that made it, whether it carried history, and whether the admin's account was kept. The *Restore* card's History lists every restore.

## API

- `GET /api/v1/audit/{type}/{id}`: one entity's history, newest first, answering `{entries, more}`; `limit` entries at a time (50 by default), and `before`, the ID of the oldest entry already shown, for the next page.
- `GET` and `PUT /api/v1/audit/retention`: the retention in days, for admins.
- `GET /api/v1/edition`: which edition the server runs, public.

The [API reference]({{ site.baseurl }}/api#audit-log) has the details, and the Octoprox Enterprise endpoints.

## Octoprox Enterprise

[Octoprox Enterprise]({{ site.baseurl }}/enterprise) runs from its own image, built on the same release, and is turned on by a license key on the *License* card under **Administration → System** (see [Getting a license]({{ site.baseurl }}/sso#getting-a-license)). While the license is active, or in the 30 days of grace after it expires, the audit log also records sign-ins and API tokens with where they came from, and writes a log line for every entry. Admins read the whole install's log on one page and export it for a SIEM. Changes are recorded as on the free edition, licensed or not.

### Sign-ins

Every sign-in, successful or refused, is an entry on the user who signed in or tried to: **Signed in** or **Sign-in failed**. Its details:

| Detail | Meaning |
|---|---|
| `method` | `password`, `invite` (an invite link used to set a password) or `sso` (a sign-in provider) |
| `provider` | For `sso`: `google`, `microsoft` or `github` |
| `linked` | `true` when this sign-in linked the provider account to the user, the first time it was used |
| `client_ip` | The address the sign-in came from (see [Client addresses](#client-addresses)) |
| `user_agent` | The browser, as it names itself, cut to 512 characters |
| `reason` | Why a sign-in was refused |

The reasons, by method:

- `password`: `unknown user`, `no password set`, `wrong password`, `account disabled`.
- `invite`: `unknown link`, `expired link`, `account disabled`.
- `sso`: `cancelled` (the person pressed cancel at the provider), `refused by the provider: <error>` with the error code the callback carried (`refused by the provider` alone when that is not a plain code of up to 64 letters, digits, `.`, `_` and `-`), `provider not enabled`, `unexpected error`, or for any other refusal the reason the server log gives, such as an email that matches no user.

A refusal of a known user is on that user's History. A password sign-in under a name no user has is recorded with the name as typed, cut to 255 characters; it is on no user, so it shows on the Activity page only, as does a provider sign-in that matched nobody. The invite link a person typed is never recorded. A refusal from a provider's callback is recorded only when the callback carries a sign-in state this install issued for that provider and has not used yet, which stays valid for 10 minutes. Anyone can get such a state by starting a sign-in, so the refusal, and the error code in its reason, is what the callback said, not something Octoprox checked with the provider.

What the person sees does not change: the same `Invalid username or password`, the same message on the login page. A sign-in is recorded in a transaction of its own and never held up or refused by it: when recording fails, the server logs `Audit sign-in record failed` and the sign-in goes on.

### Tokens and linked sign-ins

[Personal access tokens]({{ site.baseurl }}/sso#personal-access-tokens) are recorded on their owner, with the token's name, prefix, ID and expiry (`token_name`, `token_prefix`, `token_id`, `expires_at`), never its secret:

- **Token issued**, by the owner, or by an admin for a service account.
- **Token first used**: the token's first authenticated call, with its address and browser. The call may still be refused, as a viewer's token trying a write is; its first use is recorded all the same, once however many calls arrive together. When recording fails, the server logs `Audit token record failed` and the call goes on.
- **Token revoked**.

**Sign-in unlinked** records a provider account unlinked from a user, with the provider and the account's email. All of these show in the user's History (for admins) and on the Activity page. A revocation or an unlink made with a personal access token, as by a script, also names the token it was made with, as `auth_token` and `auth_token_id`, apart from the token revoked.

### The Activity page

**Administration → Activity** lists every entry of the install, newest first, with the exact count: changes, sign-ins and token events alike, those recorded before the license included. Filter by entity type, action, the user who acted, project, and a time to read back from (*Until*). An entry opens beside the list with its changes, its details, and its entity and request IDs to copy, and for an entry made with a personal access token, that token's ID. Admins only.

### Export

**Export JSON lines** on the Activity page saves every entry under the current filters, oldest first, as one JSON object per line (`application/x-ndjson`), for a SIEM or an auditor. A line has the shape of an API entry, with its time in UTC ending in `Z`:

```json
{"id": "bfc934ad-9e9e-4984-a8c3-2454ebf69534", "created_at": "2026-11-10T09:12:05.481203Z", "action": "signed_in", "entity_type": "user", "entity_id": "9d59c651-da69-42f4-8222-7b5a766d0188", "entity_name": "ivan", "project_id": null, "actor_id": "9d59c651-da69-42f4-8222-7b5a766d0188", "actor": "ivan", "request_id": "d32afe4cd6a8432194923ee484187238", "changes": {}, "details": {"method": "sso", "provider": "google", "client_ip": "203.0.113.7", "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15"}}
```

The file is read from the log as it downloads, so entries recorded meanwhile can join its end; set *Until* to a time already past for a set that cannot grow. The same export is `GET /api/v1/audit/export` with the page's filters (see the [API reference]({{ site.baseurl }}/api#octoprox-enterprise-endpoints)).

### Log lines

While the license is active or in its grace period, each entry is also one `audit` line in the server log, written once its change has committed: a change that rolls back writes none. The lines come at info level from the logger `octoprox_enterprise.audit`, and are written whatever `OCTOPROX_LOG_LEVEL` is set to. With `OCTOPROX_LOG_FORMAT=json` the line carries the entry's fields beside the usual `timestamp`, `level`, `logger`, `instance` and `role`, so a log pipeline can collect the audit log with the rest of the logs.

### Client addresses

`client_ip` is the address the connection to Octoprox came from. Behind a load balancer or reverse proxy that is the balancer's own address, so list the balancer's addresses in `geo.echo.trusted_proxies` (`OCTOPROX_GEO_ECHO_TRUSTED_PROXIES`, a JSON list of CIDRs, on the api), the setting the [echo endpoint]({{ site.baseurl }}/ip-attribution#echo-endpoint) reads too. For a request from a listed address, the client is the rightmost `X-Forwarded-For` entry outside the listed networks: the address that connected to the balancer. Several `X-Forwarded-For` headers are read as one list, in order, so a balancer that adds a header of its own rather than appending to the client's, as HAProxy does, is read right. Anything to the left of the client's entry was written by the client and is never believed. With the list empty, the default, every sign-in records the balancer's address.

- **uvicorn's `FORWARDED_ALLOW_IPS`.** uvicorn believes `X-Forwarded-For` from the hosts this variable lists, and for their requests replaces the peer address with one from that header before Octoprox sees it. Keep its default, the loopback addresses, only when nothing forwards to the api over loopback. When a proxy or sidecar on the same host or in the same pod forwards to the api over `127.0.0.1`, set `FORWARDED_ALLOW_IPS` to an empty value, so that `geo.echo.trusted_proxies` alone decides, and list the real balancers there. List `127.0.0.1/32` with them only when that local proxy adds the client's address to the header itself: a sidecar that passes the header on unchanged passes on whatever the client wrote. Set `OCTOPROX_PUBLIC_URL` as well, since the api then takes no scheme from that proxy's `X-Forwarded-Proto`.
- **The bundled nginx** appends the client's address to `X-Forwarded-For` on the API listener: list its address range.
- **On Kubernetes**, an Ingress controller adds the header, so list its pod addresses; the controller must itself see the client's address, which for most controllers means `externalTrafficPolicy: Local` on their own Service. An L4 LoadBalancer Service in front of the api adds no header, and the client's address reaches the api only with `externalTrafficPolicy: Local`: with the default, `Cluster`, the api sees a node's address. See [Running on Kubernetes]({{ site.baseurl }}/kubernetes#services).

### License

Recording sign-ins, token events and log lines needs a license that includes the audit log and is active or in its 30-day grace period. After the grace period they are no longer recorded until a new key is entered, while the Activity page and the export stay readable with the expired license, so an install can still answer for what it recorded. A key whose grace period has already ended is refused when entered, with the dates, since it would record nothing; one stored before it lapsed, or brought back by a restore, keeps the log readable. Seats and the banner admins see are under [Getting a license]({{ site.baseurl }}/sso#getting-a-license), and how to get one under [Octoprox Enterprise]({{ site.baseurl }}/enterprise#get-in-touch).
