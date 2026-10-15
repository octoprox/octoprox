---
layout: docs
title: "IP Attribution and Exit Verification"
nav_id: ip-attribution
description: "How Octoprox verifies where each proxy exit IP really is using MaxMind, DB-IP, IPinfo and IP2Location databases, vendor claims and an echo endpoint."
---

# IP Attribution

<p class="subtitle">Where an exit IP really is, decided from local IP databases, the vendor's own claim and an echo endpoint, and what that says about each provider.</p>

Every proxy Octoprox manages has an exit IP: the address a target site sees. Country routing (`-cc-` username suffix) needs to know which country that IP is in. IP attribution answers that with a small pipeline:

1. **Observe.** Every code path that learns an exit IP produces an *observation*: provider discovery, the exit lookup for manually added proxies, the health checker, the locate button, and the optional preflight check.
2. **Attribute.** The IP is looked up in the IP databases loaded on the instance (MaxMind, DB-IP, IPinfo, IP2Location). The databases, then the echo endpoint's answer, then the vendor's claim pick the country, in that fixed order, and the owning project's *conflict rule* decides whether the vendor's claim is contradicted.
3. **Act.** The resolved country lands on the proxy for routing. The verdict on the claim (confirmed, contradicted, open) is written on the row, for the country and for any state or city claimed, and a project's two *location decisions* (what to do with a contradicted claim, what to do with an exit nothing can place) may turn it into ineligibility. Every observation feeds per-connector accuracy totals.

Nothing on the request path waits for a database: attribution is a memory-mapped lookup, and observations are buffered in memory, batched through Redis and written to Postgres by one leader instance.

## How the pieces fit together

Four flows, drawn separately because they run at different times. The first is the spine every other flow feeds.

### Sightings become attributed proxies

```mermaid
flowchart TD
    syncer["Provider syncer<br/>discovery response or vendor IP list<br/>(vendor-operated URL: the country is the claim)"] -->|exit_ip_observed| attributor
    hc["Health checker<br/>check response that echoes the IP"] -->|exit_ip_observed| attributor
    added["Static proxy added without a country"] -->|proxy_added| echo1["One echo request through the proxy"] --> attributor
    attributor["ProxyAttributor<br/>conflict rule = the project owning the connector"] --> resolve
    resolve["GeoService.apply_observation<br/>databases (memory-mapped), then echo country, then vendor claim<br/>→ country, source, location record, verdict per level"]
    resolve --> changed{"anything on the<br/>proxy changed?"}
    changed -->|yes| write["Write the proxy row<br/>publish proxy_changed to peers"]
    changed -->|no| nothing["nothing"]
    resolve --> moved{"different IP than<br/>recorded on the proxy?"}
    moved -->|yes| drop["publish exit_ip_changed<br/>preflight drops its cached verdict"]
    resolve --> obs["IpObservation, always"]
    obs --> buffer["In-memory buffer<br/>pushed every 5 s or when half full"]
    buffer --> redis[("Redis list<br/>geo:observations")]
    redis -->|leader, every 30 s| flusher["ObservationFlusher<br/>a hand-out = the proxy's exit differs from<br/>the last one counted (proxy:status exit_ip)"]
    flusher --> raw[("ip_observations<br/>raw, retention window")]
    flusher --> exits[("connector_exit_ips<br/>distinct exits, latest verdict,<br/>first/last seen, reuse")]
```

### A health check, every minute, per proxy

```mermaid
flowchart TD
    get["GET the check URL through the proxy<br/>connector's own URL, else the echo endpoint"] --> status["publish health_check_completed<br/>(status, latency: unchanged behaviour)"]
    status --> carries{"response carries<br/>the caller's IP?"}
    carries -->|"no: target-site URL, no ip path"| done["done"]
    carries -->|yes| sighting["publish exit_ip_observed<br/>(ip, echo country)"]
    sighting --> same{"same IP as discovered_ip<br/>and already attributed?"}
    same -->|yes| skip["skip: no lookup,<br/>no write, no observation"]
    same -->|"no: new exit, or first time"| apply["apply_observation<br/>country, location, conflict recomputed<br/>row written once, observation recorded once"]
```

So a proxy whose exit never moves costs one lookup and one write in its lifetime. A residential slot costs one per vendor session rotation.

### Preflight, per request, when the project turns it on

```mermaid
flowchart TD
    select["Select the upstream<br/>strategy, -cc-/-st-/-city- filter, row verdict filter"] --> bound{"exit bound to a<br/>vendor session?"}
    bound -->|"no: fixed exit"| fwd0["forward: discovery placed it,<br/>health checks re-read it"]
    bound -->|yes| expected["Expected location<br/>the -cc-, -st-, -city- values the client named,<br/>else, on a dynamic gateway, the rendered place"]
    expected -->|none| fwd1["forward: nothing asked (pooled slot)<br/>or promised (dynamic gateway)"]
    expected -->|some| cached{"verdict cached for<br/>(project, proxy)?"}
    cached -->|yes| reuse["reuse it<br/>preflight_session_ttl_seconds,<br/>dropped early if the exit moves"]
    cached -->|no| echo["Echo request through the proxy"]
    echo -->|fails| fwd2["forward: an echo outage<br/>never blocks traffic<br/>(failure cached 30 s)"]
    echo -->|ip| sighting["Pooled slot: the echo is a sighting of the slot<br/>exit_ip_observed; the row is re-judged on its own claim,<br/>replaced under refuse, its stale cache dropped<br/>(dynamic gateway: the sighting is recorded only)"]
    sighting --> resolve["Resolve for the request: databases + echo country<br/>(the expectation is left out); cache the verdict"]
    resolve -->|"match at every level"| fwd3["forward"]
    resolve -->|"open at some level"| openq{"location_unknown"}
    openq -->|"vendor;<br/>refuse when only the vendor rendered<br/>the open city or state and<br/>location_contradicted is not refuse"| fwd3
    openq -->|refuse| refusedopen["refused: not confirmed;<br/>the proxy is left alone"]
    resolve -->|"mismatch at any level"| level{"location_contradicted"}
    level -->|vendor| fwd4["forward anyway;<br/>the proxy is left alone"]
    level -->|"evidence: only the vendor's promise failed<br/>and the exit is inside the connector's countries"| fwd4
    level -->|"evidence: the client named the place<br/>or the exit is outside the list"| kept["refused: outside the connector's countries<br/>or mismatch; the slot is kept"]
    level -->|refuse| mismatch["refused: mismatch; a pooled slot<br/>was already replaced by its sighting"]
    refusedopen --> announce["publish exit_location_mismatch<br/>(every refusal, logged)"]
    kept --> announce
    mismatch --> announce
    announce --> allowed{"strategy allows moving<br/>this request?"}
    allowed -->|"no: sticky with a session"| r502b["502 Exit country/state/city mismatch,<br/>Exit country outside the connector's countries,<br/>or Exit country/state/city not confirmed"]
    allowed -->|yes| reselect["select again, excluding failed proxies<br/>502 after preflight_max_attempts"]
    reselect --> echo
```

### Databases, and what happens when they change

```mermaid
flowchart TD
    upload["Upload in the admin panel"] --> row
    download["Scheduled vendor download"] --> row
    path["Path in the config file<br/>(no row, read in place)"] --> reopen
    row[("geo_databases row + bytes<br/>in Postgres")] -->|geo_database_changed| reopen
    reopen["Every instance: fetch the bytes once per checksum<br/>into geo.cache_dir, memory-map, reopen"] --> reattr["The instance that made the change re-attributes<br/>every proxy with a known IP, offline;<br/>rows written only where the answer moved"]
```

The periodic full reload re-reads the settings row and the database list as a safety net for dropped events; it hashes nothing unless a checksum or a file's size or mtime moved. If the listing itself fails, the databases already open stay open until the next reload rather than being closed on a Postgres blip.

## How exit IPs are learned, per connector type

Attribution never makes requests of its own to find an IP. It listens for *sightings* from the code paths that already learn one, and only then looks the IP up. What produces sightings depends on the connector type:

| Connector / proxy type | How the exit IP is learned | Requests through the proxy | Vendor claim to verify |
|---|---|---|---|
| Residential and mobile sessions (Oxylabs, Bright Data, Decodo, IPRoyal, NetNut) | Never by provisioning: the vendor rotates the exit behind a session and the syncer does not probe it. The health check reports it every minute when the check URL echoes the caller (the attribution echo endpoint by default), and preflight reports it when a project turns preflight on. | Health check: one per proxy per `health_check_interval`, which happens anyway. Preflight: one per (project, proxy) per `preflight_session_ttl_seconds`, or sooner when a health check sees the exit move. | The country the slot was provisioned for, when the connector or a `-cc-` request geo-targeted it. Untargeted slots make no claim. |
| Residential and mobile with **dynamic sessions** (one gateway row, a vendor session per request) | Only through preflight. The gateway row is not health-checked, since no single exit speaks for a rotating pool. Every request renders its own vendor session, so an echo through that session, before the request, is the only way to see where the request went: with preflight on, a request carrying `-sessid-` is echoed once per derived session and cached; a request without one is echoed for the connector's `exit_sample_percent` of requests (default 5), uncached. | No health checks. Preflight: one per client session per `preflight_session_ttl_seconds`, plus the sampled share of session-less requests. | The `-cc-` country or, with a country allow-list on the connector, the country rendered for the request. Requests with no country make no claim and are only recorded. |
| Port-based with IP discovery (Oxylabs ISP and datacenter, Decodo ISP and datacenter) | The syncer requests the **vendor's own** discovery endpoint (named in the descriptor, e.g. `ip.oxylabs.io`, `ip.decodo.com`) through each port when the slot is created, and again on every IP refresh (`ip_refresh_interval`, hourly by default). The response IP and the vendor's country arrive as one sighting. | One discovery request per slot at creation, one per slot per refresh, plus the health check. | The discovery endpoint's country, since the descriptor marks the URL as vendor-operated (`discovery.vendor_operated`, the default). A descriptor pointing discovery at a third-party echo unsets it and the country becomes an `endpoint` candidate instead. With countries listed on the connector, the country the port was kept for. |
| Port-based with a known-IP API (Bright Data ISP and datacenter) | The vendor's IP list gives each slot its IP and country without any request through the proxy; the syncer announces both. Refresh re-reads the list. | Health check only. | The country from the vendor's list. |
| Proxy lists (Webshare) | The list gives host, port and country. The host is normally the exit, but nothing is announced until the first health check reports the caller's IP. | Health check only. | The country from the list. |
| Static connectors (manually added proxies) | One request through the proxy to the echo endpoint when the proxy is added without a country (`proxy.geo_lookup.enabled`), and on the Detect button. Then health checks. | One at creation, one per Detect, plus the health check. | A country set by hand is treated as the claim and kept for routing. |
| Cloud instances (AWS, GCP, Azure) | Health checks only; the instance's public address is the exit. | Health check only. | None. The connector's declared countries list is not a per-proxy claim. |

Two endpoints are therefore in play. Provider descriptors keep the **vendor's discovery endpoint** for port-based IP discovery, at slot creation and on each refresh, because it also returns the vendor's own view of the location. Everything else goes to the **attribution echo endpoint** (`echo_url` in the settings): the health check of every connector that names no check URL of its own, the static-proxy lookup, the Detect button and preflight. A port-based proxy is thus probed by the vendor endpoint hourly and by the echo endpoint every minute, which is the same number of requests as before attribution existed; only the destination of the health check changed from httpbin to the echo. Preflight is a project setting, not a per-connector one: with it on, a request that asks for a country, or that lands on a proxy with a promised or pinned country, is verified against the echo once per project and proxy per cache window, whatever the connector type.

Two consequences worth knowing. A health check sighting is applied only when the IP moved or the proxy was never attributed, so a stable proxy costs one database lookup and one write in its lifetime, not one per minute. And a proxy of a connector with declared countries that attribution places elsewhere gets that country on its row, and is then never routed to, whatever was asked and under every location decision: the declared list bounds every row, and covers on its own only proxies whose location is not known yet. It is skipped, never replaced: only `location_contradicted: refuse` spends a fresh session. The rule binds rows the vendor placed in a declared country; a row that made no claim is bound by the list only for a request that names a country.

The **Detect** button on a proxy and **Re-attribute proxies** on the settings page are the manual paths. Re-attribution is offline: it re-runs the databases over every proxy with a known IP and makes no request.

### What each step costs

| Step | When | Cost |
|---|---|---|
| Database lookup | Every sighting that passes the checks above, every preflight check, every re-attribution | Memory-mapped read, microseconds, no I/O |
| Proxy write to Postgres | Only when the resolved country, source, location record, conflict flag or discovered IP changed | One row, then one `proxy_changed` event to peers |
| Observation | Every applied sighting and every preflight check (an echo through a pooled slot is applied as the slot's sighting and recorded once); deduplicated health checks and repeat preflights of an unchanged exit record nothing | Appended to an in-memory buffer; pushed to Redis every `publish_interval_seconds` or as soon as the buffer is half full; written by the leader every `flush_interval_seconds`, which also maintains the accuracy and exit IP aggregates. Each cycle writes batches of 2000, one transaction each, for as long as they come back full (up to 20 s per cycle), so a burst is drained instead of piling up; the IP attribution page shows how many are still queued |
| Echo request | Static proxy creation, Detect, preflight (per cache window), and health checks whose URL is the echo endpoint | One HTTP request through the proxy |

## Databases

Any file in MaxMind's mmdb format works, from any vendor. Octoprox reads the file's own metadata to recognise the vendor and normalises each vendor's record layout into one shape (country, region, city, coordinates, ASN, organisation, anonymity flags). IP2Location's BIN format is also supported when the optional `IP2Location` Python package is installed.

| Vendor | Files | Free edition | Notes |
|--------|-------|--------------|-------|
| MaxMind | GeoIP2 / GeoLite2 Country, City, ASN, Anonymous IP (mmdb) | GeoLite2 with a free account and license key | Attribution text required for GeoLite2; redistribution is not permitted, so every install brings its own file |
| DB-IP | Country, City, ASN (mmdb) | Lite editions, CC BY 4.0 | Same layout as MaxMind |
| IPinfo | Country + ASN, Location, Privacy (mmdb) | Country + ASN with a free token | Flat record layout, ASN as `AS12345` |
| IP2Location | DB1 to DB26 (BIN, and mmdb for DB1/DB9), IP2Proxy (BIN) | LITE editions | BIN needs `pip install IP2Location` |
| GeoNames | `cities500`, `cities1000`, `cities5000`, `cities15000`, one file per country (`BR.zip`), zipped or bare `.txt` | Free, CC BY 4.0 | Answers no IP: city coordinates for the [city radius](#city-coordinates-and-the-city-radius) |

Databases reach an instance three ways:

* **Upload** in Settings → IP attribution. The file is validated by opening it, stored in Postgres, and every instance copies it to its `geo.cache_dir` and memory-maps it. Peers learn about it over the cross-instance event feed.
* **Scheduled download** from the vendor. Register the vendor's download URL with your own credentials (MaxMind account ID and license key as basic auth, an IPinfo token in the URL). The leader instance downloads it now and again every `update_interval_hours`, unpacks `.tar.gz` and `.gz` containers, and publishes the new file only when its checksum changed. A URL may carry `{YYYY}` and `{MM}`, filled in for the current month on each run, for vendors such as DB-IP that name their files by month and offer no fixed URL; when the current month's file is not published yet the previous month's is fetched instead.
* **Config file** for installs that already run `geoipupdate` on a shared volume: list the paths under `geo.databases`. These are never copied and cannot be removed from the UI.

Databases are consulted in **priority** order, lowest first. The first database with a country answers; the others fill in fields it lacked (a country database plus an ASN database give one merged record). Uploading, enabling, disabling or removing a database re-attributes every proxy with a known exit IP, offline.

**Which files to load.** Load one geolocation database per vendor. A city file already contains everything the same vendor's country file does, so never add both: choose the country file when you only route by country and want the smaller footprint, or the city file when you route or verify below the country. [State and city routing]({{ site.baseurl }}/routing-strategies#state-and-city-routing) matches fixed exits on the state code and city the databases record, and preflight can only judge a state or city claim with a city-level database loaded, so any project that targets `-st-` or `-city-` wants the city file. Adding a second vendor's geolocation file is worthwhile: the resolver treats each database as an independent opinion, and answers the databases disagree on are left open rather than confirmed (see the conflict rule below). ASN and anonymity files sit alongside any of these and only fill in fields the geolocation file lacks.

Free editions carry a license attribution; the settings page shows it while such a database is loaded.

### City coordinates and the city radius

IP databases name the city they place an exit in, and a city claim names the place that was promised; the two often name neighbouring places. DB-IP puts much of Greater São Paulo in Diadema, a separate municipality 15 km from the centre, and some exits in São Paulo's own districts (Vila Olímpia). The record carries one city name, one state name and one coordinate, with no parent city or metro area, so there is nothing to roll Diadema up to São Paulo by. Compared by name, every one of those exits contradicts a `-city-sao_paulo` claim.

A GeoNames file gives the claimed city a point, and an independent answer within the **city radius** of that point confirms the claim. Octoprox ships no such file: load one like any database, by upload or by scheduled download (the **GeoNames cities500** preset is `https://download.geonames.org/export/dump/cities500.zip`). It shows in the database list as *City coordinates* and answers no IP, so its priority only orders it against other GeoNames files: the first one that knows a city places it.

**Which file.** The number is a population cutoff, and each file holds every place of the next one up. `cities500` (every place over 500 people and every administrative seat down to the fourth level, about 236k places) covers essentially any town a vendor lets you target, and takes about 35 MB of memory and a couple of seconds to load on each instance. `cities15000` (34k places, about 5 MB in memory) misses more than half of Brazil's municipalities. A per-country file such as `BR.zip` holds every populated place of one country. Only populated places are read, and a file with more than 1.5 million of them (`allCountries`) is refused.

**How a city is judged.** Only the city level is lenient; the country and state are still compared exactly.

* The claimed city (a `-city-` request, a vendor-listed city, a pin) is placed by the GeoNames file, by country and name, unless a database answering for the same IP named it too (next point). Several places of that name in one country resolve to the one in the claimed state when GeoNames uses the same code (US states, the UK's nations), else to the most populous.
* A database answer is placed at the database's own coordinates. Any other answer (the claim, the echo endpoint's city, a database answer without coordinates) is placed where a database answering for the same IP put a city of that name, since every answer is about one exit: when DB-IP says Camden and the echo says `camden`, both sit at DB-IP's Camden point, 3.8 km from London. Only a name no database reported for that IP is looked up in the GeoNames file.
* Two cities agree when their names are the same or their points are within the radius. The conflict rule then works as it always does: under `consensus` the claim is confirmed when every independent answer is within the radius, so DB-IP's Diadema and MaxMind's Guarulhos together confirm São Paulo, while answers on either side of the radius leave it open.
* The claim falls back to comparing names when it cannot be placed (no database named it and the file does not know it), when no GeoNames file is loaded, or when the radius is 0.

The radius is an install setting (Settings → IP attribution → Policy & echo, default 25 km), and each project may set its own on its Location tab, for instance a smaller one to be stricter. 25 km takes in the municipalities around a large city (Diadema, Guarulhos and Osasco are about 15 km from São Paulo's centre) without reaching the next real one (Campinas is 84 km away). The **Test an IP** box on the settings page takes a claimed city and shows the verdict and the distance.

A city confirmed within the radius keeps the database's answer as what was observed and resolved: the observation log shows `diadema` against the claim `sao_paulo`, confirmed. The row is routed on the claim it confirmed, so a fixed exit attributed to Diadema still serves `-city-sao_paulo`. The radius judges claims only: a row that made no city claim is routed on its database city, and a `-city-sao_paulo` request does not reach a row whose only city is `diadema`. Adding, replacing or removing a GeoNames file, and changing the install radius or a project's, re-judges the affected rows offline, as for any database.

## Sources and the conflict rule

Three kinds of evidence exist for an exit IP:

* **database**: the loaded IP databases;
* **vendor**: what the proxy vendor claimed (a country in its proxy list or known-IP API, or the country a slot was provisioned for);
* **endpoint**: what the echo endpoint reported when requested through the proxy, for endpoints that know geography.

The value routing reads has one fixed order: the databases, in their configured priority, then the echo endpoint, then the vendor, which fills only what nothing independent answered. There is no setting for it. The first kind with an answer decides the proxy's country, state and city, and the vendor's claim is judged against the independent kinds whether or not it decided. Two settings bend this, each in one case. Under the conflict rule `any` (below) a claim confirmed by a lower-ranked source is routed on that source's answer, so a confirmed row is never routed on a country its verdict disagrees with. Under the project's `location_contradicted: vendor` a contradicted level carries the vendor's claim as its routing value: that is what trusting the vendor means, and the independent answer is kept beside it as what was observed. Otherwise trust in the vendor is the two location decisions' business, never the value's: a project that serves an open claim serves it on the vendor's word, and `location_contradicted: evidence` serves a contradicted exit where the evidence puts it.

The install has a default conflict rule (Settings → IP attribution → Policy & echo). Each project may override it on its Location tab; a proxy is always judged under the rule of the project that owns its connector. None of the kinds costs a request of its own: vendor claims arrive with the vendor's proxy list or slot provisioning, and the echo endpoint is the same request discovery and health checks already make to learn the exit IP.

**Conflict rule** says when a vendor claim counts as contradicted:

* `consensus` (default): every independent source agrees with each other and all of them disagree with the vendor. Databases lag on residential ranges, so a single dissenting database is not evidence against a vendor; independent sources that disagree among themselves leave the claim *open* instead: no verdict, so the vendor is neither cleared nor blamed. A claim no independent source can judge at all, an IP no loaded database knows, is open for the same reason. An open claim changes nothing about the value: the row keeps the country the top-ranked source resolved, and only `location_unknown: refuse` treats an open claim as a reason not to route there.
* `first`: the top-ranked independent source disagrees with the vendor. The value is the top-ranked answer, as under `consensus`.
* `any`: no independent source agrees with the vendor. One that does, a database at any priority or the echo endpoint, confirms the claim, and the row is then routed on that source's answer rather than the top-ranked one, so a confirmed row is never routed on a country its verdict disagrees with. With no source agreeing the claim is a plain contradiction routed on the top-ranked answer. `any` never leaves a claim open while there is evidence; with no evidence at all it is open like the others.

A missing vendor claim never conflicts. A country set by hand on a proxy stays the routing country and is treated as the claim to verify.

## Per-project policy

Each project makes two decisions about exits whose location is in doubt (project settings → Location). Both apply to every kind of row: to the verdict written on a fixed exit or pooled slot by health checks, discovery, locate and re-attribution, and to the verdict preflight produces for a request. A confirmed claim, and a row not judged yet, serve under every setting. [Location routing]({{ site.baseurl }}/location-routing) walks the combinations of these settings with the routing strategy and the kind of proxy row, with examples.

| Setting | Values | Effect |
|---------|--------|--------|
| `location_contradicted` | `evidence`, `vendor`, `refuse` | What to do when the independent sources (databases, echo endpoint) agree against the vendor's claim. `evidence` (default) routes the proxy on where they put the exit: the row serves on its resolved country, the contradiction is shown and counted, a pooled slot is kept; a contradicted state or city only the vendor supplied is skipped, since it cannot be routed on the evidence, while a contradicted pin stays, shown as contradicted, since the operator placed it deliberately and attribution never overwrites a pin. `vendor` routes on the claim anyway and only shows the contradiction: the contradicted level carries the vendor's claim as its routing value, a contradicted pin stays, and preflight forwards a contradicted exit. `refuse` holds the vendor to its word: a contradicted pooled session slot is removed and replaced by the provider sync, a contradicted fixed exit is skipped, a contradicted exit at preflight refuses the request and removes a pooled slot. At preflight, when the client named the place, `evidence` and `refuse` both refuse a contradicted exit; when only the vendor promised it, `refuse` holds the vendor to its exact promise, `evidence` holds the exit to the connector's declared countries, `vendor` checks nothing but placement. |
| `location_unknown` | `vendor`, `refuse` | What to do when nothing independent places the exit: no loaded database knows the IP, or the databases disagree with each other under `consensus`, so the claim is open. `vendor` (default) routes on the vendor's word. `refuse` skips the row until a later sighting places it, and so a judged row with no country at all or with a country only the vendor supplied; a state or city only the vendor supplied is skipped for a `-st-` or `-city-` request; at preflight an open level the client named, or a promised country no source can place, is refused with `502 Exit country not confirmed`. A skipped slot is kept, not replaced. A country pinned by hand is the operator's word and never open: trusted under both values. Dynamic-sessions gateways carry no row verdict, so both decisions reach them only through preflight. |
| `location_preflight` | `true`, `false` | Verify a session's exit before its first request is forwarded (below). What the verdict does to the request is the two decisions' call. Not implied by them: must be on for a refusing decision to reach dynamic-sessions connectors, and to check a pooled residential or mobile session per request rather than per health check. |
| `location_conflict_rule` | `consensus`, `first`, `any`, or null | When a vendor claim counts as contradicted; null inherits the install default. |
| `location_city_radius_km` | 0 to 500, or null | How far from the claimed city an independent answer may place the exit and still confirm a city claim, once a GeoNames file is loaded; 0 compares names only, null inherits the install default (25 km). See [City coordinates and the city radius](#city-coordinates-and-the-city-radius). |

## Preflight

A residential or mobile exit belongs to the session, not to the proxy row, so verifying it means checking the proxy the request was actually routed to. With preflight on, Octoprox makes one echo request through the selected upstream, attributes the IP, and compares it with the location the request *requires*: the `-cc-`, `-st-` and `-city-` values the client named, or, on a dynamic gateway serving a request that named nothing, the place rendered into the vendor session. A client that named nothing has no expectation of a pooled slot: the slot is not preflighted for it, and its own claim is judged on the row by every sighting of its exit.

Only rows whose exit the vendor can move are checked: dynamic-sessions gateways and pooled session slots. A fixed exit (static, ISP, datacenter, list-mode) was placed by discovery and is re-read by every health check that reports its IP, so an echo per request would only repeat what attribution already knows; those rows are never preflighted, whatever was asked or promised.

Every level is judged the same way, under the project's conflict rule: the expected place is the claim, the databases and the echo endpoint are the evidence, and the level is contradicted, confirmed, or left open when nothing independent answered there or the independent sources disagreed under `consensus`. The echo endpoint contributes below the country only when `echo_state_path` and `echo_city_path` are set (Octoprox's own `/echo` answers `state_code` and `city`), and a country-only database never answers there. What a contradicted or open level does to the request is the two location decisions' call, below.

The verdict is cached in Redis per project and proxy for `preflight_session_ttl_seconds` (default 600), so a session pays one extra round trip and the rest of its requests pay nothing. That rests on the assumption that a vendor session keeps its exit for about that long. Vendors do not always oblige: a sticky window may be shorter, or the exit may be replaced after an upstream failure. So the cache is also dropped the moment a health check or an IP refresh sees the proxy exiting from a different address (the `exit_ip_changed` signal), and the next request through that proxy is verified again. The window during which a rotated exit is trusted unverified is therefore at most one `health_check_interval`, not the whole TTL. The reverse holds too: a preflight echo through a pooled slot is itself a sighting of the slot, applied to the row before the verdict is cached, so an exit that moved is re-judged by the first request that sees it, not only by the next health check. Lower the TTL for a tighter bound at the cost of one echo request per proxy per TTL.

Preflight is a switch of its own and is not implied by the decisions. With it off, a refusing decision reaches pooled residential and mobile slots only through the row verdict the health check writes, and does not reach dynamic-sessions connectors at all, so a project with such connectors that wants either decision to hold per request turns preflight on. What the verdict does to the request is the project's two location decisions, the same ones that act on the verdict written on a row. Who named the place matters: the client with `-cc-`, `-st-` or `-city-`, the vendor through the slot's provisioning or the country and city a dynamic request rendered, or an operator through a pin.

* `location_contradicted: refuse` refuses a contradicted exit, a mismatch at any level, naming that level: `Exit country mismatch: requested US, observed GB (…)`, `Exit city mismatch: requested GB, city manchester, observed GB, city london (…)`. It holds the vendor to its exact promise when the client named nothing, including a city the vendor rendered into a dynamic request, and it removes a pooled slot whose exit it refused.
* `location_contradicted: evidence` refuses a contradicted exit when the client named the place, with the same `mismatch` messages, since a request that asked for one place cannot be routed on the evidence that puts the exit in another. When the client named nothing, the exit is held to the connector's declared countries rather than to the vendor's exact promise: a placed exit anywhere in the list is forwarded (anywhere at all when the connector lists none), one outside is refused with `Exit country outside the connector's countries: requested US, observed GB (…)`, and a city or state the vendor rendered is not checked. The slot is kept.
* `location_contradicted: vendor` forwards a contradicted exit whoever named the place, and records the contradiction. It checks nothing about the place; only placement, below, can refuse under it.
* `location_unknown: refuse` refuses an exit nothing independent could place, at every level the client named: `Exit country not confirmed: requested US, observed unknown (…)` (or `observed FR` when the independent sources disagreed with each other under `consensus`), `Exit city not confirmed: requested GB, city london, observed GB (…)`, `Exit state not confirmed: requested GB, state ENG, observed GB (…)`, until a later sighting settles the claim. A country the vendor promised is refused when open under every `location_contradicted` value; a city or state the vendor rendered into a dynamic request is refused when open only together with `location_contradicted: refuse`. With no city-level database loaded, a `-city-` or `-st-` request is therefore refused under `location_unknown: refuse`. The slot is kept.
* `location_unknown: vendor` forwards an open level on the vendor's word.

A country pinned by hand is judged on the row, never at preflight. The pair `vendor` + `vendor` refuses nothing at preflight: it records.

Whether a refused request moves to another proxy is the routing strategy's call, not a setting of its own. Under `sticky` a request with a session was promised one exit, so a refusal answers `502 Bad Gateway` at once; the other strategies re-select among the project's remaining eligible proxies, up to `preflight_max_attempts` proxies in all (install-wide, default 3, counting the first), then answer 502. `preflight_max_attempts: 1` fails fast on every strategy.

The echo also acts on the proxy, whichever way the request went: for a pooled vendor-session slot (residential, mobile) it is applied to the row as a sighting, the slot's own claim is judged under the project's decisions, and the row's location and verdict are updated on the spot. Under `location_contradicted: refuse` a contradicted slot is rotated: removed, so the provider sync provisions a fresh session in its place. Under `evidence`, and for an open exit under `location_unknown: refuse`, the slot is not removed: the re-judged row routes it on where the exit is, or keeps it out while nothing places it, from the next selection on. A dynamic gateway row is left alone either way: the misplaced session was that request's, and the next request mints another.

An unreachable echo endpoint never blocks traffic, under any setting. An IP no source knows is forwarded under `location_unknown: vendor` and refused under `refuse`. Rotating pools without a session id cannot be gated this way; their observations still feed provider accuracy.

**Dynamic sessions.** A connector running a residential or mobile product with dynamic sessions has one gateway row and no slot per exit, so the verdict cache is keyed per project, gateway row, vendor session *and* expected country: a client that keeps its `-sessid-` but changes `-cc-` is verified again. A request with `-sessid-` derives its vendor session deterministically and is verified once per session per TTL, like a slot. A request without one gets a fresh vendor session that nothing else will ever use, so it is echoed only when sampled (the connector's `exit_sample_percent`, `0` never, `100` every one), the verdict is not cached, and when the request named no country there is nothing to verify: the sighting is recorded for accuracy and unique exits and the request forwarded. Sampling thins observation only. Under every pair of decisions but `vendor` + `vendor`, a session-less request that asks for a country is verified every time regardless of the percentage, at one echo per such request; a sampled guarantee would be none. One that asks for nothing and lands on a connector with an allow-list is verified every time only when the project refuses contradictions or unknown exits; under `evidence` with trusted unknowns the list is checked on the sampled requests alone: a sampled exit outside the list is refused, an unsampled one goes unseen, and `exit_sample_percent` at 100 makes the list a full guarantee. `location_contradicted: evidence` reads a promised country as the connector's declared countries: a placed exit anywhere in the list passes, one outside is refused, and one nobody can place is refused when `location_unknown` is `refuse`. Under `vendor` + `vendor`, which can refuse nothing, the percentage applies to every session-less request. Preflight is therefore the *only* source of exit sightings for dynamic traffic; with preflight off, the connector has no accuracy or unique-exit data at all (the gateway is not health-checked). Sampling below 100 undercounts, and the shortfall cannot be corrected afterwards: an exit handed out once has only the sampled chance of being seen at all, and a repeat is detected only when both hand-outs were sampled, so distinct exits and the reuse share for session-less traffic are both low. The accuracy *rate* is unaffected, being a proportion of a random sample. `GET /geo/exits` and `GET /geo/accuracy` therefore return a `coverage` object for dynamic connectors (`preflight_on`, `sampled_percent`), and the Exit locations views and the connector inspector mark their unique-exit figures "sampled N%" or "preflight off". The percentage is a connector setting (**Exit sampling** on a dynamic connector), since the cost lands on that connector's vendor bill and one pool may carry most of the session-less traffic; set it to 100 for a complete count at the price of one echo per session-less request. A mismatch on a dynamic request is judged like any other for that request, under the project's two decisions, but does nothing to the gateway row; when the strategy allows a move, a session-less request is retried on the same gateway, since selecting it again mints a fresh vendor session, while a request with a client session moves to another connector, or fails under `sticky` or when there is no other connector. The row itself is left alone: the misplaced session was that request's alone, and the next request mints another. Nothing is ever written on the gateway row by attribution, so the decisions reach a dynamic connector only through preflight; with preflight off they do nothing to dynamic traffic.

## Provider accuracy

Every observation records what the vendor claimed and what attribution resolved, and the exit IP table keeps the latest of those per distinct exit. Provider accuracy is computed from that table: of a connector's distinct exits last seen in the window, how many carried a vendor claim, and how many of those the latest verdict confirms, contradicts or leaves open (no independent answer, or independent sources that disagree). The same is kept one level down. A claim below the country is a city or state the vendor's list or discovery endpoint named for an exit, the place a request asked for with `-st-` or `-city-`, or one an operator pinned on a static proxy; it is judged by the same conflict rule as the country, against the databases and the echo endpoint, and the observation records the claim, what was resolved at that level, and a verdict with the polarity of the country's (`state_conflict`, `city_conflict`: true contradicted, false confirmed, null when nothing was claimed, nothing independent answered, or the independent sources disagreed under `consensus`). The same verdict is written on the proxy row as `state_verdict` and `city_verdict`. Routing on a fixed exit reads the resolved state and city, so the vendor's city stands where no database or echo knows one, exactly as for the country; `location_unknown: refuse` then skips the row for a `-city-` request until a database places it. Cities are compared as slugs: the databases' district entries (`Sofia (g.k. Banishora)`, `Sofia (Old City Center)`, `London (Soho)`: DB-IP has tens per city) lose the parenthesised part and count as their city, and a few spellings of one place are folded together (`City of London` is `london`). With a GeoNames file loaded, a city within the project's radius of the claimed one also confirms it (see [City coordinates and the city radius](#city-coordinates-and-the-city-radius)). After such a rule changes, **Re-attribute** on the IP attribution settings page re-judges every exit under it; the observation log keeps what was judged at the time. The accuracy table shows a country rate, a state rate and a city rate; the two lower rates are confirmed over the exits with a verdict, so a missing city-level database shows as *open* claims rather than as a bad vendor. Opening a connector's row shows the judgement per level and every contradicted pair, filterable by level and by place. The observation log and the exit IP table show the country verdict as the row's badge, since an exit right at the country is usable at the country whatever its city turned out to be, and the state and city verdicts as their own lines under it ("city contradicted"); the verdict filter is the country verdict. Each IP counts once however often it was seen, so a restart, an hourly refresh or a re-attribution does not inflate the numbers, and a re-attribution after a database change rewrites the verdicts rather than adding to a frozen history. For a residential pool that hands out a fresh IP per session this is one judgement per exit; for pools that reuse exits a wrong IP counts once, not once per hand-out. Every user sees it for the current project under **Exit locations** in the project menu; admins see all projects, with a project filter, under Settings → IP attribution. The connector inspector shows the connector's own numbers. For a dynamic-sessions connector the unique-exit figures carry a "sampled N%" or "preflight off" mark, since only preflight sees that traffic (see [Preflight](#preflight)).

Rates below 100% are normal for residential pools whose ranges databases have not caught up with. A rate that keeps falling, or one vendor far below the others, is a vendor problem.

## Unique exits per connector

Every observation also feeds `connector_exit_ips`: one row per connector and distinct exit IP, with when it was first and last seen, how many times it was newly handed out, and the state of its latest observation: the proxy that held it, how it was seen, what the vendor claimed, what attribution resolved and the verdict. That state is stored on the row so the view is one indexed table rather than a join into the raw history, and it is refreshed by every observation, including re-attribution and preflight, while only hand-outs move the count. The flusher upserts it in the same batch as the accuracy aggregates, and it is kept until `exit_ip_retention_days` after the IP was last seen (forever by default), so "how many distinct exits has this pool ever given us" survives the raw observation window. Both aggregates are deleted with their connector; the raw observations are not, so what a removed connector did stays visible until retention.

Exit locations has three tabs built on this. **Provider accuracy** shows, per connector, the distinct exits first seen in the window and ever, and the share of IPs handed out more than once. **Exit IPs** lists the distinct exits themselves with their latest state, one row per connector and IP, filtered and paged on the server. **Observation log** is the raw history, one row per sighting, which grows with every restart and IP change and follows the observation retention. Admins see the same three under Settings → IP attribution. `GET /geo/exits` returns the summary and `GET /geo/exits/ips` the paged exit list.

`sightings` counts hand-outs, and the flusher alone decides what is one. It keeps the last exit it counted for each proxy in the proxy's status hash in Redis (`proxy:status:<id>`, field `exit_ip`, the same hash that holds the health fields and goes when the proxy goes) and walks each batch in time order: a sighting is a hand-out when the proxy's exit differs from that recorded one, or none is recorded yet. Since every instance's sightings pass through the one leader in order, two instances reporting the same exit for a proxy, seconds or minutes apart, count once and both raw rows are kept. Detect and preflight count only when they find a different exit. Unchanged health checks are skipped before they become observations. Re-attribution is not a sighting at all: nothing looked at the exit, the IP already on the proxy was resolved again. It travels the same list as a judgement that the flusher applies to the exit row's claim, resolution and verdict only, so it writes no log row, counts nothing and leaves first and last seen where they were. The Exit IPs view shows such a verdict as "via reattribute". When Redis holds no exit for a proxy yet, after an upgrade or a Redis loss, the flusher asks the exit table instead: an exit already on record with that proxy as its latest holder was counted before and is not counted again. That lookup happens inside the flusher's own transaction, so nothing runs ahead of it. So the number is how many times the connector gave that IP to a proxy, not how many minutes it was in use. A residential pool that keeps returning the same few thousand exits shows a high reuse share; one drawing on millions shows a low one.

## Echo endpoint

Every Octoprox instance serves `GET /echo`, unauthenticated, returning the caller's IP as JSON plus the attribution from the loaded databases:

```json
{"ip": "203.0.113.7", "country": "GB", "city": "London", "asn": 12345, "organization": "Example Ltd", "databases": ["…"], "timestamp": "…"}
```

Requested through a proxy it returns the proxy's exit. The settings' `echo_url` is what the static-proxy lookup, the Detect button, preflight and, when a connector names no check URL of its own, the health checker request. The fresh-install default is `https://httpbin.org/ip`, which returns the IP and nothing else to every caller; set `echo_url` to a publicly reachable Octoprox instance and those requests stop depending on a third party. Provider descriptors keep their vendor-specific discovery endpoints for port-based IP discovery, since those also return the vendor's own view of the location.

The request travels out through the vendor's proxy and back in from the public internet, so the endpoint must be reachable from there. Installs on a private network run the standalone echo service on a public host instead:

```bash
OCTOPROX_ECHO_DATABASES=/data/GeoLite2-City.mmdb octoprox-echo   # same image, listens on :8090
```

Behind a load balancer the peer address is the balancer's, so list its addresses in `geo.echo.trusted_proxies` (or `OCTOPROX_ECHO_TRUSTED_PROXIES` for the standalone service) and the client IP is read from `X-Forwarded-For`. The bundled nginx config sets that header on the API listener.

## Health checks

Connectors that name no check URL are checked against the echo endpoint, so every check already sees the exit IP; Octoprox reads it and re-attributes the proxy when the IP changed, with no extra request. A dynamic-sessions gateway row is the exception: it is not health-checked at all, and any sighting of it (Detect, preflight) is recorded but never written on the row, whose location would otherwise mean nothing. Connectors whose check URL is a target site rather than an echo endpoint report no IP. For a custom echo-style URL set `healthcheck_ip_path` (and optionally `healthcheck_country_path`) in the connector config to the JMESPath of the address in the response, or `@text` for a plain-text body; the httpbin `origin` layout is recognised without configuration.

## What is stored on a proxy

| Metadata key | Meaning |
|--------------|---------|
| `country` | The resolved country routing reads |
| `country_source` | `database`, `vendor`, `endpoint` or `manual` |
| `vendor_country`, `vendor_state`, `vendor_city` | What the vendor claimed at each level: its proxy list, known-IP API or own discovery endpoint |
| `endpoint_country`, `endpoint_state`, `endpoint_city` | What a third-party discovery endpoint or the echo reported; a vendor-operated discovery URL writes the `vendor_*` keys instead |
| `state_code`, `city` | The resolved state and city routing reads for `-st-` and `-city-`, with `state_source` and `city_source` |
| `location` | Full record from the databases: the first-level subdivision as `region` (its name) and `state_code` (its ISO 3166-2 part), city, coordinates, ASN, anonymity flags |
| `manual_location` | A state code and city an operator pinned by hand on a static proxy; routing matches on them ahead of `location` |
| `location_conflict` | The vendor's country claim is contradicted under the conflict rule |
| `location_verdict` | How the country claim fared: `confirmed`, `contradicted` or `open`; absent when nothing was claimed |
| `state_verdict`, `city_verdict` | The same for a claimed state or city; read for `-st-` and `-city-` requests |
| `location_checked_at` | When the row was last judged; a row without it has not been judged yet and serves under every setting |
| `location_candidates` | Each source's answer, for the inspector |

`country`, `location.state_code` and `location.city` (or the manual pin) take part in routing: `-cc-` matches the country, `-st-` the state code and `-city-` the city as a slug. The rest is shown in the inspector.

## Backup and restore

The admin backup (Settings > Backup) always carries the attribution settings
row and the database records. The two optional boxes decide the rest:

| Backup option | What it adds |
|---------------|--------------|
| Include history | Raw exit IP observations and the per-connector exit IPs with their latest verdicts, alongside proxy and project metrics. |
| Include IP database files | The bytes of every uploaded or downloaded database. Off by default because a city database is tens of megabytes. |

A restore without the files keeps the records, so the databases list shows
each one without a file and with a load error until it is re-uploaded, or
refreshed when it has an update URL. Operator-managed files from the config
are not part of the backup at all; they live on disk next to the instance.

## Configuration

```yaml
geo:
  cache_dir: data/geo                 # where this instance caches stored database files
  databases:                          # operator-managed files (optional)
    - path: /etc/octoprox/GeoLite2-City.mmdb
      priority: 10
  defaults:                           # settings of a fresh install; the admin panel edits the live row
    default_conflict_rule: consensus
    default_city_radius_km: 25
    echo_url: https://proxy.example.com/echo
    preflight_max_attempts: 3
  echo:
    enabled: true
    trusted_proxies: [10.0.0.0/8]
  observations:
    publish_interval_seconds: 5
    flush_interval_seconds: 30
    max_buffer: 5000
  updater:
    check_interval_seconds: 3600
```

The older `proxy.geo_lookup` settings still seed the echo endpoint of a fresh install, so existing installs keep their endpoint until an admin saves the settings.

## API

See the [API reference]({{ site.baseurl }}/api#ip-attribution) for the `/api/v1/geo` endpoints: settings, databases, lookup, re-attribution, observations, accuracy and status.
