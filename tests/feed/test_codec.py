# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the feed codec: sealing and opening sections, digests, and the internal token.

Pure, no Redis: a payload is opened by the codec that sealed it, by one
holding another secret, and after being altered.
"""

import base64

import pytest

from api.feed.codec import FeedCodec, FeedDecodeError, internal_token
from api.feed.models import ConnectorSection, GlobalSection
from tests.feed.conftest import SECRET, make_connector_section, make_project


class TestRoundTrip:
    def test_a_section_comes_back_as_it_went_in(self, codec: FeedCodec) -> None:
        section = make_connector_section(hosts=("10.0.0.1", "10.0.0.2"))
        digest, payload = codec.encode(section)
        assert codec.decode(payload, ConnectorSection, digest) == section

    def test_a_payload_is_compressed_and_shows_nothing_of_its_content(self, codec: FeedCodec) -> None:
        section = GlobalSection(projects=[make_project(id=f"project-{n}", password="hunter2-feed") for n in range(50)])
        _, payload = codec.encode(section)
        assert len(payload) < len(section.model_dump_json())
        assert b"hunter2-feed" not in base64.b64decode(payload)


class TestDigest:
    def test_an_unchanged_section_keeps_its_digest_across_encodes(self, codec: FeedCodec) -> None:
        first_digest, first_payload = codec.encode(make_connector_section())
        second_digest, second_payload = codec.encode(make_connector_section())
        assert first_digest == second_digest
        # Every encode seals with a fresh nonce.
        assert first_payload != second_payload

    def test_a_changed_section_gets_another_digest(self, codec: FeedCodec) -> None:
        digest, _ = codec.encode(make_connector_section())
        assert codec.encode(make_connector_section(hosts=("10.0.0.2",)))[0] != digest

    def test_digests_are_keyed_by_the_secret(self, codec: FeedCodec) -> None:
        section_json = make_connector_section().model_dump_json().encode()
        assert codec.digest(section_json) != FeedCodec("another-secret").digest(section_json)

    def test_the_manifest_digest_ignores_the_order_of_entries(self, codec: FeedCodec) -> None:
        digest = codec.manifest_digest({"global": "a", "connector:x": "b"})
        assert codec.manifest_digest({"connector:x": "b", "global": "a"}) == digest
        assert codec.manifest_digest({"global": "a", "connector:x": "c"}) != digest


class TestDecodeRefusals:
    def test_another_secret_cannot_open_a_payload(self, codec: FeedCodec) -> None:
        digest, payload = codec.encode(make_connector_section())
        with pytest.raises(FeedDecodeError, match="cannot be opened"):
            FeedCodec("another-secret").decode(payload, ConnectorSection, digest)

    def test_an_altered_payload_is_refused(self, codec: FeedCodec) -> None:
        digest, payload = codec.encode(make_connector_section())
        raw = bytearray(base64.b64decode(payload))
        raw[-1] ^= 1
        with pytest.raises(FeedDecodeError, match="cannot be opened"):
            codec.decode(base64.b64encode(bytes(raw)).decode(), ConnectorSection, digest)

    @pytest.mark.parametrize("payload", ["not base64!", "", base64.b64encode(b"short").decode()])
    def test_a_payload_that_is_no_sealed_section_is_refused(self, codec: FeedCodec, payload: str) -> None:
        with pytest.raises(FeedDecodeError, match="cannot be opened"):
            codec.decode(payload, ConnectorSection, "digest")

    def test_a_payload_newer_than_its_manifest_entry_is_refused(self, codec: FeedCodec) -> None:
        digest, _ = codec.encode(make_connector_section())
        _, newer = codec.encode(make_connector_section(hosts=("10.0.0.2",)))
        with pytest.raises(FeedDecodeError, match="manifest entry"):
            codec.decode(newer, ConnectorSection, digest)

    def test_a_section_that_does_not_parse_is_refused(self, codec: FeedCodec) -> None:
        digest, payload = codec.encode(GlobalSection())
        with pytest.raises(FeedDecodeError, match="does not parse as ConnectorSection"):
            codec.decode(payload, ConnectorSection, digest)


class TestInternalToken:
    def test_one_secret_gives_one_token(self) -> None:
        token = internal_token(SECRET)
        assert token == internal_token(SECRET)
        assert token != internal_token("another-secret")
        # 256 bits, as hex.
        assert len(token) == 64
        int(token, 16)
