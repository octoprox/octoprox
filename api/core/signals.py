# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Signal definitions for decoupled component communication.

This module defines blinker signals used for event-driven communication
between components like HealthChecker, ProxyServer, ProxyManager,
DemandTracker, and AutoScaler.

All signals support async receivers via send_async().
"""

from blinker import signal

# =============================================================================
# Health Check Signals
# =============================================================================

# Emitted when a health check completes for a proxy
# Sender: HealthChecker
# Args: proxy_id (str), status (ProxyStatus), latency_ms (float), consecutive_failures (int)
health_check_completed = signal("health-check-completed")


# =============================================================================
# Request/Traffic Signals
# =============================================================================

# Emitted when a proxy request completes (success or failure)
# Sender: ProxyServer
# Args: proxy_id (str), project_id (str), success (bool), latency_ms (float),
#       bytes_sent (int), bytes_received (int),
#       peer_id (str | None): the tunnel device the request came from, when it
#       arrived through a tunnel rather than the proxy port,
#       target_host (str | None): the destination the request was for (the
#       CONNECT target or the URL host), counted per host under the connector
request_completed = signal("request-completed")

# Emitted when a request is rejected (e.g., no upstream proxy available)
# Sender: ProxyServer
# Args: project_id (str), reason (str)
request_rejected = signal("request-rejected")


# =============================================================================
# Proxy Lifecycle Signals
# =============================================================================

# Emitted when a new proxy is added to the pool
# Sender: ProxyManager
# Args: proxy (Proxy)
proxy_added = signal("proxy-added")

# Emitted when a proxy is removed from the pool
# Sender: ProxyManager
# Args: proxy_id (str)
proxy_removed = signal("proxy-removed")

# Emitted when a proxy's status changes
# Sender: ProxyManager
# Args: proxy_id (str), old_status (ProxyStatus), new_status (ProxyStatus)
proxy_status_changed = signal("proxy-status-changed")


# =============================================================================
# Scaling Request Signals (AutoScaler -> ProxyManager)
# =============================================================================

# Emitted when AutoScaler wants to add a new proxy to the pool
# Sender: AutoScaler
# Args: proxy (Proxy)
proxy_add_requested = signal("proxy-add-requested")

# Emitted when AutoScaler wants to remove a proxy from the pool
# Sender: AutoScaler
# Args: proxy_id (str)
proxy_remove_requested = signal("proxy-remove-requested")

# Emitted when AutoScaler wants to start draining a proxy
# Sender: AutoScaler
# Args: proxy_id (str)
proxy_draining_requested = signal("proxy-draining-requested")

# Emitted when AutoScaler wants to mark a proxy as terminating
# Sender: AutoScaler
# Args: proxy_id (str)
proxy_terminating_requested = signal("proxy-terminating-requested")


# =============================================================================
# Scaling Event Signals (notifications after actions complete)
# =============================================================================

# Emitted when a proxy starts draining (no new requests, waiting for existing to complete)
# Sender: ProxyManager
# Args: proxy_id (str), connector_id (str)
proxy_draining_started = signal("proxy-draining-started")

# Emitted when a proxy is marked for termination (draining complete)
# Sender: ProxyManager
# Args: proxy_id (str), connector_id (str)
proxy_marked_terminating = signal("proxy-marked-terminating")

# Emitted when a scale-up operation is requested (for observability)
# Sender: AutoScaler
# Args: connector_id (str), count (int), reason (str)
scale_up_requested = signal("scale-up-requested")

# Emitted when a scale-down operation is requested (for observability)
# Sender: AutoScaler
# Args: connector_id (str), count (int), reason (str)
scale_down_requested = signal("scale-down-requested")

# Emitted when proxy rotation starts (creating replacement, draining old)
# Sender: AutoScaler
# Args: old_proxy_id (str), connector_id (str)
proxy_rotation_started = signal("proxy-rotation-started")

# Emitted when a cloud instance is terminated
# Sender: AutoScaler
# Args: proxy_id (str), connector_id (str), instance_id (str)
proxy_instance_terminated = signal("proxy-instance-terminated")

# Emitted when AutoScaler wants to remove a connector (after all proxies terminated)
# Sender: AutoScaler
# Args: connector_id (str)
connector_remove_requested = signal("connector-remove-requested")


# =============================================================================
# Connector Error Signals
# =============================================================================

# Emitted when a connector's error state changes (error occurred or cleared)
# Sender: AutoScaler
# Args: connector_id (str), error (str | None), consecutive_errors (int)
connector_error_updated = signal("connector-error-updated")


# =============================================================================
# Provider Syncer Signals
# =============================================================================

# Emitted when a proxy's metadata is updated (e.g., IP refresh)
# Sender: ProxyProviderSyncer
# Args: proxy (Proxy)
proxy_update_requested = signal("proxy-update-requested")

# Emitted when a provider connector needs to be synced (after create/update)
# Sender: API routes
# Args: connector (Connector)
provider_connector_sync_requested = signal("provider-connector-sync-requested")


# =============================================================================
# Cross-instance Cache Invalidation Signals
# =============================================================================
#
# These four are emitted by ProxyManager after every entity write to Postgres.
# They are the only signals routed across instances via Redis Pub/Sub by the
# EventBus. Receivers in other instances call ProxyManager.reload_<entity>(id)
# to refresh their cache. Local handlers can subscribe too - they fire
# in-process alongside the distributed publish.
#
# Sender: ProxyManager
# Args: entity_id (str), op (Literal["added", "updated", "removed"])
project_changed = signal("project-changed")
credential_changed = signal("credential-changed")
connector_changed = signal("connector-changed")
# proxy_changed takes one extra op: "status", emitted by
# ProxyManager.update_proxy_status when only the health fields moved. Receivers
# refresh those from Redis instead of reloading the row from Postgres - health
# flips are frequent enough that the difference is the peers' database load.
proxy_changed = signal("proxy-changed")

# Emitted by the providers routes after a custom provider descriptor is
# created, updated or deleted. Cross-instance: receivers reload that
# descriptor from Postgres into their provider registry (or unregister it).
# Sender: providers route
# Args: entity_id (provider_id, str), op (Literal["added", "updated", "removed"])
provider_changed = signal("provider-changed")

# Emitted by RateLimiter when a proxy is quarantined or released.
# Cross-instance: receivers re-hydrate that proxy's quarantine TTL from
# Redis so peer-side quarantines block traffic on this instance too.
# Sender: RateLimiter
# Args: entity_id (proxy_id, str), op (Literal["quarantined", "released"])
proxy_quarantine_changed = signal("proxy-quarantine-changed")

# Emitted by TrafficLimiter when a connector reaches its traffic limit and
# stops taking requests, or is released (period rollover, raised limit, manual
# reset). Cross-instance: receivers re-read the block key from Redis so the
# connector drops out of selection everywhere within a subscriber tick, and
# running transfers on peers are cut when the action is interrupt.
# Sender: TrafficLimiter
# Args: entity_id (connector_id, str), op (Literal["blocked", "released"])
connector_traffic_changed = signal("connector-traffic-changed")


# Emitted by the geo routes and the database updater after an IP database is
# uploaded, changed, deleted or refreshed. Cross-instance: receivers re-read the
# row (and fetch the new file when the checksum moved) into their GeoDatabaseStore.
# Sender: geo route / GeoDatabaseUpdater
# Args: entity_id (database id, str), op (Literal["added", "updated", "removed"])
geo_database_changed = signal("geo-database-changed")

# Emitted after the install-wide IP attribution settings row is written.
# Cross-instance: receivers reload it from Postgres.
# Sender: the geo settings route
# Args: entity_id ("default"), op (Literal["updated"])
geo_settings_changed = signal("geo-settings-changed")

# Emitted by the WireGuard routes after a peer (a device allowed into the
# tunnel) is created, changed or deleted. Cross-instance: every instance
# re-reads the row into its peer directory, and the one carrying the tunnel
# re-syncs the interface so the device can (or can no longer) connect.
# Sender: wireguard route
# Args: entity_id (peer id, str), op (Literal["added", "updated", "removed"])
wireguard_peer_changed = signal("wireguard-peer-changed")

# Emitted after the install-wide WireGuard settings row (key pair, endpoint,
# subnet) is written. Cross-instance: receivers reload it from Postgres and a
# carrying instance re-applies its key and port to the interface.
# Sender: wireguard route
# Args: entity_id ("default"), op (Literal["updated"])
wireguard_settings_changed = signal("wireguard-settings-changed")

# Emitted by the OpenVPN routes after a peer is created, changed or deleted.
# Cross-instance: every instance re-reads the row into its peer directory,
# and one carrying the endpoint ends the device's session when it may no
# longer connect (disabled, removed, certificate rotated).
# Sender: openvpn route
# Args: entity_id (peer id, str), op (Literal["added", "updated", "removed"])
openvpn_peer_changed = signal("openvpn-peer-changed")

# Emitted after the install-wide OpenVPN settings row (identity, endpoint,
# transport, subnet) is written. Cross-instance: receivers reload it from
# Postgres and a carrying instance restarts its daemon when what the daemon
# was started with changed.
# Sender: openvpn route
# Args: entity_id ("default"), op (Literal["updated"])
openvpn_settings_changed = signal("openvpn-settings-changed")

# Emitted by the transparent listener when a tunnel connection's destination
# name could not be recovered (not a fake IP, no SNI, no Host header) and it
# is relayed by address: domain filters then see an address and the exit may
# differ from the one that resolved the name. Local only; the ProxyManager
# counts it on the device, in the same pipeline as its requests.
# Sender: TransparentProxyServer
# Args: peer_id (str)
tunnel_name_unresolved = signal("tunnel-name-unresolved")

# Emitted by the transparent listener when it closes an encrypted-DNS
# connection (DNS over TLS, or a known DNS-over-HTTPS resolver) from a tunnel
# device so the device falls back to the tunnel resolver. Local only; counted
# on the device like the signal above.
# Sender: TransparentProxyServer
# Args: peer_id (str)
tunnel_encrypted_dns_blocked = signal("tunnel-encrypted-dns-blocked")

# Emitted in-process whenever a code path learns which IP a proxy exits from:
# the provider syncer after discovery, the health checker after a check whose
# response carried the caller's address. Local only; the ProxyAttributor
# subscribes and writes the attribution onto the proxy.
# Sender: ProxyProviderSyncer / HealthChecker
# Args: proxy_id (str), ip (str), source (ObservationSource value, str), endpoint_country (str | None)
exit_ip_observed = signal("exit-ip-observed")

# Emitted in-process by the ProxyAttributor when a sighting shows a proxy
# exiting from a different IP than the one recorded on it. Local only; the
# PreflightChecker subscribes and forgets the verdict it cached for the proxy,
# so the next request through it is verified against the new exit rather than
# trusted for the rest of the session TTL.
# Sender: ProxyAttributor
# Args: proxy_id (str), project_id (str | None), old_ip (str), new_ip (str)
exit_ip_changed = signal("exit-ip-changed")

# Emitted by the ExitVerifier when preflight finds a proxy exiting somewhere
# other than the country the request needed. Local only; the ProxyAttributor
# rotates a vendor-session slot or flags a fixed exit as contradicted.
# Sender: ExitVerifier
# Args: proxy_id (str), project_id (str), expected (str), observed (str | None), ip (str | None)
exit_location_mismatch = signal("exit-location-mismatch")
