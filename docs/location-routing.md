---
layout: docs
title: "Location Routing: Settings and Scenarios"
nav_id: location-routing
description: "How the source order, conflict rule, location assurance level, preflight switch and routing strategy combine for pooled sessions, dynamic sessions and fixed exits, with worked examples."
---

# Location Routing

<p class="subtitle">What decides where a request goes when a country, state or city is asked for, what happens when the exit is not there, and how that differs between pooled sessions, dynamic sessions and fixed exits.</p>

Four project settings, the routing strategy and one connector setting decide a request's fate once it names a place. Each is simple on its own; the combinations are not. This page walks through them with examples. The mechanics of attribution (databases, the echo endpoint, what is stored where) are on the [IP attribution]({{ site.baseurl }}/ip-attribution) page and the username syntax on [Routing strategies]({{ site.baseurl }}/routing-strategies#country-routing).

One setting, the assurance level, says how much a project wants to know about an exit's location before serving a request from it. The kind of proxy row decides the mechanism that applies the level: the verdict written on the row for fixed exits and pooled session slots, a preflight verdict for dynamic sessions, and both for a pooled slot in a project with preflight on.

## The settings at a glance

| Setting | Where | What it decides |
|---------|-------|-----------------|
| **Which source decides** (`location_sources`) | Project, Location tab; default under Settings, IP attribution | Which answer becomes a proxy's country, state and city for routing: the local databases, the vendor's claim or the echo endpoint, in order. The first with an answer wins. |
| **Vendor contradicted when** (`location_conflict_rule`) | Same | When the vendor's claim counts as contradicted: `consensus` (every independent source agrees against it) or `first` (the top-ranked independent source disagrees). |
| **Location assurance** (`location_assurance`) | Project, Location tab | Which verdicts on a claimed location may still serve a request, for every kind of row: `observe` serves on any verdict and only records it, `verified` refuses a contradicted claim, `confirmed` serves only a confirmed one. |
| **Preflight check** (`location_preflight`) | Project, Location tab | Whether a session's exit is verified with an echo request before its first request is forwarded. What the verdict then does to the request is the level's call. |
| **Routing strategy** | Project, Routing tab | Whether a request that preflight refused may move to another proxy. `sticky` promised a session one exit, so it will not move it. |
| **Exit sampling** (`exit_sample_percent`) | Connector, dynamic sessions only | The share of session-less requests preflight echoes when nothing has to be verified. |

Two ideas to keep apart, because the settings act on different ones:

* The **value** is the country (state, city) routing matches a request's `-cc-` (`-st-`, `-city-`) against. It comes from the source order and nothing else.
* The **verdict** is a judgement of the vendor: confirmed, contradicted or open. It comes from the conflict rule. The level acts on the verdict, wherever it was produced: on the row by a health check, discovery, manual locate or re-attribution, or for one request by preflight.

Changing the level never changes which country a proxy is routed on. Changing the source order changes the value and, under `first`, which source does the judging.

## Three kinds of proxy row

Everything below depends on what kind of row the request lands on, because the three kinds learn their exit differently and only some can be verified or flagged.

| | Fixed exit | Pooled session slot | Dynamic-sessions gateway |
|---|---|---|---|
| Examples | Static proxies, ISP and datacenter ports (Oxylabs, Decodo, Bright Data), proxy lists (Webshare), cloud instances | Residential and mobile connectors with **Sessions: Fixed pool**: `num_proxies` rows per country, each holding one vendor session | Residential and mobile connectors with **Sessions: Dynamic**: one row, a vendor session rendered per request |
| Where the exit comes from | Discovery when the row is created, re-read on every IP refresh and health check | The vendor, behind the session; seen only by the health check (every minute, when the check URL echoes the caller) and by preflight | The vendor, per request; seen only by preflight |
| Health-checked | Yes | Yes | No. The row is always healthy; a dead credential shows as failed requests |
| Vendor claim judged | The country (and city) the vendor's list or discovery endpoint reported, or the one an operator pinned | The country the slot was provisioned for (`-cc-` on demand, or the connector's list). Untargeted slots make no claim | The `-cc-` of the request, or the country the allow-list rendered for it |
| Can carry the contradiction flag | Yes | Yes | **Never**. Nothing is written on the row, so the level reaches it only through preflight |
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

* **The conflict rule never decides the value.** The top-ranked source with an answer does, under either rule, so the third row is routed on MaxMind's `FR` under `consensus` and `first` alike. The rule only decides the flag: `consensus` refuses to blame the vendor on one dissenting database and leaves the claim open, `first` flags on the same evidence it routes on. A project that wants no routing on unsettled evidence uses `confirmed`, which refuses open claims.
* **The databases override the vendor** for routing whenever one of them knows the IP. A contradicted slot serves `-cc-fr` requests and untargeted requests. It no longer serves `-cc-de`, because its country is now `FR` and matching is exact. Under `observe` that is already the case; `verified` and `confirmed` only add that it serves nothing at all.
* **The vendor fills the gap** only when no independent source knows the IP. Under the "Databases only" order the vendor never decides and such a row has no country, so only its connector's declared list can make it eligible.
* **"Vendor decides, databases flag"** is the mirror image: the second row is routed on `DE` although it is contradicted. That order says "route on the vendor's word, tell me when it is wrong". Combined with `confirmed` it routes only where the databases confirm the vendor, which is the same set as the default order gives.

A verdict changes when the evidence does: a later health check that sees the slot at a new IP re-resolves it, and **Re-attribute proxies** on the settings page re-judges every row offline after a database is added or replaced. The verdict is shown on the Proxies page: a red triangle for a contradicted claim, a question mark for an open one.

## What the level does

The level reads a verdict and decides whether the exit behind it may serve. The verdict is the one written on the row (fixed exit, pooled slot) or, with preflight on, the one produced for the request (pooled slot when a place is required, dynamic gateway per vendor session). The same three levels apply to both, and they mean the same thing at both.

When it reads the row, the level does not look at the request, so it is not "verified for the contradicted country": a row the level refuses is out for every request of the project, including untargeted ones.

**Observe.** Every verdict serves. The verdict is recorded and shown: the Proxies page shows a warning triangle with "Vendor claimed DE; attribution says FR" or a question mark for an open claim, and provider accuracy counts the exit. With the German slot above, `-cc-de` traffic simply no longer reaches it, `-cc-fr` and untargeted traffic can. Preflight, when on, only records; nothing is refused.

**Verified.** A contradicted claim is refused; an open claim serves on the vendor's word. On a row, a contradicted fixed exit is skipped and a contradicted pooled session slot is skipped and removed, so the provider sync provisions a fresh session in its place. A fixed exit is only skipped: the port refresh already drops an exit that left the connector's countries, and a static row is the operator's to fix. At preflight, a contradicted exit refuses the request and the strategy decides whether it moves (below). With no database loaded and an echo endpoint that reports no country, every claim is open and `verified` refuses nothing.

**Confirmed.** Routing wants clear evidence. A claim serves only while it is **confirmed**, or not judged yet: a slot provisioned seconds ago, before its first health check, is routable so that on-demand provisioning works, and a row with no claim is never judged. A contradicted claim is refused exactly as under `verified`. An **open** claim is refused as well, until a later sighting settles it:

* An open row is skipped, not replaced: rotating on missing evidence would churn a pool the databases do not cover, at the vendor's price, for sessions just as unknown as the last. The exception is a country an operator pinned by hand on a static proxy: it is judged like a vendor claim, but trusted while open, since the operator placed that row deliberately. A pin the databases agree against is still skipped.
* At preflight, an open country refuses the request with `502 Exit country not confirmed: requested DE, observed unknown (ip)`, or `observed FR` when the independent sources disagreed with each other. An open state or city is not refused, at any level, just as on a row: with no city-level database loaded there is no verdict below the country, and a country-level check should not fail on that.

So with the table above, `confirmed` routes only to the first row; the third and fourth rows wait and the second is replaced. `verified` replaces the second and routes to the rest. Rows without a claim (untargeted slots, cloud instances, static proxies with no country set) are never judged and stay eligible at every level. No level changes what a row is routed on, only whether it is routed to at all.

**Confirmed needs databases.** With no IP database loaded and an echo endpoint that reports no country, every claim is open, and a `confirmed` project routes to nothing that carries a claim. Load at least one database before turning it on, and expect residential pools in countries the databases cover poorly to run short.

**A dynamic-sessions gateway is reached only through preflight.** Its exits belong to the sessions it renders, nothing is written on the row, and so there is no row verdict to act on. With preflight off, the level does nothing to a dynamic connector at all.

**Verified and confirmed need preflight for residential and mobile sessions.** The switch is independent of the level, so turning the level up does not turn preflight on. With preflight off, `verified` and `confirmed` act on a pooled slot only through the verdict the health check wrote on its row, so a session whose exit moved since the last check is trusted until the next one, and they do not act on a dynamic-sessions connector at all. A project with pooled or dynamic residential or mobile connectors that wants either level has preflight on; the Location tab warns when it is not. Fixed exits need no preflight: their row verdict is the whole story.

## Preflight

With preflight on, the request is routed as usual and then, before it is forwarded, one echo request goes through the selected upstream. The exit IP is resolved under the project's source order and conflict rule with the **required place** as the claim:

1. the request's own `-cc-`, `-st-` and `-city-`;
2. else the country the vendor promised for the slot (the group it was provisioned for, or what a dynamic request rendered from the allow-list) and any state or city rendered into a dynamic request;
3. else a country pinned by hand on a static row.

A place that was merely observed is not required: an untargeted residential slot is not preflighted unless the request names a place. A fixed exit is never preflighted. The verdict is cached per project and proxy (per vendor session and place for a dynamic row) for `preflight_session_ttl_seconds` (default 600), and dropped early when a health check sees the exit move.

The check has three outcomes, and the level decides which of them refuse the request:

| Outcome | Example | What happens |
|---------|---------|--------------|
| Match | `-cc-de`, exit resolved as DE | forwarded |
| Open | IP unknown to every database, or databases disagree under `consensus` | forwarded under `observe` and `verified`; under `confirmed` refused when it is the country (`502 Exit country not confirmed`), forwarded when it is the state or city |
| Mismatch | `-cc-de`, exit resolved as FR; or `-city-manchester`, exit in London | forwarded under `observe`; refused under `verified` and `confirmed`, below |

An echo that fails (endpoint unreachable, timeout) is not a verdict and never refuses, at any level: the request is forwarded.

### On a mismatch

A refused verdict has no mode of its own. Whether the request moves to another proxy is the routing strategy's call, up to `max attempts` proxies in all: `preflight_max_attempts` under Settings, IP attribution, install-wide, default 3, counting the first proxy, so 3 means two retries. What happens depends on the level, the kind of row, whether the client sent `-sessid-`, and the strategy.

| Row | `-sessid-` | Strategy | `observe` | `verified` / `confirmed` |
|-----|-----------|----------|-----------|--------------------------|
| Pooled slot | none, or any under a non-sticky strategy | any but sticky with a session | forwarded through the misplaced exit; the observation is recorded, nothing else happens | slot excluded, another eligible proxy tried, up to max attempts, then 502. The slot is removed and the sync provisions a fresh session |
| Pooled slot | yes | `sticky` | forwarded | **no move**: 502 at once, the slot is removed. The next request with the same `-sessid-` finds its binding gone and binds to another proxy, which is verified afresh |
| Dynamic gateway | none | any | forwarded (only sampled requests are checked at all) | the **same gateway** is selected again, which mints a fresh vendor session; up to max attempts, then 502 |
| Dynamic gateway | yes | any but `sticky` | forwarded | the gateway is excluded and another connector's proxy is tried with the same `-sessid-`; with no other connector, 502 |
| Dynamic gateway | yes | `sticky` | forwarded | 502 at once |
| Fixed exit | - | - | not preflighted | not preflighted |

Under `confirmed` an open country is refused the same way, and the request moves or fails by the same rows, with one difference: an open-refused pooled slot is **not** removed, and a `sticky` binding to it is not dropped either. The same `-sessid-` gets the cached 502 until the next health check (within one interval) sees the slot's new exit, marks the row open and takes it out of rotation; the binding then points at an ineligible proxy and the session re-binds on its next request. The slot waits there until a later sighting settles the claim.

Points that are easy to miss:

* **`observe` never touches the proxy or its flag.** The observation is recorded for provider accuracy and unique exits, and that is all. A preflight mismatch under `observe` does not flag the slot; only a health check, discovery, manual locate or re-attribution writes the flag, and they do so whether preflight is on or off. Under `observe` the flag then changes nothing either.
* **Whether a refused request moves is the strategy's call, not the level's.** `verified` and `confirmed` refuse; `round_robin`, `random`, `least_used` and `health_based` then try another proxy, `sticky` with a session fails at once. Fail-fast on a non-sticky strategy is `preflight_max_attempts: 1`. A client session on a dynamic gateway in a single-connector project has nowhere to go: 502 under every strategy.
* **A dynamic client session that was refused stays refused for the TTL.** The vendor session is derived from the `-sessid-` and the project, so the same `-sessid-` asking for the same place lands on the same vendor session, and the cached verdict answers 502 without a new echo until the TTL passes. Asking for a different `-cc-` is verified afresh. Clients should pick a new `-sessid-`.
* **A pooled client session is not lost.** The misplaced slot goes, the `-sessid-` re-binds, the sync fills the pool. Under `sticky` the client sees one 502 (another if the replacement is also misplaced, since it is verified in turn); under the other strategies the request moves and the client sees only latency.
* **Sampling never weakens a guarantee.** On a dynamic connector, session-less requests are echoed at `exit_sample_percent` under `observe`, and at every level when they require nothing. Under `verified` and `confirmed` a session-less request that asks for a country, or lands on a connector whose allow-list promises one, is verified every time.
* **One level, two mechanisms.** A pooled slot in a project with preflight on is judged twice: on the row by the health check, and per request by preflight when a place is required. Both read the same level, so a slot the health check contradicted is already out before preflight would have seen it, and a slot preflight contradicted is removed before the health check would have flagged it.

## Scenarios

**1. Residential pool, Germany listed, preflight off, `observe`.**
Ten Oxylabs sessions are provisioned with `cc-de` in the credentials. The health check, hitting the echo endpoint every minute, reports each slot's exit; attribution resolves it. Slot 7 resolves as `FR` under both databases: its country becomes `FR`, it is flagged, the accuracy page shows one contradicted exit of ten. A `-cc-de` request is now spread over nine slots; slot 7 serves untargeted requests and would serve `-cc-fr`. The vendor's next session rotation may bring it back to `DE`, at which point the next health check clears the flag.

**2. The same pool under `verified`, and under `confirmed`.**
Under either level slot 7 is removed the moment the health check contradicts it, and the next provider sync provisions a fresh German session in its place, so the pool stays at ten. The removed exit is counted once as contradicted in provider accuracy. Had the databases disagreed about slot 7 instead, the claim would be open: under `verified` the slot stays and serves on its resolved country, `FR` (MaxMind outranks DB-IP), so `-cc-fr` and untargeted requests reach it; under `confirmed` it stays too, but skipped by every request of the project until a health check finds it at an IP the databases agree on, and the pool runs on nine meanwhile. Watch the connector's provider accuracy and change vendor if the rate is poor.

**3. Dynamic Oxylabs, `verified`, preflight on, client sessions.**
A client sends `-sessid-job42-cc-de`. The gateway renders a vendor session derived from `job42` and the project, with `cc-de`. One echo goes through it; the exit resolves as `DE`; the request is forwarded, and so is every later `job42` request for the next ten minutes with no further echo. A second client sends `-sessid-job43-cc-de` and its exit resolves as `NL`: it gets `502 Exit country mismatch: requested DE, observed NL (…)`. Retrying with `job43` within the TTL gets the same 502 from the cache; the client moves on to `job44` and gets a different vendor session. A third client's exit is an IP no database knows: `verified` forwards it on the vendor's word. Under `confirmed` it would get `502 Exit country not confirmed: requested DE, observed unknown (…)` instead.

**4. Dynamic, `verified`, preflight on, no client session, `round_robin`.**
Every request mints a fresh vendor session, so every `-cc-de` request is echoed (a place is required, so sampling does not apply). A mismatch selects the gateway again, which mints another session, up to three sessions in all. The client sees nothing but latency unless all three miss. Without `-cc-` and with no allow-list on the connector, nothing is required: 5% of requests are echoed for the accuracy page and all are forwarded. Under `observe` the 5% would apply to the `-cc-de` requests as well, since nothing is ever refused.

**5. Dynamic, `verified`, preflight on, client session, `sticky`.**
`sticky` will not move a bound session, so a mismatch is a 502 at once. If the project also has a second connector, that does not help: the session stays where it hashed. Use `random` or `round_robin` when moving matters more than affinity.

**6. Pooled slots, `verified`, preflight on, `round_robin`, two mismatches.**
The request picks slot 3, which echoes as `FR`: slot 3 is removed for rotation and the strategy picks slot 8 with 3 excluded. Slot 8 echoes as `AT`: removed as well, and slot 1 is tried as the third and last attempt. It matches and the request goes through, having cost three echo round trips. Two fresh sessions are provisioned by the next sync.

**7. `observe` with preflight on, hoping for the best of both.**
Preflight records the mismatch and forwards. The slot is not judged by that, and `observe` acts on nothing, so the next request can land on the same misplaced exit. The verdict arrives, if at all, from the next health check that sees the slot at a new IP, exactly as it would with preflight off, and even then `observe` only flags the slot. If you want preflight, or the health check, to act, raise the level: `verified` refuses the request and replaces the slot.

**8. ISP or datacenter ports.**
These are fixed exits: discovery placed each port's IP and the hourly refresh re-reads it, dropping a port whose IP left the connector's listed countries. Preflight does not apply. Attribution judges the vendor's reported country against the databases on every sighting, so `verified` and `confirmed` work on them and the accuracy page covers them.

**9. A pool with no countries listed and a `-cc-de` request.**
The first `-cc-de` request creates a group of `num_proxies` sessions geo-targeted to Germany, on demand, and routes to one of them; those slots claim `DE` and are preflighted against it when preflight is on. An existing untargeted slot whose last health check happened to resolve as `DE` is eligible for the request too, and preflight (a place was asked) verifies it against `DE` before forwarding.

## Choosing a configuration

* **The country must be right, every request.** Pooled or dynamic residential with `verified`, preflight on and a non-sticky strategy, so a misplaced exit is refused and the request moves. A drifted pooled slot is replaced rather than left taking untargeted traffic. Choose `confirmed` instead when the vendor's word is not enough, with at least one IP database loaded and a pool sized for what the databases do not cover. Expect one echo per session.
* **The country must be right and sessions must hold.** `sticky` with `verified` and preflight on: a misplaced session fails fast and the client restarts it under a new `-sessid-`.
* **Just measure the vendor.** `observe` with preflight on, and `exit_sample_percent` at 100 on dynamic connectors for a complete count; the accuracy page does the rest.
* **Fixed exits only.** Preflight is irrelevant. The source order, conflict rule and level are what matter; `verified` with `consensus` skips what the databases agree is misplaced, and `confirmed` also refuses what they cannot judge, so turn it on once a database is loaded.

## Upgrading from the location policy and preflight mode

Releases before this one had two settings in place of the level: `location_policy` (`warn`, `strict`) for the verdict on the row and `location_preflight` (`off`, `report`, `retry`, `reject`) for the verdict at preflight. The migration maps each project onto the level once:

| Had | Now |
|-----|-----|
| `strict`, any preflight mode | `confirmed` |
| `warn` with `retry` or `reject` | `verified` |
| `warn` with `off` or `report` | `observe` |
| preflight `report`, `retry` or `reject` | preflight on |
| preflight `off` | preflight off |

Three behaviours change on upgrade:

* A project that had `strict` with `report` now refuses at preflight instead of forwarding: `confirmed` applies at preflight as well as on the row.
* A project that had `strict` with `retry` or `reject` now also refuses an open country at preflight, where the old modes forwarded one. An exit no database knows gets `502 Exit country not confirmed` until a later sighting settles it; use `verified` to keep the old behaviour of serving on the vendor's word.
* `retry` against `reject` is no longer a setting. A refused request moves under every strategy but `sticky` with a session, up to `preflight_max_attempts`; set it to 1 for the old `reject` on a non-sticky strategy.
