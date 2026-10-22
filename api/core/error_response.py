# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What an error answer tells the client besides its status code.

A client library that fails a CONNECT shows its caller the status line
and nothing else (requests, httpx) or the status line and the headers
(aiohttp); none of them reads the body. So the message that explains an
error goes in the reason phrase as well as the body, and a failure of the
upstream leg is also named in an RFC 9209 Proxy-Status header.
"""

import re
import unicodedata

from api.core.failure_log import RequestFailure
from api.core.stats import FailureKind

#: How this proxy names itself in a Proxy-Status header.
PROXY_NAME = "octoprox"

# Longest detail a reason phrase carries; the body has the whole message.
MAX_REASON_DETAIL_CHARS = 200

# RFC 9209 error types for the failures of the upstream leg. A refusal is
# the vendor answering the CONNECT or SOCKS handshake with an error; for an
# HTTP upstream the header also carries the vendor's status. A client that
# went away is not told about its own doing.
_UPSTREAM_ERROR_TYPES = {
    FailureKind.UPSTREAM_TIMEOUT: "connection_timeout",
    FailureKind.UPSTREAM_UNREACHABLE: "destination_unavailable",
    FailureKind.UPSTREAM_REJECTED: "connection_refused",
    FailureKind.UPSTREAM_ERROR: "http_protocol_error",
}

_NOT_PRINTABLE = re.compile(r"[^\x21-\x7e]+")


def _folded(text: str) -> str:
    """``text`` as printable ASCII on one line, cut to ``MAX_REASON_DETAIL_CHARS``."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    folded = _NOT_PRINTABLE.sub(" ", folded).strip()
    if len(folded) > MAX_REASON_DETAIL_CHARS:
        folded = folded[: MAX_REASON_DETAIL_CHARS - 3].rstrip() + "..."
    return folded


def reason_phrase(phrase: str, detail: str = "") -> str:
    """The status line's reason phrase: ``phrase``, then ``detail`` in parentheses.

    The detail is folded to printable ASCII on one line: it can quote a
    vendor's answer, and a line break in it would end the status line.
    """
    folded = _folded(detail)
    return f"{phrase} ({folded})" if folded else phrase


def refusal_message(answer: str, vendor_message: str | None = None) -> str:
    """What the client is told when the upstream proxy refused the tunnel with ``answer``, and said ``vendor_message``."""
    message = f"Upstream proxy refused the tunnel: {answer}" if answer else "Upstream proxy refused the tunnel"
    return f"{message}. The vendor said: {vendor_message}" if vendor_message else message


def proxy_status(error: str, received_status: int | None = None, details: str | None = None) -> str:
    """A Proxy-Status value naming ``error``, an RFC 9209 error type, the next hop's status, and ``details``."""
    value = f"{PROXY_NAME}; error={error}"
    if received_status is not None:
        value += f"; received-status={received_status}"
    if details and (folded := _folded(details)):
        escaped = folded.replace("\\", "\\\\").replace('"', '\\"')
        value += f'; details="{escaped}"'
    return value


def upstream_proxy_status(failure: RequestFailure) -> str | None:
    """The Proxy-Status value for a failure of the upstream leg, with what the vendor said; None when there is nothing to name."""
    error = _UPSTREAM_ERROR_TYPES.get(failure.kind)
    return None if error is None else proxy_status(error, failure.upstream_status, failure.upstream_message)


def refused_proxy_status(received_status: int | None) -> str:
    """The Proxy-Status value for the upstream proxy refusing a tunnel with ``received_status``."""
    return proxy_status(_UPSTREAM_ERROR_TYPES[FailureKind.UPSTREAM_REJECTED], received_status)


#: The Proxy-Status value for a connector over its traffic limit, whichever status it answers with.
BANDWIDTH_LIMIT_PROXY_STATUS = proxy_status("bandwidth_limit_exceeded")
