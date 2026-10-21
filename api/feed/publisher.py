# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Publishing the routing feed: the controller's one write path to every gateway, only what changed and only while it leads.

Each publish diffs against what Redis holds, and one script fenced by the controller lease writes it.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from api.db.redis import (
    FEED_HEAD_KEY,
    FEED_MANIFEST_KEY,
    FEED_SECTIONS_KEY,
    FEED_STREAM_KEY,
    FEED_STREAM_MAXLEN,
)
from api.feed.models import FEED_SCHEMA, FeedHead, Section

if TYPE_CHECKING:
    from redis.commands.core import AsyncScript

    from api.db.redis import RedisClient
    from api.feed.codec import FeedCodec

logger = structlog.get_logger()

# Publishes one compile, fenced by the controller lease. Nothing is written
# unless the lease still names this process, and then everything is, so the
# manifest, the sections and the head never disagree. The feed stream gets
# the head's digest only when the head changed, in the same step, so an
# entry is never visible before the feed it announces, and a publish sent
# again (the same head) wakes nobody.
#   KEYS[1] = the controller lease, KEYS[2] = head, KEYS[3] = manifest, KEYS[4] = sections,
#   KEYS[5] = the feed stream
#   ARGV[1] = the lease owner this publish needs
#   ARGV[2] = the head JSON
#   ARGV[3] = "1" to start the manifest and the sections over (the stored feed is not trusted)
#   ARGV[4] = the head's digest, ARGV[5] = the feed stream's approximate MAXLEN
#   ARGV[6] = n, then n triples (section, digest, payload) to write, then the sections to drop
# Returns 1 when written, 0 when the lease names another owner or none.
_PUBLISH_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return 0
end
if ARGV[3] == '1' then
  redis.call('DEL', KEYS[3], KEYS[4])
end
local last = 6 + 3 * tonumber(ARGV[6])
for i = 7, last, 3 do
  redis.call('HSET', KEYS[3], ARGV[i], ARGV[i + 1])
  redis.call('HSET', KEYS[4], ARGV[i], ARGV[i + 2])
end
for i = last + 1, #ARGV do
  redis.call('HDEL', KEYS[3], ARGV[i])
  redis.call('HDEL', KEYS[4], ARGV[i])
end
local previous = redis.call('GET', KEYS[2])
redis.call('SET', KEYS[2], ARGV[2])
if previous ~= ARGV[2] then
  redis.call('XADD', KEYS[5], 'MAXLEN', '~', ARGV[5], '*', 'digest', ARGV[4])
end
return 1
"""


@dataclass(frozen=True)
class PublishResult:
    """What one publish did."""

    # Sections encoded and written: the new and changed ones, or every one
    # when the stored feed was not trusted.
    written: int
    # Sections the compile no longer has, dropped from Redis.
    removed: int
    # The head was gone before this publish: a fresh Redis, or a wiped one.
    head_missing: bool
    # The controller lease names another process, or none: nothing was written.
    lease_lost: bool


class FeedPublisher:
    """Writes each compile of the feed to Redis: only what changed, and only while this process leads.

    One publish at a time, and a section object is never changed once handed over, so one handed over again keeps its digest.

    Args:
        codec: seals the sections; the followers must hold the same secret.
        owner_id: the value the lease holds while this process leads: its ``instance_id``.
    """

    def __init__(self, redis_client: RedisClient, codec: FeedCodec, *, lease_key: str, owner_id: str) -> None:
        self._redis = redis_client
        self._codec = codec
        self._lease_key = lease_key
        self._owner_id = owner_id
        self._script: AsyncScript | None = None
        # Each section of the last publish with its digest, by name: a section
        # passed again as the same object keeps its digest. A digest depends
        # on the content alone, so the memo is right whether or not that
        # publish got through; the diff is always against Redis's manifest.
        self._digests: dict[str, tuple[Section, str]] = {}

    async def publish(
        self, sections: Mapping[str, Section], *, changes: str | None, compiled_at: datetime
    ) -> PublishResult:
        """Make Redis hold this compile, every section in ``sections`` and no other, encoding only those that changed.

        ``changes`` and ``compiled_at`` go into the head for display; a publish that writes a new head wakes the gateways.
        """
        # The JSON serialised for a digest here, which the encode below reuses.
        serialised: dict[str, bytes] = {}
        digests: dict[str, str] = {}
        for name, section in sections.items():
            known = self._digests.get(name)
            if known is not None and known[0] is section:
                digests[name] = known[1]
            else:
                serialised[name] = section.model_dump_json().encode()
                digests[name] = self._codec.digest(serialised[name])
        self._digests = {name: (section, digests[name]) for name, section in sections.items()}
        pipe = self._redis.client.pipeline(transaction=True)
        pipe.get(FEED_HEAD_KEY)
        pipe.hgetall(FEED_MANIFEST_KEY)
        pipe.hlen(FEED_SECTIONS_KEY)
        raw_head, stored, stored_count = await pipe.execute()
        head_missing = raw_head is None
        trusted = self._trusts(raw_head, stored, stored_count)
        manifest: dict[str, str] = dict(stored) if trusted else {}
        changed = sorted(name for name, digest in digests.items() if manifest.get(name) != digest)
        dropped = sorted(name for name in manifest if name not in digests)
        if trusted and not changed and not dropped:
            return PublishResult(written=0, removed=0, head_missing=False, lease_lost=False)

        triples: list[str] = []
        for name in changed:
            section_json = serialised.get(name)
            if section_json is None:
                section_json = sections[name].model_dump_json().encode()
            digest, payload = self._codec.seal(section_json)
            manifest[name] = digest
            triples += [name, digest, payload]
        for name in dropped:
            del manifest[name]
        head = FeedHead(
            schema_version=FEED_SCHEMA,
            digest=self._codec.manifest_digest(manifest),
            changes=changes,
            compiled_at=compiled_at,
            sections=len(manifest),
        )
        if self._script is None:
            self._script = self._redis.client.register_script(_PUBLISH_SCRIPT)
        written = await self._script(
            keys=[self._lease_key, FEED_HEAD_KEY, FEED_MANIFEST_KEY, FEED_SECTIONS_KEY, FEED_STREAM_KEY],
            args=[
                self._owner_id,
                head.model_dump_json(),
                "0" if trusted else "1",
                head.digest,
                str(FEED_STREAM_MAXLEN),
                str(len(changed)),
                *triples,
                *dropped,
            ],
        )
        if not written:
            logger.warning("Routing feed not published: the controller lease is not held", lease=self._lease_key)
            return PublishResult(written=0, removed=0, head_missing=head_missing, lease_lost=True)
        if trusted:
            logger.debug("Routing feed published", changes=changes, written=len(changed), removed=len(dropped))
        else:
            logger.info(
                "Routing feed published in full", changes=changes, sections=len(manifest), head_missing=head_missing
            )
        return PublishResult(written=len(changed), removed=len(dropped), head_missing=head_missing, lease_lost=False)

    def _trusts(self, raw_head: str | None, manifest: dict[str, str], stored_sections: int) -> bool:
        """Whether Redis holds one whole compile this process could have published.

        The head must be readable, of this schema, and carry the HMAC of the
        manifest under this process's secret, and the sections hash must hold
        as many sections as the manifest lists.
        """
        if raw_head is None or not manifest or len(manifest) != stored_sections:
            return False
        try:
            head = FeedHead.model_validate_json(raw_head)
        except ValidationError:
            return False
        return head.schema_version == FEED_SCHEMA and hmac.compare_digest(
            head.digest, self._codec.manifest_digest(manifest)
        )
