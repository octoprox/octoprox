---
layout: docs
title: "Location Routing: Settings and Scenarios"
nav_id: location-routing
description: "How the conflict rule, the two location decisions, the preflight switch and the routing strategy combine for pooled sessions, dynamic sessions and fixed exits, with worked examples."
---

# Location Routing

<p class="subtitle">What decides where a request goes when a country, state or city is asked for, what happens when the exit is not there, and how that differs between pooled sessions, dynamic sessions and fixed exits.</p>

Four project settings, the routing strategy and one connector setting decide a request's fate once it names a place. Each is simple on its own; the combinations are not. This page walks through them with examples. The mechanics of attribution (databases, the echo endpoint, what is stored where) are on the [IP attribution]({{ site.baseurl }}/ip-attribution) page and the username syntax on [Routing strategies]({{ site.baseurl }}/routing-strategies#country-routing).

Two of the settings are decisions about an exit whose location is in doubt: what to do when the independent sources contradict the vendor's claim (`location_contradicted`), and what to do when nothing independent can place the exit at all (`location_unknown`). The kind of proxy row decides the mechanism that applies them: the verdict written on the row for fixed exits and pooled session slots, a preflight verdict for dynamic sessions, and both for a pooled slot in a project with preflight on.

## The settings at a glance

| Setting | Where | What it decides |
|---------|-------|-----------------|
| **Vendor contradicted when** (`location_conflict_rule`) | Project, Location tab; default under Settings, IP attribution | When the vendor's claim counts as contradicted: `consensus` (every independent source agrees against it), `first` (the top-ranked independent source disagrees) or `any` (no independent source agrees with it; one that does confirms it). |
| **City radius** (`location_city_radius_km`) | Project, Location tab; default under Settings, IP attribution | How far from the claimed city an independent answer may place the exit and still confirm a city claim (default 25 km, 0 compares names only). Needs a GeoNames file loaded; see [City coordinates and the city radius]({{ site.baseurl }}/ip-attribution#city-coordinates-and-the-city-radius). |
| **Databases contradict the vendor** (`location_contradicted`) | Project, Location tab | The independent sources agree against the vendor's claim. `evidence` (default) routes the proxy on where they put the exit; `vendor` routes it on the claim anyway and only shows the contradiction; `refuse` skips the proxy and replaces a pooled session slot. |
| **No database knows the exit** (`location_unknown`) | Project, Location tab | No database knows the IP, or the databases disagree under `consensus`. `vendor` (default) routes on the vendor's word; `refuse` skips the proxy until a later sighting places it. |
| **Preflight check** (`location_preflight`) | Project, Location tab | Whether a session's exit is verified with an echo request before its first request is forwarded. What the verdict then does to the request is the two decisions' call. |
| **Routing strategy** | Project, Routing tab | Whether a request that preflight refused may move to another proxy. `sticky` promised a session one exit, so it will not move it. |
| **Exit sampling** (`exit_sample_percent`) | Connector, dynamic sessions only | The share of session-less requests preflight echoes when nothing has to be verified. |

There is no setting for which source decides a proxy's country. The value is the databases, in their configured priority, then the echo endpoint, then the vendor filling only what nothing independent answered, with one exception each for the decisions and the rule, below.

Two ideas to keep apart, because the settings act on different ones:

* The **value** is the country (state, city) routing matches a request's `-cc-` (`-st-`, `-city-`) against. It is the independent answer when there is one and the vendor's word when there is none.
* The **verdict** is a judgement of the vendor: confirmed, contradicted or open. It comes from the conflict rule. The two decisions act on the verdict, wherever it was produced: on the row by a health check, discovery, manual locate or re-attribution, or for one request by preflight.

The decisions change whether a proxy is routed to, not what it is routed on, with one exception: `location_contradicted: vendor` routes a contradicted level on the vendor's claim, since that is what trusting the vendor means. The conflict rule changes the verdict, and the value only under `any`: a claim confirmed by a lower-ranked source is routed on that source's answer, so a confirmed row is never routed on a country its verdict disagrees with. The city radius changes the city verdict, and a city it confirms is routed on the claim: a row the databases place in Diadema that was claimed as São Paulo serves `-city-sao_paulo`.

## Three kinds of proxy row

Everything below depends on what kind of row the request lands on, because the three kinds learn their exit differently and only some can be verified or flagged.

| | Fixed exit | Pooled session slot | Dynamic-sessions gateway |
|---|---|---|---|
| Examples | Static proxies, ISP and datacenter ports (Oxylabs, Decodo, Bright Data), proxy lists (Webshare), cloud instances | Residential and mobile connectors with **Sessions: Fixed pool**: `num_proxies` rows per country, each holding one vendor session | Residential and mobile connectors with **Sessions: Dynamic**: one row, a vendor session rendered per request |
| Where the exit comes from | Discovery when the row is created, re-read on every IP refresh and health check | The vendor, behind the session; seen only by the health check (every minute, when the check URL echoes the caller) and by preflight | The vendor, per request; seen only by preflight |
| Health-checked | Yes | Yes | No. The row is always healthy; a dead credential shows as failed requests |
| Vendor claim judged | The country (and state or city) the vendor's list or discovery endpoint reported, or the one an operator pinned | The country the slot was provisioned for (`-cc-` on demand, or the connector's list). Untargeted slots make no claim | The `-cc-` of the request, or the country the allow-list rendered for it |
| Can carry the contradiction flag | Yes | Yes | **Never**. Nothing is written on the row, so the decisions reach it only through preflight |
| Preflighted | **Never**, whatever was asked. Its exit is already known and re-read every minute | When the client names a place. A client that names nothing has no expectation, and the slot's own promise is judged on the row by every sighting of its exit | Always, once preflight is on |

## What a proxy is routed on

Attribution resolves every sighting of an exit IP and writes the result on the row as its country, state and city. The value has one fixed order: the databases, in their configured priority, then the echo endpoint, then the vendor, which fills only what nothing independent answered. Routing then matches `-cc-` exactly against that country: a row whose country is known must match, and a row with no country is eligible only through its connector's declared list.

The examples use two databases (MaxMind and DB-IP, in that priority), an echo endpoint that reports no country of its own, and an Oxylabs residential connector listing Germany and France with a slot provisioned for Germany, so the vendor claims `DE`. The project routes a contradicted exit on the evidence (`location_contradicted: evidence`).

| Case | MaxMind | DB-IP | Under `consensus` | Under `first` | Under `any` |
|------|---------|-------|-------------------|---------------|-------------|
| Everything agrees | DE | DE | routed on DE, **confirmed** | routed on DE, **confirmed** | routed on DE, **confirmed** |
| Databases agree against the vendor | FR | FR | routed on FR, **contradicted** | routed on FR, **contradicted** | routed on FR, **contradicted** |
| Databases disagree with each other | FR | DE | **open** (no flag): routed on DE, the vendor's word, under `location_unknown: vendor`; routed on FR, and skipped, under `refuse` | routed on FR, **contradicted** (MaxMind outranks DB-IP) | routed on DE, **confirmed** (DB-IP agrees with the vendor, so its answer is the value) |
| No database knows the IP | - | - | routed on DE, the vendor's word, **open** | routed on DE, the vendor's word, **open** | routed on DE, the vendor's word, **open** |
| Untargeted slot, no claim | FR | FR | routed on FR, no verdict | routed on FR, no verdict | routed on FR, no verdict |

What to take from the table:

* **The conflict rule decides the verdict, and under `consensus` and `first` never the value.** The top-ranked source with an answer decides, unless the claim is left open and the project trusts the vendor for unknowns, in which case the open claim carries the vendor's word, as it does when no source answers at all. So the third row is routed on MaxMind's `FR` under `first`, and under `consensus` on `DE` or `FR` by the unknown decision: `consensus` refuses to blame the vendor on one dissenting database and leaves the claim open, `first` flags on the same evidence it routes on. `any` is the generous rule: one independent source agreeing with the vendor confirms the claim, and the row is then routed on that source's answer rather than the top-ranked one, so a confirmed row is never routed on a country its verdict disagrees with. With no source agreeing, `any` is a plain contradiction routed on the top-ranked answer, and with no evidence at all the claim is open as under the other rules. A project that wants no routing on unsettled evidence sets `location_unknown` to `refuse`, which skips open claims.
* **The databases override the vendor** for routing whenever one of them knows the IP, unless the project chose otherwise. Under `evidence` a contradicted slot serves `-cc-fr` requests and untargeted requests, and no longer serves `-cc-de`, because its country is now `FR` and matching is exact. Under `location_contradicted: vendor` the second row is routed on `DE`, the vendor's claim: it keeps serving `-cc-de` and the contradiction is only shown. Under `refuse` it serves nothing at all.
* **The echo endpoint is independent evidence too.** When no database knows the IP but the echo endpoint reports a country, the exit is routed on the echo's answer and the vendor's claim is judged against it. An echo that reports no country, like the one in the example, places nothing.
* **The vendor fills the gap** only when neither a database nor the echo can place the IP. Such a row is routed on the vendor's word and its claim is open: `location_unknown: vendor` serves it, `refuse` skips it until a later sighting places the exit.
* **The connector's declared countries bound every setting.** A row whose resolved country lies outside its connector's list is never routed to, whatever was asked and whatever the decisions. Had the connector above listed Germany alone, the second row under `evidence` would serve neither `-cc-fr` nor untargeted traffic. A row with no country yet is still eligible through the list. The rule binds rows the vendor placed in a declared country: a slot provisioned for one country that the evidence puts in another, or a port-mode exit listed under a country it has left. It is a skip, never a replacement: only `location_contradicted: refuse` spends a fresh session, since a database that mis-places a vendor's range would otherwise replace every session it minted, forever. A row that made no claim is bound by the list only for a request that names a country.

A verdict changes when the evidence does: a later health check that sees the slot at a new IP re-resolves it, and **Re-attribute proxies** on the settings page re-judges every row offline after a database is added or replaced. The value is written when the row is judged, so changing `location_contradicted`, `location_unknown` or the conflict rule re-judges the project's rows in the background, the same way, and a row whose exit has not moved does not have to wait for it to. The verdict is shown on the Proxies page: a red triangle for a contradicted claim, a question mark for an open one.

**After the upgrade from the assurance levels, nothing was judged again at once.** The three levels became pairs of decisions: `observe` became `evidence` + `vendor`, `verified` became `refuse` + `vendor`, and `confirmed` became `refuse` + `refuse`. A row judged under the retired vendor-first order, or without its state and city verdicts, is judged again when its exit moves, when a database or the project's decisions change, or when **Re-attribute proxies** is pressed. Rows of an `observe` project behave as before, while preflight now refuses a place the client named that the exit is not in; no pair of decisions keeps both. A project that had `confirmed` now skips a judged row that made no claim and that nothing independent placed, as `location_unknown: refuse` does for every project.

## What the two decisions do

Each decision reads one shape of verdict and says whether the exit behind it may serve. The verdict is the one written on the row (fixed exit, pooled slot) or, with preflight on, the one produced for the request (pooled slot when a place is required, dynamic gateway per vendor session). The same two decisions apply to both, and they mean the same thing at both. A confirmed verdict, and a row not judged yet, serve under every setting.

**`location_contradicted`** acts when the independent sources agree against the vendor's claim: the databases, or the echo endpoint, place the exit somewhere other than where the vendor said.

* **`evidence`** (default) trusts the databases over the vendor. The row is routed on the country the evidence puts it in: the German slot the databases place in France serves `-cc-fr` and untargeted traffic, and the contradiction is shown and counted in provider accuracy. The slot is kept, since the exit is placed and useful where it is. A contradicted level that cannot be routed on the evidence is skipped: a country, state or city pinned by hand, or a state or city only the vendor supplied, since the evidence has no value of its own there. At preflight, a place the client named is held exactly, and a vendor promise is held to the connector's declared countries instead.
* **`vendor`** trusts the vendor over the databases. The row is routed on the vendor's claim: the German slot the databases place in France still serves `-cc-de`, and the contradiction is only shown and counted. A contradicted pin stays as well. Preflight forwards a contradicted exit, whoever named the place. Choose this for a vendor you trust more than the databases, or to record contradictions without acting on them.
* **`refuse`** holds the vendor to its word. A contradicted fixed exit is skipped and a contradicted pooled session slot is skipped and removed, so the provider sync provisions a fresh session in its place. A fixed exit is only skipped: the port refresh already drops an exit that left the connector's countries, and a static row is the operator's to fix. At preflight, a contradicted exit refuses the request and the strategy decides whether it moves (below), and a pooled slot is removed either way.

| Contradicted verdict | `evidence` | `vendor` | `refuse` |
|----------------------|------------|----------|----------|
| Country, state or city placed by a database or the echo | serves, on the resolved place | serves, on the vendor's claim | refused: fixed exit skipped, pooled slot removed and replaced |
| Country, state or city pinned by hand | skipped | serves, on the pin | skipped |
| State or city the vendor alone supplied | skipped | serves, on the claim | skipped |

**`location_unknown`** acts when nothing independent places the exit: no loaded database knows the IP and the echo reports no country, or the databases disagree with each other under `consensus`. The verdict is open.

* **`vendor`** (default) routes on the vendor's word. The slot provisioned for Germany whose IP no database knows is routed on `DE`, and a question mark on the Proxies page says so. With no database loaded this is every claim.
* **`refuse`** waits for evidence. An open row is skipped until a later sighting places it, and so is a judged row that nothing could place: one with no country at all, or one whose country only the vendor supplied. A state or city the vendor alone supplied is skipped for a `-st-` or `-city-` request. At preflight, an open level the client named is refused with `502 Exit country not confirmed`, and so is a promised country no source can place. A skipped slot is kept, not replaced: rotating on missing evidence would churn a pool the databases do not cover, at the vendor's price, for sessions just as unknown as the last.

| Open verdict | `vendor` | `refuse` |
|--------------|----------|----------|
| Country, state or city the vendor supplied, nothing independent answered | serves, on the vendor's word | skipped |
| Databases disagree with each other under `consensus` | serves, on the top-ranked answer | skipped |
| Judged row with no country at all | serves untargeted requests | skipped |
| Country, state or city pinned by hand | serves | serves |

When they read the row, the decisions do not look at the request, so a row they refuse is out for every request of the project, including untargeted ones.

Rules that hold under every setting:

* **A row not judged yet is eligible.** A slot provisioned seconds ago, before its first health check, or a static proxy before its first lookup, is routable under every setting so that on-demand provisioning and a fresh pool work. A row with no claim (an untargeted slot, a cloud instance) is judged by its sightings like any other and has no vendor claim to refuse, so it stays eligible, with two bounds: under `location_unknown: refuse` a judged row that nothing independent could place is skipped, and its connector's list binds it for a request that names a country.
* **A judged row nothing could place is skipped under `location_unknown: refuse`.** No database or echo placed it: either it has no country at all, or its country is the vendor's word alone. `vendor` serves it for untargeted requests; no setting serves a row with no country for a `-cc-` request it cannot match.
* **A pinned country is the operator's word, and stays the routing value.** It is trusted while open under both `location_unknown` values, since a static list in ranges no database covers would otherwise go dark. When the evidence agrees against it, the row is shown as contradicted and kept under `evidence` and `vendor`, since the operator placed it deliberately and attribution never overwrites a pin; only `location_contradicted: refuse` skips it. A pinned state or city reads the same way.
* **An open row is skipped, not replaced.** Only a contradiction under `location_contradicted: refuse` replaces a slot.
* **State and city are judged like the country, when asked for.** Attribution writes a `state_verdict` and `city_verdict` on the row next to the country's. For a `-st-` or `-city-` request on a fixed exit the asked level is judged the same way: a contradicted state or city is kept only under `vendor` when its value came from the vendor or a pin, and under `evidence` or `vendor` when a database or the echo placed it; under `location_unknown: refuse` the state or city must be placed by a database or the echo endpoint, or confirmed, or pinned by hand, so one the vendor alone supplied is skipped. One no source knows is never eligible for the request. A city verdict only matters when a city was asked for: a fixed exit whose city is contradicted still serves a plain `-cc-` request.

So with the table above, under `evidence` + `vendor` every row serves (the second on `FR`). `refuse` + `vendor` replaces the second and routes to the rest. `evidence` + `refuse` routes to the first and second rows (the second on `FR`) and waits on the third and fourth. `refuse` + `refuse` routes only to the first row; the third and fourth wait and the second is replaced. `vendor` + `vendor` routes to every row, the second on `DE`. Rows without a claim (untargeted slots, cloud instances, static proxies with no country set) carry no verdict, so `location_contradicted` never touches them; they stay eligible under `location_unknown: vendor`, and under `refuse` only once a database or the echo has placed them.

**`location_unknown: refuse` needs databases.** With no IP database loaded and an echo endpoint that reports no country, every claim is open, and a project refusing unknown exits routes to nothing that carries a claim and has been judged. Load at least one database before turning it on, and expect residential pools in countries the databases cover poorly to run short. `location_contradicted: refuse` is harmless without databases: every claim is open, so it refuses nothing.

**A dynamic-sessions gateway is reached only through preflight.** Its exits belong to the sessions it renders, nothing is written on the row, and so there is no row verdict to act on. With preflight off, the decisions do nothing to a dynamic connector at all.

**Every refusing decision needs preflight for residential and mobile sessions.** The switch is independent of the decisions, so choosing `refuse` does not turn preflight on. With preflight off, the decisions act on a pooled slot only through the verdict the health check wrote on its row, so a session whose exit moved since the last check is trusted until the next one, and they do not act on a dynamic-sessions connector at all. A project with pooled or dynamic residential or mobile connectors that wants either decision to hold per request has preflight on; the Location tab warns when it is not. Fixed exits need no preflight: their row verdict is the whole story.

## Preflight

With preflight on, the request is routed as usual and then, before it is forwarded, one echo request goes through the selected upstream. The exit IP is resolved under the project's conflict rule with the **required place** as the claim:

1. the request's own `-cc-`, `-st-` and `-city-`;
2. else, on a dynamic gateway only, the country rendered into the vendor session from the connector's allow-list, and any state or city rendered with it.

A pooled slot is not checked for a request that named nothing: the client has no expectation, and the slot's own promise is judged on the row by every sighting of its exit, including the echoes that targeted requests make. A country pinned by hand is judged on the row too.

A fixed exit is never preflighted. The verdict is cached per project and proxy (per vendor session and place for a dynamic row) for `preflight_session_ttl_seconds` (default 600), and dropped early when a health check sees the exit move.

Who named the place decides how `location_contradicted` reads a mismatch. When the client named it, the exit is held to it exactly: a contradicted level refuses the request unless the decision is `vendor`. When the client named nothing and the vendor promised the place, `refuse` holds the vendor to the exact country it rendered and to the city or state rendered with it; `evidence` holds the exit to the connector's declared countries instead: a placed exit anywhere in the list is forwarded (anywhere at all when the connector lists no countries), one outside is refused, and a city or state the vendor rendered is not checked; `vendor` checks nothing about the place. Whatever was named, `location_unknown: refuse` refuses an exit nobody can place, and an open state or city the client named; a pinned country is trusted while open and kept when contradicted only under `vendor`.

The check has three outcomes:

| Outcome | Example | What happens |
|---------|---------|--------------|
| Match | `-cc-de`, exit resolved as DE | forwarded |
| Open | IP unknown to every database and the echo, or databases disagree under `consensus` | forwarded under `location_unknown: vendor`; refused under `refuse` with `502 Exit country not confirmed: requested US, observed unknown (ip)`, at every level the client named: an open city on a `-city-` request refuses too (`502 Exit city not confirmed: requested GB, city london, observed GB (ip)`). A country the vendor promised is refused when open under `refuse`; a city or state the vendor rendered for a dynamic request only under `refuse` + `refuse`. A pinned country is forwarded while open |
| Mismatch | `-cc-de`, exit resolved as FR; or `-city-manchester`, exit in London | a place the client named, or a pin that disagrees: forwarded under `location_contradicted: vendor`, refused under `evidence` and `refuse` (`502 Exit country mismatch: requested US, observed GB (ip)`, `502 Exit city mismatch: requested GB, city manchester, observed GB, city london (ip)`). A vendor promise that failed: forwarded under `vendor`; under `evidence` forwarded when the exit is inside the connector's countries and refused when outside (`502 Exit country outside the connector's countries: requested US, observed GB (ip)`); refused under `refuse` as a plain mismatch |

An echo that fails (endpoint unreachable, timeout) is not a verdict and never refuses, under any setting: the request is forwarded.

### On a mismatch

A refused verdict has no mode of its own. Whether the request moves to another proxy is the routing strategy's call, up to `max attempts` proxies in all: `preflight_max_attempts` under Settings, IP attribution, install-wide, default 3, counting the first proxy, so 3 means two retries. What happens depends on `location_contradicted`, the kind of row, whether the client sent `-sessid-`, and the strategy.

| Row | `-sessid-` | Strategy | `vendor` | `evidence` | `refuse` |
|-----|-----------|----------|----------|------------|----------|
| Pooled slot | none, or any under a non-sticky strategy | any but sticky with a session | forwarded through the misplaced exit; the observation is recorded, nothing else happens | slot excluded, another tried, up to max attempts, then 502. The slot is kept, re-judged by the same echo (a worker writes the row within seconds), and routed on where the exit is from then on | slot excluded, another eligible proxy tried, up to max attempts, then 502. The slot is removed and the sync provisions a fresh session; in a pool with no countries listed, only while the country has other slots: once its last is gone, the next request for the country asks for a new group (example 11) |
| Pooled slot | yes | `sticky` | forwarded | **no move**: 502 at once. The slot stays, re-judged by the same echo; the binding re-binds on the next request if the row no longer serves the place | **no move**: 502 at once, the slot is removed. The next request with the same `-sessid-` finds its binding gone and binds to another proxy, which is verified afresh |
| Dynamic gateway | none | any | forwarded | the **same gateway** is selected again, which mints a fresh vendor session; up to max attempts, then 502 | the same |
| Dynamic gateway | yes | any but `sticky` | forwarded | the gateway is excluded and another connector's proxy is tried with the same `-sessid-`; with no other connector, 502 | the same |
| Dynamic gateway | yes | `sticky` | forwarded | 502 at once, when refused | 502 at once |
| Fixed exit | - | - | not preflighted | not preflighted | not preflighted |

An open level refused under `location_unknown: refuse` moves or fails by the same rows as a refusal under `evidence`, under every `location_contradicted` value. The pooled slot is **not** removed: nothing is known about its exit yet. The echo that refused the request also re-judges the row, which a worker writes within seconds, so the unplaced slot is out of rotation from then on while the decisions say so; a `sticky` binding to it finds the proxy ineligible and re-binds on its next request. The slot waits there until a later sighting settles the claim.

Points that are easy to miss:

* **`vendor` + `vendor` never refuses anything at preflight.** It is the record-only pair: the observation is recorded for provider accuracy and unique exits, and that is all. A preflight mismatch does not flag the slot; only a health check, discovery, manual locate or re-attribution writes the flag, and they do so whether preflight is on or off. Under this pair the flag then changes nothing either.
* **`evidence` refuses a client-named mismatch.** A request that asked for `-cc-de` and landed in France cannot be routed on the evidence, so it is refused and moved when the strategy allows; only a vendor promise is read against the declared list. A project that wants preflight to only record sets `location_contradicted` to `vendor`.
* **Whether a refused request moves is the strategy's call, not the decisions'.** The decisions refuse; `round_robin`, `random`, `least_used` and `health_based` then try another proxy, `sticky` with a session fails at once. Fail-fast on a non-sticky strategy is `preflight_max_attempts: 1`. A client session on a dynamic gateway in a single-connector project has nowhere to go: 502 under every strategy.
* **Only a contradiction under `location_contradicted: refuse` rotates a slot.** `evidence` keeps a contradicted slot, since the exit is placed and serves where it is, `vendor` keeps it because it trusts the claim, and no setting rotates on missing evidence.
* **A dynamic client session that was refused stays refused for the TTL.** The vendor session is derived from the `-sessid-` and the project, so the same `-sessid-` asking for the same place lands on the same vendor session, and the cached verdict answers 502 without a new echo until the TTL passes. Asking for a different `-cc-` is verified afresh. Clients should pick a new `-sessid-`.
* **A pooled client session is not lost.** Under `refuse` the misplaced slot goes, the `-sessid-` re-binds, the sync fills the pool. Under `sticky` the client sees one 502 (another if the replacement is also misplaced, since it is verified in turn); under the other strategies the request moves and the client sees only latency.
* **Sampling never weakens a guarantee, and the allow-list is not one under `evidence`.** On a dynamic connector, session-less requests are echoed at `exit_sample_percent` when they require nothing, and under `vendor` + `vendor` even when they name a country, since nothing can be refused. A session-less request that names a country is verified every time under every other pair. One that names nothing and lands on a connector with an allow-list is verified every time only where the promise is held exactly or unknown exits are refused; under `evidence` with trusted unknowns the exit is checked against the list on the sampled requests only: a sampled exit outside the list is refused, an unsampled one goes unseen, so the sampling rate is how much of that promise the operator pays to enforce, and 100 makes it a full guarantee.
* **Two decisions, one row.** A pooled slot in a project with preflight on is judged by every sighting of its exit, whether the health check saw it or preflight did: preflight hands its echo to attribution exactly as a health check does, and judges the slot on it at once: under `refuse` the gateway takes a contradicted slot out of its rotation on the spot, rather than a health check interval later, and a worker writes the row's location and verdict and replaces the slot within seconds, after which it leaves every other gateway's pool too. Preflight additionally judges the request itself against the place the client asked for. Under `evidence` the row keeps the slot and routes it on where it is; under `vendor` on the claim.
* **The declared list is checked twice as well.** On the row, a resolved country outside the connector's countries makes the row ineligible under every setting; at preflight, `evidence` refuses an exit outside the list when only the vendor's promise was at stake, where `refuse` refuses it as a plain mismatch and `vendor` forwards it.

## Scenarios

**1. Residential pool, Germany and France listed, preflight off, `evidence` + `vendor` (the defaults).**
Ten Oxylabs sessions are provisioned with `cc-de` in the credentials and ten with `cc-fr`. The health check, hitting the echo endpoint every minute, reports each slot's exit; attribution resolves it. German slot 7 resolves as `FR` under both databases: its country becomes `FR`, it is flagged, the accuracy page shows one contradicted exit of twenty. A `-cc-de` request is now spread over nine slots; slot 7 serves `-cc-fr` and untargeted requests alongside the French ones. The vendor's next session rotation may bring it back to `DE`, at which point the next health check clears the flag. Had the connector listed Germany alone, slot 7 would serve nothing at all under `evidence`: a row outside its connector's countries is never routed to. It then waits in the pool, skipped, until the vendor moves it back; only `refuse` would replace it.

**2. The same pool under `refuse` + `vendor`, and under `refuse` + `refuse`.**
Under either pair slot 7 is removed the moment the health check contradicts it, and the next provider sync provisions a fresh German session in its place, so the pool stays at twenty. The removed exit is counted once as contradicted in provider accuracy. Had the databases disagreed about slot 7 instead, the claim would be open under `consensus`: with `location_unknown: vendor` the slot stays and serves on its resolved country, `FR` (MaxMind outranks DB-IP), so `-cc-fr` and untargeted requests reach it; with `refuse` it stays too, but skipped by every request of the project until a health check finds it at an IP the databases agree on, and the pool runs on nineteen meanwhile. Under the `any` rule the same disagreement confirms the claim, since DB-IP agrees with the vendor, and slot 7 is routed on `DE`. Watch the connector's provider accuracy and change vendor if the rate is poor.

**3. The same pool under `evidence` + `refuse`.**
Slot 7, placed in France by both databases, is contradicted and kept: it serves `-cc-fr` and untargeted requests on the country the evidence puts it in, as under the defaults, and the accuracy page counts the contradiction. Nothing is replaced, so a vendor that misplaces sessions costs no extra sessions. Had the connector listed Germany alone, slot 7 would be skipped instead, since France is outside the list. The open case is the one this pair acts on: a slot the databases disagree about under `consensus`, or one no database knows, is skipped until a later health check places it.

**4. The same pool under `vendor` + `vendor`: a vendor the operator trusts over the databases.**
The pool runs in ranges the databases attribute badly, and the operator has checked a sample by hand and found the vendor right. Slot 7, which both databases put in France, keeps `DE` as its routing value: it goes on serving `-cc-de` requests, the Proxies page shows the warning triangle with "Vendor claimed DE; attribution says FR", and the accuracy page counts the contradiction, so the vendor is still measured. Had the connector listed Germany alone, slot 7 would still serve, since the value it is routed on is inside the list. Nothing is ever refused or replaced; a `-cc-de` request that lands on an exit no database knows is served on the vendor's word as well. Turning preflight on under this pair only adds sightings. Switching `location_contradicted` back to `evidence` later and re-attributing routes slot 7 on `FR` again.

**5. Dynamic Oxylabs, `refuse` + `vendor`, preflight on, client sessions.**
A client sends `-sessid-job42-cc-de`. The gateway renders a vendor session derived from `job42` and the project, with `cc-de`. One echo goes through it; the exit resolves as `DE`; the request is forwarded, and so is every later `job42` request for the next ten minutes with no further echo. A second client sends `-sessid-job43-cc-de` and its exit resolves as `NL`: it gets `502 Exit country mismatch: requested DE, observed NL (…)`. Retrying with `job43` within the TTL gets the same 502 from the cache; the client moves on to `job44` and gets a different vendor session. A third client's exit is an IP no database knows: `location_unknown: vendor` forwards it on the vendor's word. Under `refuse` it would get `502 Exit country not confirmed: requested DE, observed unknown (…)` instead. Under `location_contradicted: evidence` the second client is refused just the same, since it named the country itself; under `vendor` it is forwarded to the Dutch exit.

**6. Dynamic, `refuse` + `vendor`, preflight on, no client session, `round_robin`.**
Every request mints a fresh vendor session, so every `-cc-de` request is echoed (a place is required, so sampling does not apply). A mismatch selects the gateway again, which mints another session, up to three sessions in all. The client sees nothing but latency unless all three miss. Without `-cc-` and with no allow-list on the connector, nothing is required: 5% of requests are echoed for the accuracy page and all are forwarded. Under `vendor` + `vendor` the 5% would apply to the `-cc-de` requests as well, since nothing is ever refused.

**7. Dynamic, `refuse` + `vendor`, preflight on, client session, `sticky`.**
`sticky` will not move a bound session, so a mismatch is a 502 at once. If the project also has a second connector, that does not help: the session stays where it hashed. Use `random` or `round_robin` when moving matters more than affinity.

**8. Pooled slots, `refuse` + `vendor`, preflight on, `round_robin`, two mismatches.**
The request picks slot 3, which echoes as `FR`: slot 3 is removed for rotation and the strategy picks slot 8 with 3 excluded. Slot 8 echoes as `AT`: removed as well, and slot 1 is tried as the third and last attempt. It matches and the request goes through, having cost three echo round trips. Two fresh sessions are provisioned by the next sync. Under `evidence` the same `-cc-de` request is refused by slots 3 and 8 just the same, since the client named the country, but both slots stay in the pool and are routed on `FR` and `AT` where the list allows; an untargeted request through slot 3, whose vendor promise of `DE` failed, is forwarded under `evidence` as long as France is among the connector's countries.

**9. The defaults with preflight on, hoping for the best of both.**
Under `evidence` + `vendor`, preflight refuses a `-cc-de` request whose exit is in France and moves it when the strategy allows. The same echo re-judges the slot: within seconds its row reads `FR`, contradicted, so later `-cc-de` requests do not land on it, while `-cc-fr` and untargeted requests can, provided `FR` is in the connector's list. If you want the slot replaced instead, set `location_contradicted` to `refuse`, which refuses the request, takes the slot out of rotation on the spot and replaces it within seconds. If you want preflight to only record, set it to `vendor`.

**10. ISP or datacenter ports.**
These are fixed exits: discovery placed each port's IP and the hourly refresh re-reads it, dropping a port whose IP left the connector's listed countries. Preflight does not apply. Attribution judges the vendor's reported country against the databases on every sighting, so both decisions work on them and the accuracy page covers them. A `-city-` request against such a port reads the row's city verdict: under `location_unknown: refuse` the city has to come from a database or the echo, or be confirmed, so a city only the vendor's list named is skipped until a city-level database places it; under `vendor` it serves on the vendor's word.

**11. A pool with no countries listed and a `-cc-de` request.**
The first `-cc-de` request creates a group of `num_proxies` sessions geo-targeted to Germany, on demand: the gateway asks a worker for it and holds the request until the group arrives through the routing feed (usually within a few seconds, at most 5), then routes to one of them; those slots claim `DE` and are preflighted against it when preflight is on. An existing untargeted slot whose last health check happened to resolve as `DE` is eligible for the request too, and preflight (a place was asked) verifies it against `DE` before forwarding.

## Choosing a configuration

* **The country must be right, every request.** Pooled or dynamic residential with `location_contradicted: refuse`, `location_unknown: vendor`, preflight on and a non-sticky strategy, so a misplaced exit is refused and the request moves. A drifted pooled slot is replaced rather than left taking untargeted traffic. Set `location_unknown` to `refuse` as well when the vendor's word is not enough, with at least one IP database loaded and a pool sized for what the databases do not cover. Expect one echo per session.
* **Every exit must be placed, but vendor mistakes should not cost sessions.** `location_contradicted: evidence`, `location_unknown: refuse`, preflight on, with at least one IP database loaded. You trust the databases more than the vendor and want every exit you use placed by them, but a slot the vendor put in the wrong country is still a placed exit: it is routed on where it is and kept, instead of being refused and replaced at the vendor's price. Untargeted traffic is held to the connector's declared countries rather than to the vendor's exact promise. Pick `refuse` for the contradicted decision when a wrong vendor claim should also take the slot out.
* **The country must be right and sessions must hold.** `sticky` with `location_contradicted: refuse` and preflight on: a misplaced session fails fast and the client restarts it under a new `-sessid-`.
* **The vendor is right and the databases are not.** `location_contradicted: vendor`, `location_unknown: vendor`: every exit is routed on the vendor's claim, and the databases only report. Use it for a vendor you have checked by hand in ranges the databases attribute badly, and watch the accuracy page for the day that stops being true.
* **Just measure the vendor.** `vendor` + `vendor` with preflight on, and `exit_sample_percent` at 100 on dynamic connectors for a complete count; the accuracy page does the rest.
* **Fixed exits only.** Preflight is irrelevant. The conflict rule and the two decisions are what matter; `refuse` with `consensus` skips what the databases agree is misplaced, `location_unknown: refuse` also skips what they cannot judge, so turn it on once a database is loaded. `any` with `evidence` is the forgiving pair for a vendor one database tends to agree with.
