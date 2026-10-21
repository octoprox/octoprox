# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Routing on a gateway: which proxy a request goes through, from the applied feed and what only this gateway knows.

:mod:`~api.routing.snapshot` indexes the feed and :mod:`~api.routing.router` selects; this gateway's own state lives in
:mod:`~api.routing.exclusions`, :mod:`~api.routing.inflight`, :mod:`~api.routing.outcomes` and
:mod:`~api.routing.ondemand`.
"""
