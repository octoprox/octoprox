---
layout: docs
title: Domain Filtering
nav_id: domain-filtering
---

# Domain Filtering

<p class="subtitle">Control which target domains are routed through each connector.</p>

Connectors support optional domain-based filtering to control which target domains their proxies serve. This is configured per-connector via the `routing_config` field.

- **Whitelist mode** — Only requests for the listed domains are routed through the connector.
- **Blacklist mode** — All requests *except* those for the listed domains are routed through the connector.

Domain matching is hierarchical: `bing.com` matches `bing.com` and all subdomains (`www.bing.com`, `images.bing.com`, etc.).

Connectors with no domain filtering rules (the default) allow all domains. See the [API Reference]({{ site.baseurl }}/api#domain-filtering-routing_config) for configuration details.
