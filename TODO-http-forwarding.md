# Plain-HTTP Forwarding - Tracked Follow-ups

Gaps in `ProxyServer._handle_http` found while fixing the leaked
Proxy-Authorization header (Sep 2026). None of these affect CONNECT
tunnels, which relay bytes without parsing HTTP. All are rare for proxy
clients in practice but each is a correctness hole.

## Expect: 100-continue

A client that sends `Expect: 100-continue` waits for a `100 Continue`
before transmitting the body. `_handle_http` immediately calls
`readexactly(content_length)` on the client stream, so both sides wait
until the connection timeout. curl does this for POST bodies above 1 MB.

Fix: when the request carries `Expect: 100-continue`, answer
`HTTP/1.1 100 Continue` on the client side ourselves, drop the Expect
header from the relayed request, then read and forward the body as today.

## Chunked request bodies

Only `Content-Length` bodies are read from the client. A request with
`Transfer-Encoding: chunked` has its headers relayed but its body never
follows, so the upstream hangs waiting for it. `_forward_chunked` already
exists for the response direction and can be reused client-to-upstream.

## Response bodies without length or chunking

The response body is relayed only when the upstream sends `Content-Length`
or `Transfer-Encoding: chunked`. A response with neither (body ends at
connection close) is silently truncated to headers. We now send
`Connection: close` upstream, so the correct behaviour is to read until
EOF and relay. Needs a timeout guard: `readexactly` and the body reads
have none today, and an upstream that ignores `Connection: close` on a
bodiless response (204, 304, HEAD) would hang a read-until-EOF forever.

## Vendor 407 relayed as-is

If the upstream vendor rejects our credentials, its 407 is copied to the
client, who then believes their Octoprox credentials are wrong. The
`Proxy-Authenticate` challenge is now stripped, but the status should be
translated to a 502 with a body naming the upstream, and the request
should count as a failure for quarantine purposes. Today `success` is set
as soon as any status line arrives, so vendor-side 4xx/5xx never trip
health tracking on this path.

## Connection reuse

One request per client connection. Clients that pipeline or reuse the
socket get a closed connection on the second request. We now advertise
`Connection: close` in every response so well-behaved clients open a new
connection rather than fail; real keep-alive support would need a request
loop in `_handle_client` and per-request upstream selection.
