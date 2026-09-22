# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""IP attribution: where an exit IP is, and whether the vendor told the truth about it.

The package has three layers, each usable without the ones above it:

* :mod:`api.geo.readers` open local IP databases (MaxMind, DB-IP, IPinfo and
  IP2Location files in mmdb format, IP2Location BIN when its library is
  installed) and normalise every vendor's record layout into one
  :class:`~api.geo.models.IpLocation`.
* :mod:`api.geo.resolver` combines the answers of the databases, the vendor's
  own claim and an echo endpoint into one :class:`~api.geo.models.Resolution`
  under the operator's source policy, and decides whether the vendor's claim
  is contradicted.
* :mod:`api.geo.service` applies resolutions to proxies, records every IP
  observation for provider tracking, and runs the per-session preflight check.
"""
