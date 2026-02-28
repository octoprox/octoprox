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
# Without session ID — uses client IP for session affinity
Proxy-Authorization: Basic base64(myuser:password)

# With session ID — uses "order-123" for session affinity
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
