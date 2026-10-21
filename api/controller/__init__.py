# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The controller role's loops: the feed builder (:mod:`~api.controller.feed_builder`) and the job producers.

Both act only in the process holding the controller lease; :mod:`api.roles.controller` composes them, and workers do
the work the producers (:mod:`~api.controller.producers`) publish.
"""
