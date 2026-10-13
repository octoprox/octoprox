---
layout: docs
title: "Location Routing: Settings and Scenarios"
nav_id: location-routing
description: "How the source order, conflict rule, location policy, preflight mode and routing strategy combine for pooled sessions, dynamic sessions and fixed exits, with worked examples."
---

# Location Routing

<p class="subtitle">What decides where a request goes when a country, state or city is asked for, what happens when the exit is not there, and how that differs between pooled sessions, dynamic sessions and fixed exits.</p>

Four project settings, the routing strategy and one connector setting decide a request's fate once it names a place. Each is simple on its own; the combinations are not. This page walks through them with examples. The mechanics of attribution (databases, the echo endpoint, what is stored where) are on the [IP attribution]({{ site.baseurl }}/ip-attribution) page and the username syntax on [Routing strategies]({{ site.baseurl }}/routing-strategies#country-routing).

## The settings at a glance

| Setting | Where | What it decides |
|---------|-------|-----------------|
| **Which source decides** (`location_sources`) | Project, Location tab; default under Settings, IP attribution | Which answer becomes a proxy's country, state and city for routing: the local databases, the vendor's claim or the echo endpoint, in order. The first with an answer wins. |
| **Vendor contradicted when** (`location_conflict_rule`) | Same | When the vendor's claim counts as contradicted: `consensus` (every independent source agrees against it) or `first` (the top-ranked independent source disagrees). |
| **Contradicted vendor locations** (`location_policy`) | Project, Location tab | What the verdict on a claim does to the proxy's *eligibility*: `warn` keeps routing to every proxy, `strict` routes only where the claim is confirmed and replaces a contradicted pooled session. |
| **Preflight check** (`location_preflight`) | Project, Location tab | Whether a session's exit is verified with an echo request before its first request is forwarded, and what a mismatch does to the request: `report`, `retry` or `reject`. |
| **Routing strategy** | Project, Routing tab | Whether a request may move to another proxy after a mismatch. `sticky` promised a session one exit, so it will not move it. |
| **Exit sampling** (`exit_sample_percent`) | Connector, dynamic sessions only | The share of session-less requests preflight echoes when nothing has to be verified. |

Two ideas to keep apart, because the settings act on different ones:

* The **value** is the country (state, city) routing matches a request's `-cc-` (`-st-`, `-city-`) against. It comes from the source order and nothing else.
* The **verdict** is a judgement of the vendor: confirmed, contradicted or open. It comes from the conflict rule. The policy acts on the verdict. Preflight produces a verdict for one request and acts on the request.

Changing the policy never changes which country a proxy is routed on. Changing the source order changes the value and, under `first`, which source does the judging.

## Three kinds of proxy row

Everything below depends on what kind of row the request lands on, because the three kinds learn their exit differently and only some can be verified or flagged.

| | Fixed exit | Pooled session slot | Dynamic-sessions gateway |
|---|---|---|---|
| Examples | Static proxies, ISP and datacenter ports (Oxylabs, Decodo, Bright Data), proxy lists (Webshare), cloud instances | Residential and mobile connectors with **Sessions: Fixed pool**: `num_proxies` rows per country, each holding one vendor session | Residential and mobile connectors with **Sessions: Dynamic**: one row, a vendor session rendered per request |
| Where the exit comes from | Discovery when the row is created, re-read on every IP refresh and health check | The vendor, behind the session; seen only by the health check (every minute, when the check URL echoes the caller) and by preflight | The vendor, per request; seen only by preflight |
| Health-checked | Yes | Yes | No. The row is always healthy; a dead credential shows as failed requests |
| Vendor claim judged | The country (and city) the vendor's list or discovery endpoint reported, or the one an operator pinned | The country the slot was provisioned for (`-cc-` on demand, or the connector's list). Untargeted slots make no claim | The `-cc-` of the request, or the country the allow-list rendered for it |
| Can carry the contradiction flag | Yes | Yes | **Never**. Nothing is written on the row, so `strict` cannot exclude it |
| Preflighted | **Never**, whatever was asked. Its exit is already known and re-read every minute | When the request asks for a place or the slot was provisioned for one | Always, once preflight is on |

## What a proxy is routed on

Attribution resolves every sighting of an exit IP under the project's source order and writes the winner on the row as its country, state and city. Routing then matches `-cc-` exactly against that country: a row whose country is known must match, and a row with no country is eligible only through its connector's declared list. The default order is databases, vendor, echo endpoint.

The examples use two databases (MaxMind and DB-IP, in that priority) and an Oxylabs residential slot provisioned for Germany, so the vendor claims `DE`.

| Case | MaxMind | DB-IP | Under `consensus` | Under `first` |
|------|---------|-------|-------------------|---------------|
| Everything agrees | DE | DE | routed on DE, **confirmed** | routed on DE, **confirmed** |
| Databases agree against the vendor | FR | FR | routed on FR, **contradicted** | routed on FR, **contradicted** |
| Databases disagree with each other | FR | DE | routed on FR, **open** (no flag) | routed on FR, **contradicted** (MaxMind outranks DB-IP) |
| No database knows the IP | - | - | routed on DE, the vendor's word, **open** | routed on DE, the vendor's word, **open** |
| Untargeted slot, no claim | FR | FR | routed on FR, no verdict | routed on FR, no verdict |

What to take from the table:

* **The conflict rule never decides the value.** The top-ranked source with an answer does, under either rule, so the third row is routed on MaxMind's `FR` under `consensus` and `first` alike. The rule only decides the flag: `consensus` refuses to blame the vendor on one dissenting database and leaves the claim open, `first` flags on the same evidence it routes on. A project that wants no routing on unsettled evidence uses `strict`, which refuses open claims.
* **The databases override the vendor** for routing whenever one of them knows the IP. A contradicted slot serves `-cc-fr` requests and untargeted requests. It no longer serves `-cc-de`, because its country is now `FR` and matching is exact. Under `warn` that is already the case; `strict` only adds that it serves nothing at all.
* **The vendor fills the gap** only when no independent source knows the IP. Under the "Databases only" order the vendor never decides and such a row has no country, so only its connector's declared list can make it eligible.
* **"Vendor decides, databases flag"** is the mirror image: the second row is routed on `DE` although it is contradicted. That order says "route on the vendor's word, tell me when it is wrong". Combined with `strict` it routes only where the databases confirm the vendor, which is the same set as the default order gives.

A verdict changes when the evidence does: a later health check that sees the slot at a new IP re-resolves it, and **Re-attribute proxies** on the settings page re-judges every row offline after a database is added or replaced. The verdict is shown on the Proxies page: a red triangle for a contradicted claim, a question mark for an open one.

## What the policy does

The policy reads the verdict on the row. It does not look at the request, so it is not "strict for the contradicted country": a row that strict refuses is out for every request of the project, including untargeted ones.

**Warn.** Every row stays eligible and is routed on its resolved country. The Proxies page shows a warning triangle with "Vendor claimed DE; attribution says FR" and provider accuracy counts the exit as contradicted. With the German slot above, `-cc-de` traffic simply no longer reaches it, `-cc-fr` and untargeted traffic can.

**Strict.** Routing wants clear evidence. A row with a vendor claim is eligible only while the claim is **confirmed** (or not judged yet: a slot provisioned seconds ago, before its first health check, is routable so that on-demand provisioning works). Two kinds of row are refused:

* A **contradicted** row is skipped, and when it is a pooled session slot it is also removed, so the provider sync provisions a fresh session in its place. A fixed exit is only skipped: the port refresh already drops an exit that left the connector's countries, and a static row is the operator's to fix.
* An **open** row is skipped until a later sighting settles the claim. It is not replaced: rotating on missing evidence would churn a pool the databases do not cover, at the vendor's price, for sessions just as unknown as the last. The exception is a country an operator pinned by hand on a static proxy: it is judged like a vendor claim, but trusted while open, since the operator placed that row deliberately. A pin the databases agree against is still skipped.

So under `strict` with the table above, only the first row is routed to. The third and fourth rows wait; the second is replaced. Rows without a claim (untargeted slots, cloud instances, static proxies with no country set) are never judged and stay eligible. `strict` does not change what a row is routed on, only whether it is routed to at all.

**Strict needs databases.** With no IP database loaded and an echo endpoint that reports no country, every claim is open, and a strict project routes to nothing that carries a claim. Load at least one database before turning it on, and expect residential pools in countries the databases cover poorly to run short.

**Neither touches a dynamic-sessions gateway.** Its exits belong to the sessions it renders, nothing is written on the row, and so there is no verdict to act on. The only way to act on a dynamic connector's misplaced exits is preflight.

## Preflight

With preflight on, the request is routed as usual and then, before it is forwarded, one echo request goes through the selected upstream. The exit IP is resolved under the project's source order and conflict rule with the **required place** as the claim:

1. the request's own `-cc-`, `-st-` and `-city-`;
2. else the country the vendor promised for the slot (the group it was provisioned for, or what a dynamic request rendered from the allow-list) and any state or city rendered into a dynamic request;
3. else a country pinned by hand on a static row.

A place that was merely observed is not required: an untargeted residential slot is not preflighted unless the request names a place. A fixed exit is never preflighted. The verdict is cached per project and proxy (per vendor session and place for a dynamic row) for `preflight_session_ttl_seconds` (default 600), and dropped early when a health check sees the exit move.

The check has three outcomes, and only one of them ever stops a request:

| Outcome | Example | Any mode |
|---------|---------|----------|
| Match | `-cc-de`, exit resolved as DE | forwarded |
| Open | echo unreachable, IP unknown to every database, or databases disagree under `consensus` | forwarded; nothing is ever rejected on missing evidence |
| Mismatch | `-cc-de`, exit resolved as FR; or `-city-manchester`, exit in London | depends on the mode, below |

### On a mismatch

What happens depends on the mode, the kind of row, whether the client sent `-sessid-`, and the strategy. `max attempts` is `preflight_max_attempts` under Settings, IP attribution: install-wide, default 3, counting the first proxy, so 3 means two retries.

| Row | `-sessid-` | Strategy | `report` | `retry` | `reject` |
|-----|-----------|----------|----------|---------|----------|
| Pooled slot | none, or any under a non-sticky strategy | any but sticky with a session | forwarded through the misplaced exit; nothing else happens | slot excluded, another eligible proxy tried, up to max attempts, then 502. The slot is removed and the sync provisions a fresh session | 502. The slot is removed and the sync provisions a fresh session |
| Pooled slot | yes | `sticky` | forwarded | **no move**: 502 at once, the slot is removed. The next request with the same `-sessid-` finds its binding gone and binds to another proxy, which is verified afresh | same |
| Dynamic gateway | none | any | forwarded (only sampled requests are checked at all unless a place is required) | the **same gateway** is selected again, which mints a fresh vendor session; up to max attempts, then 502 | 502. The next session-less request gets a new vendor session anyway |
| Dynamic gateway | yes | any but `sticky` | forwarded | the gateway is excluded and another connector's proxy is tried with the same `-sessid-`; with no other connector, 502 | 502 |
| Dynamic gateway | yes | `sticky` | forwarded | 502 at once | 502 |
| Fixed exit | - | - | not preflighted | not preflighted | not preflighted |

Points that are easy to miss:

* **`report` never touches the proxy or its flag.** The observation is recorded for provider accuracy and unique exits, and that is all. A `report` mismatch does not make a `strict` project drop the slot; only a health check, discovery or re-attribution writes the flag, and they do so under every preflight mode alike.
* **A dynamic client session that was rejected stays rejected for the TTL.** The vendor session is derived from the `-sessid-` and the project, so the same `-sessid-` asking for the same place lands on the same vendor session, and the cached mismatch answers 502 without a new echo until the TTL passes. Asking for a different `-cc-` is verified afresh. Clients should pick a new `-sessid-`.
* **A pooled client session is not lost.** The misplaced slot goes, the `-sessid-` re-binds, the sync fills the pool. The client sees one 502 (two under `reject` if the replacement is also misplaced, since it is verified in turn).
* **Sampling never weakens a guarantee.** On a dynamic connector, session-less requests are echoed at `exit_sample_percent` under `report`, or when they require nothing. Under `retry` and `reject` a session-less request that asks for a country, or lands on a connector whose allow-list promises one, is verified every time.
* **`retry` on a single-connector project with a client session is `reject`.** There is nowhere to go.

## Scenarios

**1. Residential pool, Germany listed, preflight off, `warn`.**
Ten Oxylabs sessions are provisioned with `cc-de` in the credentials. The health check, hitting the echo endpoint every minute, reports each slot's exit; attribution resolves it. Slot 7 resolves as `FR` under both databases: its country becomes `FR`, it is flagged, the accuracy page shows one contradicted exit of ten. A `-cc-de` request is now spread over nine slots; slot 7 serves untargeted requests and would serve `-cc-fr`. The vendor's next session rotation may bring it back to `DE`, at which point the next health check clears the flag.

**2. The same pool under `strict`.**
Slot 7 is removed the moment the health check contradicts it, and the next provider sync provisions a fresh German session in its place, so the pool stays at ten. The removed exit is counted once as contradicted in provider accuracy. Had the databases disagreed about slot 7 instead, it would have stayed, skipped by every request of the project, until a health check found it at an IP the databases agree on; the pool runs on nine meanwhile. Watch the connector's provider accuracy and change vendor if the rate is poor.

**3. Dynamic Oxylabs, `reject`, client sessions.**
A client sends `-sessid-job42-cc-de`. The gateway renders a vendor session derived from `job42` and the project, with `cc-de`. One echo goes through it; the exit resolves as `DE`; the request is forwarded, and so is every later `job42` request for the next ten minutes with no further echo. A second client sends `-sessid-job43-cc-de` and its exit resolves as `NL`: it gets `502 Exit country mismatch: requested DE, observed NL (…)`. Retrying with `job43` within the TTL gets the same 502 from the cache; the client moves on to `job44` and gets a different vendor session.

**4. Dynamic, `retry`, no client session, `round_robin`.**
Every request mints a fresh vendor session, so every `-cc-de` request is echoed (a place is required, so sampling does not apply). A mismatch selects the gateway again, which mints another session, up to three sessions in all. The client sees nothing but latency unless all three miss. Without `-cc-` and with no allow-list on the connector, nothing is required: 5% of requests are echoed for the accuracy page and all are forwarded.

**5. Dynamic, `retry`, client session, `sticky`.**
`sticky` will not move a bound session, so a mismatch is a 502, as under `reject`. If the project also has a second connector, that does not help: the session stays where it hashed. Use `retry` with `random` or `round_robin` when moving matters more than affinity.

**6. Pooled slots, `retry`, `round_robin`, two mismatches.**
The request picks slot 3, which echoes as `FR`: slot 3 is removed for rotation and the strategy picks slot 8 with 3 excluded. Slot 8 echoes as `AT`: removed as well, and slot 1 is tried as the third and last attempt. It matches and the request goes through, having cost three echo round trips. Two fresh sessions are provisioned by the next sync.

**7. `report` with `strict`, hoping for the best of both.**
Preflight records the mismatch and forwards. The slot is not judged by that, so `strict` does nothing on the next request. The verdict arrives, if at all, from the next health check that sees the slot at a new IP, exactly as it would with preflight off, and only then is the slot replaced. If you want preflight to act, use `retry` or `reject`.

**8. ISP or datacenter ports.**
These are fixed exits: discovery placed each port's IP and the hourly refresh re-reads it, dropping a port whose IP left the connector's listed countries. Preflight does not apply. Attribution judges the vendor's reported country against the databases on every sighting, so `strict` works on them and the accuracy page covers them.

**9. A pool with no countries listed and a `-cc-de` request.**
The first `-cc-de` request creates a group of `num_proxies` sessions geo-targeted to Germany, on demand, and routes to one of them; those slots claim `DE` and are preflighted against it when preflight is on. An existing untargeted slot whose last health check happened to resolve as `DE` is eligible for the request too, and preflight (a place was asked) verifies it against `DE` before forwarding.

## Choosing a configuration

* **The country must be right, every request.** Pooled or dynamic residential with `reject`, or `retry` with a non-sticky strategy. Keep `strict` on for pooled slots so a drifted slot is replaced rather than left taking untargeted traffic, with at least one IP database loaded. Expect one echo per session.
* **The country must be right and sessions must hold.** `sticky` with `reject`: a misplaced session fails fast and the client restarts it under a new `-sessid-`.
* **Just measure the vendor.** `report`, and `exit_sample_percent` at 100 on dynamic connectors for a complete count; the accuracy page does the rest.
* **Fixed exits only.** Preflight is irrelevant. The source order, conflict rule and policy are what matter; `strict` with `consensus` is a safe default once a database is loaded.
