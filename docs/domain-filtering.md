---
layout: docs
title: "Domain Filtering per Connector"
nav_id: domain-filtering
description: "Route only certain target domains through a connector with whitelist or blacklist rules, matched hierarchically across subdomains."
---

# Domain Filtering

<p class="subtitle">Control which target domains are routed through each connector.</p>

Connectors support optional domain-based filtering to control which target domains their proxies serve. This is configured per-connector via the `routing_config` field.

- **Whitelist mode** - Only requests for the listed domains are routed through the connector.
- **Blacklist mode** - All requests *except* those for the listed domains are routed through the connector.

Domain matching is hierarchical: `bing.com` matches `bing.com` and all subdomains (`www.bing.com`, `images.bing.com`, etc.).

Connectors with no domain filtering rules (the default) allow all domains. See the [API Reference]({{ site.baseurl }}/api#domain-filtering-routing_config) for configuration details.

Domain filtering combines with [country routing]({{ site.baseurl }}/routing-strategies#country-routing): a request with a `-cc-<code>` username suffix only considers connectors that both allow the target domain and serve that country.

It also feeds [connector weights]({{ site.baseurl }}/routing-strategies#connector-weights): a connector whose filter excludes the target domain takes no share of that request, and the remaining connectors split it by their own weights.
