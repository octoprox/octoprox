# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""WireGuard key material: generated here, in the format wg expects."""

import base64

from api.wireguard import keys


def test_keypair_is_wireguard_shaped() -> None:
    private, public = keys.generate_keypair()
    assert len(private) == 44 and len(public) == 44
    assert len(base64.b64decode(private)) == 32
    assert keys.public_key_of(private) == public
    assert private != public


def test_derivation_is_deterministic_and_pairs_are_distinct() -> None:
    private, public = keys.generate_keypair()
    assert keys.public_key_of(private) == public
    other_private, other_public = keys.generate_keypair()
    assert other_private != private and other_public != public


def test_validation() -> None:
    assert keys.is_valid_key(keys.generate_preshared_key())
    assert not keys.is_valid_key("not-a-key")
    assert not keys.is_valid_key(base64.b64encode(b"short").decode())
    assert not keys.is_valid_key("")
