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
#       bytes_sent (int), bytes_received (int)
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

