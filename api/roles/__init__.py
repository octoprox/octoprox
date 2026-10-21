# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The runtimes each process role composes, and the node that runs them.

One module per runtime (``api``, ``controller``, ``worker``, ``gateway``) with
its snapshot model, beside the echo role's app (``echo``), the definitions
refresher (``refresh``) and the :class:`~api.roles.node.Node` (``node``);
importing the package imports the node, and with it every runtime.
"""

from __future__ import annotations

from api.roles.node import Node

__all__ = ["Node"]
