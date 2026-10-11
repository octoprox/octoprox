# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Sign-in through an identity provider: Google, Microsoft or GitHub.

``identity`` turns what a provider says about an account into the one thing
Octoprox acts on, a verified email and a stable subject, and is pure.
``flow`` drives the OAuth round trip (authorize, callback, token exchange)
with the per-login state in Redis so the callback may land on any instance.
"""

from api.sso.flow import SsoFlow
from api.sso.identity import Identity, SsoError

__all__ = ["Identity", "SsoError", "SsoFlow"]
