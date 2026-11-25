# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reputation: what clients report about the exits and connectors that served them on a site, decided per site.

* :mod:`api.reputation.site` turns a host or URL into the site a record is kept for (the exact host) and an IP into
  its one canonical text.
* :mod:`api.reputation.record` holds the records, one per (project, exit IP, site) and one per (project, connector,
  site, requested place), and decides as evidence arrives until when an IP is avoided for a site and what factor a
  dynamic connector's weight takes there.
* :mod:`api.reputation.tag_note` is what a gateway notes of a request a client tagged with ``-tag-``, which an exit
  report names it by.
* :mod:`api.reputation.reports` resolves the exit reports the api takes and appends them for the workers;
  :mod:`api.reputation.ingest` applies them to the records, and :mod:`api.reputation.signal` tells the controller when
  routing moved.
* :mod:`api.reputation.weight` is the effective-weight rule: what a connector's routing weight is multiplied by for a
  request's site, on gateways and in the api alike; :mod:`api.reputation.split` shows it beside the traffic split, and
  :mod:`api.reputation.history` how the dynamic connectors' factors moved at a place over time.
"""
