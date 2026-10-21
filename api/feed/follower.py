# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Following the routing feed: how a gateway learns every definition without Postgres, one verified update at a time.

It reads the head as it stands, never an entry's content, and hands on only a feed verified whole.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from api.core import utc_now
from api.core.consumers import BroadcastFollower
from api.core.workers import WorkerName
from api.db.redis import FEED_HEAD_KEY, FEED_MANIFEST_KEY, FEED_SECTIONS_KEY, FEED_STREAM_KEY
from api.feed.codec import FeedDecodeError
from api.feed.models import FEED_SCHEMA, GLOBAL_SECTION, FeedHead, Section, section_model

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.db.streams import StreamEntry
    from api.feed.codec import FeedCodec

logger = structlog.get_logger()


@dataclass(frozen=True)
class FeedUpdate:
    """One verified move of the feed, handed to the applier whole."""

    head: FeedHead
    # The sections new or changed since the last update applied: every one on the first.
    changed: dict[str, Section]
    # The sections the feed no longer has.
    removed: set[str]
    # Every section after this update: a new mapping each time, never changed afterwards.
    sections: Mapping[str, Section]


# What the gateway does with a verified update: build its next routing
# snapshot from the sections, and re-sync its tunnel endpoints and IP
# databases from the global section. An applier that raises leaves the
# update unapplied; the follower offers it again after a back-off.
FeedApplier = Callable[[FeedUpdate], Awaitable[None]]


class FeedFollower(BroadcastFollower):
    """Follows the feed stream and applies each verified update of the head; every gateway runs one.

    Args:
        codec: opens the sections; it must hold the controller's secret.
        reconcile_interval: seconds between two reads of the head that no entry asked for.
    """

    def __init__(
        self, redis_client: RedisClient, codec: FeedCodec, apply: FeedApplier, *, reconcile_interval: float
    ) -> None:
        super().__init__(
            redis_client, [FEED_STREAM_KEY], name=WorkerName.FEED_FOLLOWER, reconcile_interval=reconcile_interval
        )
        self._codec = codec
        self._apply = apply
        # The applied copy: each section, and its digest in the manifest it came from.
        self._sections: dict[str, Section] = {}
        self._digests: dict[str, str] = {}
        # The current head's schema while it is one this release cannot read.
        self._foreign_schema: int | None = None
        self._head_missing = False
        # Whether the last read of the head applied an update: a read that did not counts as idle.
        self._moved = False
        # The head last applied and when (naive UTC); None until the first apply.
        self.applied_head: FeedHead | None = None
        self.applied_at: datetime | None = None

    def readiness(self) -> str | None:
        """None once a feed is applied, whatever the head says since; else why there is none to route on."""
        if self.applied_head is not None:
            return None
        if self._foreign_schema is not None:
            return f"feed schema {self._foreign_schema} is not supported (this release reads {FEED_SCHEMA})"
        return "feed not applied yet"

    async def poll(self) -> bool:
        """Apply the feed if its head moved since the update applied last; whether one was.

        Raises :class:`FeedDecodeError` when what Redis holds cannot be trusted (another secret, a section missing or
        altered, no global section), and applies nothing then.
        """
        head = self._usable(await self._redis.client.get(FEED_HEAD_KEY))
        if head is None or self._is_applied(head):
            return False
        pipe = self._redis.client.pipeline(transaction=True)
        pipe.get(FEED_HEAD_KEY)
        pipe.hgetall(FEED_MANIFEST_KEY)
        raw_head, manifest = await pipe.execute()
        head = self._usable(raw_head)
        if head is None or self._is_applied(head):
            return False
        if not hmac.compare_digest(self._codec.manifest_digest(manifest), head.digest):
            raise FeedDecodeError("Feed manifest does not match its head: published with another internal secret?")
        if GLOBAL_SECTION not in manifest:
            raise FeedDecodeError("Feed has no global section")

        names = sorted(name for name, digest in manifest.items() if self._digests.get(name) != digest)
        changed = await self._fetch(raw_head, names, manifest)
        if changed is None:
            logger.debug("Routing feed moved while its sections were read; its entry brings it")
            return False
        removed = {name for name in self._sections if name not in manifest}
        sections = {name: section for name, section in self._sections.items() if name not in removed}
        sections.update(changed)
        await self._apply(FeedUpdate(head=head, changed=changed, removed=removed, sections=sections))

        # The first apply makes the gateway ready; the rest are routine.
        log = logger.info if self.applied_head is None else logger.debug
        log(
            "Routing feed applied",
            changes=head.changes,
            changed=len(changed),
            removed=len(removed),
            sections=len(sections),
        )
        self._sections = sections
        self._digests = dict(manifest)
        self.applied_head = head
        self.applied_at = utc_now()
        return True

    def _on_entries(self, key: str, entries: list[StreamEntry]) -> bool:
        """Any entry may announce a new head, so every one asks for a read of it."""
        return True

    async def _full_read(self) -> None:
        """Read the head and apply it if it moved; raising leaves the read owed, tried again after the back-off."""
        self._moved = await self.poll()

    async def _act(self) -> bool:
        """Read the head when a read is owed; one that found the head applied already counts as idle."""
        return await super()._act() and self._moved

    def _is_applied(self, head: FeedHead) -> bool:
        """Whether ``head`` describes the feed already applied."""
        return self.applied_head is not None and head.digest == self.applied_head.digest

    async def _fetch(self, raw_head: str, names: list[str], manifest: dict[str, str]) -> dict[str, Section] | None:
        """The sections named, opened and checked against ``manifest``; None when the head moved meanwhile."""
        if not names:
            return {}
        pipe = self._redis.client.pipeline(transaction=True)
        pipe.get(FEED_HEAD_KEY)
        pipe.hmget(FEED_SECTIONS_KEY, names)
        raw_again, payloads = await pipe.execute()
        if raw_again != raw_head:
            return None
        sections: dict[str, Section] = {}
        for name, payload in zip(names, payloads, strict=True):
            if payload is None:
                raise FeedDecodeError(f"Feed section {name} is missing")
            sections[name] = self._codec.decode(payload, section_model(name), manifest[name])
        return sections

    def _usable(self, raw_head: str | bytes | None) -> FeedHead | None:
        """The head when this release can apply it; None while it is missing or of another schema.

        Each of those is logged when it starts, not at every read.
        """
        if raw_head is None:
            if not self._head_missing:
                # Expected before the controller's first publish (a first start,
                # a switch-over), so only a gateway with a feed to lose warns.
                log = logger.warning if self.applied_head is not None else logger.info
                log("Routing feed head missing; keeping the feed applied so far", applied=self.applied_head is not None)
            self._head_missing = True
            return None
        self._head_missing = False
        try:
            head = FeedHead.model_validate_json(raw_head)
        except ValidationError:
            raise FeedDecodeError("Feed head is unreadable") from None
        if head.schema_version != FEED_SCHEMA:
            if self._foreign_schema != head.schema_version:
                logger.error(
                    "Routing feed of another schema ignored", schema=head.schema_version, supported=FEED_SCHEMA
                )
            self._foreign_schema = head.schema_version
            return None
        self._foreign_schema = None
        return head
