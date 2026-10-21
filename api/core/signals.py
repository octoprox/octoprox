# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The in-process signals a gateway's listeners publish about each request.

The gateway's request metering and failure log receive them on the request
path, where ``send_async`` passes a receiver's exception back to the
publisher, so a receiver touches memory only and never raises.
"""

from blinker import signal

# Emitted when a proxy request completes (success or failure).
# Sender: ProxyServer
# Args: proxy_id (str), project_id (str), success (bool), latency_ms (float),
#       bytes_sent (int), bytes_received (int),
#       peer_id (str | None): the tunnel device the request came from, when it
#       arrived through a tunnel rather than the proxy port,
#       target_host (str | None): the destination the request was for (the
#       CONNECT target or the URL host), counted per host under the connector
request_completed = signal("request-completed")

# Emitted when a request is refused before it reaches an upstream proxy (no
# proxy available, every candidate quarantined, the traffic limit reached, a
# preflight mismatch or an invalid location); counted as demand.
# Sender: ProxyServer
# Args: project_id (str), reason (str)
request_rejected = signal("request-rejected")

# Emitted for every request that failed or was refused, describing why, for
# the project's failure log (api.core.failure_log). Fires alongside
# request_completed (success=False) or request_rejected, and for a failed
# request inside an intercepted connection.
# Sender: ProxyServer / MitmHandler
# Args: failure (RequestFailure)
request_failed = signal("request-failed")

# Emitted by the transparent listener when a tunnel connection's destination
# name could not be recovered (not a fake IP, no SNI, no Host header) and it
# is relayed by address: domain filters then see an address and the exit may
# differ from the one that resolved the name. The gateway's request metering
# counts it on the device, in the same pipeline as its requests.
# Sender: TransparentProxyServer
# Args: peer_id (str)
tunnel_name_unresolved = signal("tunnel-name-unresolved")

# Emitted by the transparent listener when it closes an encrypted-DNS
# connection (DNS over TLS, or a known DNS-over-HTTPS resolver) from a tunnel
# device so the device falls back to the tunnel resolver. Counted on the
# device like the signal above.
# Sender: TransparentProxyServer
# Args: peer_id (str)
tunnel_encrypted_dns_blocked = signal("tunnel-encrypted-dns-blocked")
