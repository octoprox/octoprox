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

> **Note:** The string `-sessid-` is a reserved delimiter and should not appear in your project username.

## Country Routing

A single project can hold connectors that exit from different countries. Instead of maintaining one project per location, clients choose the country per request by embedding a country code in the proxy authentication username, the same way session IDs work.

**Format:** `<username>-cc-<iso_code>`

The code is a two-letter ISO 3166-1 alpha-2 country code and is case-insensitive.

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

For every port-based type with countries listed, the periodic IP refresh re-reads each proxy's location. A proxy whose exit moved outside its country is dropped immediately, and the reconciliation that follows the refresh discovers a replacement, so the pool never serves a country it should not.
| Static connectors | The countries the manually added proxies exit from, used for proxies whose own location is unknown. | Each proxy is matched on its own exit country, looked up when it was added (`proxy.geo_lookup`), set by hand, or refreshed from the Proxies page. |
| Cloud connectors | Optional: makes the region's instances selectable by country. | Not selectable with `-cc-`. |

### How a request is matched

Octoprox narrows the project's healthy proxies before the routing strategy runs, so country routing composes with every strategy and with [domain filtering]({{ site.baseurl }}/domain-filtering):

1. Connectors that list countries only serve the countries they list.
2. A proxy with a known exit country must match exactly. The exit country is what the vendor reported (list-mode entries, known-IP APIs, or IP discovery for port-mode types such as Oxylabs ISP, Decodo ISP and Bright Data ISP) or, failing that, the country the slot was provisioned for.
3. A proxy with no known country is eligible only through its connector's list.
4. A residential or mobile pool with **no** countries listed that has no slot group yet for the requested country creates one on the first request: `num_proxies` fresh sessions geo-targeted to that country, ready immediately. Later requests rotate across that group like any other, and the periodic sync keeps it alive. Pools that list countries are provisioned up front and never expand on demand.

If nothing in the project serves the requested country, the request is rejected with `502 Bad Gateway`. Octoprox never silently falls back to another country.

Requests **without** a `-cc-` suffix are unaffected: they may use any proxy in the project, except the on-demand country groups of *all countries* pools, which keep serving only the clients that asked for that country.

**Other behavior:**

- With the `sticky` strategy, a session that switches country is re-bound to a proxy in the new country.
- Country groups created on demand are not removed automatically. Delete unused ones from the Proxies page, or list the countries you need on the connector so the sync manages them.
- The Proxies page shows each proxy's country when it is known, and the Overview page shows a world map of where the project's proxies exit from, with healthy and total counts per country.

> **Note:** The string `-cc-` is a reserved delimiter and should not appear in your project username or in session IDs.
