# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Sites and IPs as exit reputation keeps them: a site is the exact host a request went to, an IP its one canonical text.

``www.google.com``, ``maps.google.com`` and ``google.com`` are three sites, since each may judge an exit on its own.
An IP-literal host is its own site. The Public Suffix List, bundled with ``publicsuffixlist``, only keeps a report
from naming a public suffix such as ``co.uk``, which no request goes to.
"""

from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from urllib.parse import urlsplit

from publicsuffixlist import PublicSuffixList

# The longest a domain name can be, and the width of a stored site.
MAX_SITE_LENGTH = 253
# The longest text a host can be given as: the longest name with a trailing dot and a port.
MAX_HOST_TEXT = MAX_SITE_LENGTH + len(".:65535")
# Distinct hosts whose site a process remembers: gateways ask once per request.
SITE_CACHE_SIZE = 16_384

# The list with its private section (github.io, appspot.com), as browsers read it.
_SUFFIXES = PublicSuffixList()
# Where a host written without a scheme ends: its path, query or fragment.
_HOST_END = re.compile(r"[/?#]")
# What a host name holds in its ASCII form: letters, digits, hyphens and dots, and the underscores some names carry.
_HOST_NAME = re.compile(r"[a-z0-9_.-]+")


def canonical_ip(text: str) -> str | None:
    """``text`` as its IP's one text (an IPv4-mapped IPv6 as the IPv4), or None when it is not an IP.

    A scoped IPv6 (``fe80::1%eth0``) names an address on one host's link, never an exit, so it is not one either.
    """
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            return None
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
    return str(address)


def site_of(host: str) -> str | None:
    """The site a request to ``host`` counts for: the host in lower case without port, brackets or trailing dot, in
    its ASCII form, or an IP literal's canonical IP; None when there is no host."""
    # Longer text names no host, and is kept out of the cache, which would otherwise hold it whole.
    if len(host) > MAX_HOST_TEXT:
        return None
    return _site_of(host)


@lru_cache(maxsize=SITE_CACHE_SIZE)
def _site_of(host: str) -> str | None:
    """:func:`site_of` for text short enough to be a host, remembered per host."""
    name = _bare(host)
    if not name:
        return None
    ip = canonical_ip(name)
    if ip is not None:
        return ip
    site = _ascii(name)
    return site if len(site) <= MAX_SITE_LENGTH else None


def counted_site(host: str | None) -> str | None:
    """The site a request to ``host`` is counted for (:func:`site_of`), when a record can hold it: None for no host, and
    for text with spaces or control characters, which no request reaches a site by and Postgres may refuse."""
    site = site_of(host) if host else None
    if site is None or not site.isprintable() or " " in site:
        return None
    return site


def report_site(text: str) -> str:
    """The site an exit report names, the exact host of a host name or a URL; ValueError when it names none or a public
    suffix."""
    # A leading dot, as in a cookie's domain, still names the domain.
    name = _bare(_named_host(text)).lstrip(".")
    if not name:
        raise ValueError("site is empty")
    if canonical_ip(name) is None:
        encoded = _ascii(name)
        # A wildcard, a space, an address with a user, a Unicode name that does not encode: no request goes there.
        if "" in encoded.split(".") or not _HOST_NAME.fullmatch(encoded):
            raise ValueError(f"not a site: {name} is not a host name")
        if _SUFFIXES.is_public(encoded):
            raise ValueError(f"not a site: {name} is a public suffix")
    site = site_of(name)
    if site is None:
        raise ValueError(
            f"not a site: longer than the {MAX_SITE_LENGTH} characters a host name can have"
        )
    return site


def _bare(host: str) -> str:
    """``host`` as a request names it, lower case, without brackets, port or trailing dot."""
    name = host.strip().lower()
    if name.startswith("["):
        # A bracketed IPv6 literal, with or without a port after it.
        name = name[1:].partition("]")[0]
    elif name.count(":") == 1:
        # One colon is a port: a name or IPv4 has none, an IPv6 literal without brackets has several.
        name = name.partition(":")[0]
    return name.rstrip(".")


def _ascii(name: str) -> str:
    """``name`` with its Unicode labels in the ASCII form a CONNECT carries; unchanged when it cannot be encoded."""
    if name.isascii():
        return name
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return name


def _named_host(text: str) -> str:
    """The host part of what a report names: a URL's host, else the text up to a path, query or fragment."""
    text = text.strip()
    if "://" in text or text.startswith("//"):
        try:
            return urlsplit(text).hostname or ""
        except ValueError:
            # A URL whose brackets do not close.
            return ""
    return _HOST_END.split(text, maxsplit=1)[0]
