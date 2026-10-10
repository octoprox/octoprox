---
layout: docs
title: "Proxy Routing Strategies"
nav_id: routing-strategies
description: "Choose how Octoprox distributes requests across proxy pools: round robin, least used, random, sticky sessions and health based, combined with connector weights."
---

# Routing Strategies

<p class="subtitle">Distribute requests across proxy pools using different routing strategies.</p>

Octoprox supports multiple routing strategies for distributing requests across proxy pools:

| Strategy | Description |
|----------|-------------|
| `round_robin` | Distributes requests evenly across all healthy proxies in order |
| `least_used` | Routes to the proxy with the fewest active connections |
| `random` | Randomly selects a healthy proxy for each request |
| `sticky` | Routes requests carrying the same `-sessid-` to the same proxy; requests without one are routed like `random` |
| `health_based` | Prioritizes proxies with better health scores and lower latency |

Every strategy picks in two steps when a project has more than one connector: first a **connector**, by its [weight](#connector-weights), then a proxy inside that connector, by the strategy. A project with a single connector skips the first step.

## Connector weights

Each connector has a **Weight** on its Routing tab (API: `routing_config.weight`, an integer from 1 to 100, default 1). It is the connector's share of the project's traffic relative to the other connectors that can serve the request. Weights are ratios, not percentages: 1 and 3 split requests 25/75, and 2 and 6 do the same.

The share does **not** depend on how many proxies a connector holds. Before weights, a strategy saw one flat list of proxies, so a pool of ten sessions took ten times the traffic of a connector with one row. That made a [dynamic sessions]({{ site.baseurl }}/providers#descriptor-reference) connector, which has exactly one gateway row but unlimited exits, almost invisible next to any pool. With weights, one gateway row and a fifty-slot pool split evenly at equal weights, and you move the split by changing one number.

### How a request is placed

1. The project's connectors are narrowed to those that can serve the request: enabled, at least one healthy and non-quarantined proxy, target domain allowed by the connector's [domain filter]({{ site.baseurl }}/domain-filtering), and the requested country served (see [country routing](#country-routing)).
2. One of them is picked in proportion to weight. A connector that dropped out in step 1 takes no share, and the remaining connectors split its traffic by their own ratios: with weights 1, 1 and 2, the connector at 2 takes 50%, but if a 1 has no healthy proxy it takes 67%.
3. The project's strategy picks a proxy inside that connector as it always did.

How the strategy performs step 2:

| Strategy | Connector pick | Then, inside the connector |
|----------|----------------|----------------------------|
| `random` | Weighted random draw | Random proxy |
| `round_robin` | Smooth weighted round robin: weights 3 and 1 give the fixed order A A B A, three A for every B, spread out rather than clumped | Proxies cycled in order; each connector keeps its own position |
| `least_used` | Lowest requests divided by weight, so weight reads as relative capacity | Proxy with the fewest requests |
| `sticky` | The session id is hashed onto a connector, weighted, so a session always lands on the same connector | The usual sticky binding |
| `health_based` | Weighted random draw; weight is not adjusted for health | Healthiest proxies preferred |

Under `sticky`, a session already bound to a proxy keeps it while that proxy is eligible, whatever the weights say. Weight changes steer new sessions only, and raising one weight moves a proportional slice of new sessions rather than reshuffling them all.

### Examples

**One pool plus a dynamic residential connector.** A datacenter pool with 20 IPs at weight 1 and an Oxylabs residential connector with dynamic sessions at weight 1: half the requests go to the pool (each IP sees 2.5% of the total), half to Oxylabs, each opening a fresh vendor session or reusing the client's. Set Oxylabs to 3 and it takes 75%.

**Two residential vendors.** Bright Data at weight 7 and Decodo at weight 3, both dynamic: 70/30, regardless of their slot counts. Under `sticky`, a given `-sessid-` always reaches the same vendor, so the vendor session stays stable.

**Cheap first, expensive as a slice.** ISP proxies at weight 8, a residential pool at 1 and a dynamic mobile connector at 1: 80/10/10. A request with `-cc-de` for a country the ISP connector has no IPs in skips it, and the other two split that request 50/50.

**Two pools of different sizes.** Pools of 10 and 30 slots at the default weight split 50/50. Before weights they split 25/75 by row count; set weights 1 and 3 to keep that.

### Seeing the split

The **Traffic split** panel on the Overview and Connectors pages shows, for each connector, its weight, the expected share of untargeted requests under the current weights and health, and the observed share over the selected window, with a sentence explaining the current setup. Connectors that take no traffic say why (disabled, no healthy proxy). The connector list shows the weight and expected share next to each connector, and the Weight field in the connector editor previews the share while you type. The same numbers are available from `GET /api/v1/projects/{project_id}/metrics/traffic-split`, see the [API reference]({{ site.baseurl }}/api#traffic-split).

Requests that carry a country or hit a filtered domain see a narrower set of connectors, so the observed split can differ from the expected one. Observed counts come from the connector's own metrics history, so they survive proxy rotation. A connector blocked by its [traffic limit]({{ site.baseurl }}/traffic-limits) is listed as *over traffic limit* and its weight is shared by the others, like a disabled connector's; with a price per GB the panel also shows what each connector's traffic in the window cost.

## Session IDs

When using the `sticky` routing strategy, you can control session affinity by embedding a session ID in the proxy authentication username. This allows you to group requests under a specific session, ensuring they are all routed to the same upstream proxy.

**Format:** `<username>-sessid-<session_id>`

**Examples:**

```
# Without session ID - no affinity, each request is routed like random
Proxy-Authorization: Basic base64(myuser:password)

# With session ID - uses "order-123" for session affinity
Proxy-Authorization: Basic base64(myuser-sessid-order-123:password)

# Hyphenated username works too
Proxy-Authorization: Basic base64(my-project-sessid-abc456:password)
```

**Behavior:**

- The `-sessid-` delimiter separates the real username from the session ID. The password remains unchanged.
- The session ID is the only key the sticky strategy uses. The client's address is never one: behind a NAT or a load balancer it is shared by every client, and a session should exist only when a client asked for one.
- If the upstream proxy assigned to a session becomes unhealthy (e.g., IP rotation), a new proxy is automatically assigned on the next request.
- Without a session ID, the sticky strategy picks a connector by weight and a proxy inside it at random for each request, exactly like `random`.
- This feature only takes effect when the project's routing strategy is set to `sticky`. Other strategies ignore the session ID.
- Connectors running a residential or mobile product with **dynamic sessions** also forward the session to the vendor: the `-sessid-` value is hashed into the vendor's session id, so the same value keeps the same exit IP across requests and instances, and a request without `-sessid-` gets a fresh vendor session every time. The whole requested place is part of that hash: `-sessid-order-1-cc-de` and `-sessid-order-1-cc-us` are two vendor sessions with two exits, and so are `-cc-us-city-austin` and `-cc-us-city-dallas` under one `-sessid-`, each kept for as long as the client reuses it, since a vendor does not move a session it has already placed somewhere else. This applies under every routing strategy, not only `sticky`. See [Dynamic sessions]({{ site.baseurl }}/providers#descriptor-reference).

> **Note:** The strings `-sessid-`, `-cc-`, `-st-` and `-city-` are reserved delimiters and should not appear in your project username or in session IDs.

## Country Routing

A single project can hold connectors that exit from different countries. Instead of maintaining one project per location, clients choose the country per request by embedding a country code in the proxy authentication username, the same way session IDs work.

**Format:** `<username>-cc-<iso_code>`

The code is a two-letter ISO 3166-1 alpha-2 country code and is case-insensitive. `uk` is accepted as an alias of `gb`, the ISO code for the United Kingdom, wherever a country is entered: in this suffix, in a connector's country settings and on a manually added proxy. Anything else (`-cc-usa`) is answered with `400 Bad Request` rather than routed.

**Examples:**

```
# Route through proxies serving the United States
Proxy-Authorization: Basic base64(myuser-cc-us:password)

# Combine with a sticky session, in either order
Proxy-Authorization: Basic base64(myuser-cc-de-sessid-order-123:password)
Proxy-Authorization: Basic base64(myuser-sessid-order-123-cc-de:password)
```

### Declaring countries on a connector

Every connector has one **Countries** setting, on its General (or Infrastructure) tab. It takes zero or more codes, and **"Number of proxies" always means per country**. What happens then depends on how the provider reaches a country:

| Connector | Countries listed | Countries empty |
|-----------|------------------|-----------------|
| Residential and mobile pools (Oxylabs, Bright Data, Decodo, IPRoyal, NetNut) | *N* sessions are created **per listed country right away**, geo-targeted in the upstream credentials. Requests for other countries are refused. | *N* sessions are created without a country. In addition, the first `-cc-` request for any country creates *N* sessions geo-targeted to it, **on demand**, and they stay for later requests. |
| Port-based types that pick IPs from the vendor's list (Bright Data ISP and datacenter) | *N* IPs are pinned per listed country right away, chosen from the IPs the zone has in that country. | IPs are pinned in list order; each keeps its reported country for per-proxy matching. |
| Port-based types where each gateway port is pinned to an IP (Oxylabs and Decodo ISP and datacenter) | Gateway ports are scanned in order and **only IPs located in the listed countries are kept**, *N* per country. With 5 countries and *N* = 10 the scan aims for 50 proxies and stops early when the ports start returning failures or IPs already held, or after `max_scan_ports` ports. | Ports are taken in order regardless of location; each IP keeps its discovered country for per-proxy matching. |
| Static connectors | The countries the manually added proxies exit from, used for proxies whose own location is unknown. | Each proxy is matched on its own exit country, looked up when it was added (`proxy.geo_lookup`), set by hand, or refreshed from the Proxies page. |
| Cloud connectors | Optional: makes the region's instances selectable by country. | Not selectable with `-cc-`. |

For every port-based type with countries listed, the periodic IP refresh re-reads each proxy's location. A proxy whose exit moved outside its country is dropped immediately, and the reconciliation that follows the refresh discovers a replacement, so the pool never serves a country it should not.

### How a request is matched

Octoprox narrows the project's healthy proxies before the routing strategy runs, so country routing composes with every strategy and with [domain filtering]({{ site.baseurl }}/domain-filtering):

1. Connectors that list countries only serve the countries they list.
2. A proxy with a known exit country must match exactly. The exit country is what [IP attribution]({{ site.baseurl }}/ip-attribution) resolved for the proxy's exit IP from the local IP databases, the vendor's claim (list-mode entries, known-IP APIs, the country a slot was provisioned for) and the echo endpoint, in the order the attribution policy sets.
   Under a project's `strict` location policy, a proxy whose vendor-declared country is contradicted by attribution is not eligible at all.
3. A proxy with no known country is eligible only through its connector's list.
4. A residential or mobile pool with **no** countries listed that has no slot group yet for the requested country creates one on the first request: `num_proxies` fresh sessions geo-targeted to that country, ready immediately. Later requests rotate across that group like any other, and the periodic sync keeps it alive. Pools that list countries are provisioned up front and never expand on demand.

If nothing in the project serves the requested country, the request is rejected with `502 Bad Gateway`. Octoprox never silently falls back to another country.

## State and City Routing

Below the country a request can name a state and a city:

**Format:** `<username>-cc-<iso_code>[-st-<subdivision>][-city-<slug>]`

- `-st-` takes the country's first-level subdivision, whatever it is called locally, as the subdivision part of its ISO 3166-2 code: a US state (`ny` for `US-NY`), a Canadian province (`on`), a German Land (`by`), a French region (`idf`), England (`eng`). The full code (`US-NY`) is accepted too. "State" is the word the vendors use; the IP databases report the same first-level subdivision, so a fixed exit in Ontario matches `-cc-ca-st-on`.
- `-city-` takes the city as a slug: lower case, underscores for spaces (`new_york`, `los_angeles`, `saint_denis`). Spaces and hyphens typed instead are turned into underscores. A few spellings of one city are folded into one slug (`city_of_london` and `nyc` are `london` and `new_york`), and a qualifier in parentheses is dropped, so the IP databases' district entries (`Sofia (g.k. Banishora)`, `London (Soho)`) are the city they belong to.
- Both need `-cc-`. A username with a state or city and no country, a state that is not a subdivision code, or a city with nothing to slug is answered with `400 Bad Request` rather than routed anywhere.

```
# New York City exits
Proxy-Authorization: Basic base64(myuser-cc-us-st-ny-city-new_york:password)

# Any exit in Texas, kept for a session
Proxy-Authorization: Basic base64(myuser-cc-us-st-tx-sessid-order-9:password)
```

Two kinds of proxy can serve such a request, and a request is never quietly widened to the country:

| Proxy | How it serves a state or city |
|-------|-------------------------------|
| A residential or mobile connector with **dynamic sessions** | The state and city are rendered into the vendor request, in the vendor's spelling: Oxylabs and Decodo get the US state as a name (`us_new_york`), IPRoyal the name without spaces (`newyork`), Bright Data the ISO code, NetNut the `country_state_city` triple. A connector whose vendor cannot say what was asked is skipped: Oxylabs, Decodo, IPRoyal and NetNut name only US states, so `-cc-gb-st-eng` reaches Bright Data alone; NetNut takes a city only together with its state. The Webshare descriptor mirrors a proxy list and targets nothing per request. |
| A fixed exit: static, ISP, datacenter, list-mode and cloud proxies | Matched on where its exit IP is known to be: the state code and city [IP attribution]({{ site.baseurl }}/ip-attribution) resolved under the project's source policy from the IP databases, the vendor's own word (a city its list or discovery endpoint names) and the echo endpoint, exactly as the country is; or the state and city an operator set by hand on a static proxy (the pin wins). A proxy whose state or city is unknown does not match a request that names one. |

Pooled residential slots (fixed pool of sessions) serve the country only: their sessions were placed by the vendor without a state or city, and nothing is provisioned per city. If nothing in the project serves the place, the request is rejected with `502 Bad Gateway` naming it.

Vendors take US states everywhere and other subdivisions only on Bright Data, and cities everywhere; how well they honour a state or city is what the **Exit locations** pages report per level (see [Provider accuracy]({{ site.baseurl }}/ip-attribution#provider-accuracy)). Verification below the country needs a city-level IP database loaded.

With the project's preflight check set to `reject`, a session whose exit turns out to be somewhere other than the requested country is also rejected with `502 Bad Gateway` (`Exit country mismatch: requested DE, observed FR (...)`; a state or city that fails is named the same way), after one echo request through the selected upstream.

Requests **without** a `-cc-` suffix are unaffected: they may use any proxy in the project, except the on-demand country groups of *all countries* pools, which keep serving only the clients that asked for that country.

**Other behavior:**

- With the `sticky` strategy, a session that switches country, state or city is re-bound to a proxy in the new place.
- Country groups created on demand are not removed automatically. Delete unused ones from the Proxies page, or list the countries you need on the connector so the sync manages them.
- The Proxies page shows each proxy's country, and its state and city when known, and the Overview page shows a world map of where the project's proxies exit from, with healthy and total counts per country. A dynamic-sessions connector has no exit of its own to plot, since the vendor picks one per request, so the map tints the countries its allow-list names, or the whole world when it names none.

> **Note:** The strings `-cc-`, `-st-` and `-city-` are reserved delimiters and should not appear in your project username or in session IDs.
