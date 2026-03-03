# TLS Extraction — Low Priority Future Work

## TCP Fingerprint (p0f-style)

OS-level fingerprinting from the TCP SYN packet: window size, TTL, MSS, window
scaling, TCP options order. Tells you the OS, not just the TLS library. Requires
raw socket access which may not be feasible in the current asyncio architecture.

## Raw Extension Payload Sizes

The byte length of each TLS extension payload. Two clients can advertise the
same extensions but with different payload sizes (e.g., different numbers of
key_share groups produce different sizes). Niche but useful for advanced
fingerprinting.

## HTTP/2 Fingerprint (Akamai-style)

After the TLS handshake, fingerprint the HTTP/2 SETTINGS frame parameters,
WINDOW_UPDATE values, and PRIORITY/HEADERS pseudo-header order. This is one of
the most effective signals for identifying browsers vs bot frameworks. Sometimes
called "HTTP/2 fingerprint" or referenced in the JA4H spec.
