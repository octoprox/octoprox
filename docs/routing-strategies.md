---
layout: docs
title: Routing Strategies
nav_id: routing-strategies
---

# Routing Strategies

<p class="subtitle">Distribute requests across proxy pools using different routing strategies.</p>

Octoprox supports multiple routing strategies for distributing requests across proxy pools:

| Strategy | Description |
|----------|-------------|
| `round_robin` | Distributes requests evenly across all healthy proxies in order |
| `least_used` | Routes to the proxy with the fewest active connections |
| `random` | Randomly selects a healthy proxy for each request |
| `sticky` | Routes requests from the same client to the same proxy |
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
# Without session ID - uses client IP for session affinity
Proxy-Authorization: Basic base64(myuser:password)

# With session ID - uses "order-123" for session affinity
Proxy-Authorization: Basic base64(myuser-sessid-order-123:password)

# Hyphenated username works too
Proxy-Authorization: Basic base64(my-project-sessid-abc456:password)
```

**Behavior:**

- The `-sessid-` delimiter separates the real username from the session ID. The password remains unchanged.
- When a session ID is provided, it replaces the client IP as the session identifier for the sticky strategy.
- If the upstream proxy assigned to a session becomes unhealthy (e.g., IP rotation), a new proxy is automatically assigned on the next request.
- Without a session ID, the sticky strategy falls back to using the client IP address, which is the default behavior.
- This feature only takes effect when the project's routing strategy is set to `sticky`. Other strategies ignore the session ID.
- Connectors running a residential or mobile product with **dynamic sessions** also forward the session to the vendor: the `-sessid-` value is hashed into the vendor's session id, so the same value keeps the same exit IP across requests and instances, and a request without `-sessid-` gets a fresh vendor session every time. This applies under every routing strategy, not only `sticky`. See [Dynamic sessions]({{ site.baseurl }}/providers#descriptor-reference).

> **Note:** The string `-sessid-` is a reserved delimiter and should not appear in your project username.

## Country Routing

A single project can hold connectors that exit from different countries. Instead of maintaining one project per location, clients choose the country per request by embedding a country code in the proxy authentication username, the same way session IDs work.

**Format:** `<username>-cc-<iso_code>`

The code is a two-letter ISO 3166-1 alpha-2 country code and is case-insensitive. `uk` is accepted as an alias of `gb`, the ISO code for the United Kingdom, wherever a country is entered: in this suffix, in a connector's country settings and on a manually added proxy.

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

With the project's preflight check set to `reject`, a session whose exit turns out to be somewhere other than the requested country is also rejected with `502 Bad Gateway` (`Exit location mismatch`), after one echo request through the selected upstream.

Requests **without** a `-cc-` suffix are unaffected: they may use any proxy in the project, except the on-demand country groups of *all countries* pools, which keep serving only the clients that asked for that country.

**Other behavior:**

- With the `sticky` strategy, a session that switches country is re-bound to a proxy in the new country.
- Country groups created on demand are not removed automatically. Delete unused ones from the Proxies page, or list the countries you need on the connector so the sync manages them.
- The Proxies page shows each proxy's country when it is known, and the Overview page shows a world map of where the project's proxies exit from, with healthy and total counts per country. A dynamic-sessions connector has no exit of its own to plot, since the vendor picks one per request, so the map tints the countries its allow-list names, or the whole world when it names none.

> **Note:** The string `-cc-` is a reserved delimiter and should not appear in your project username or in session IDs.
