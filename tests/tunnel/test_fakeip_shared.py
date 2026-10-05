# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The cluster-wide fake-IP mapping, against a real Redis."""

from api.db.redis import TUNNEL_FAKEIP_COUNTER_KEY, RedisClient
from api.tunnel.dns import FakeIpDirectory, FakeIpPool


async def test_two_instances_agree_on_the_mapping(redis_client: RedisClient) -> None:
    first = FakeIpDirectory(FakeIpPool("198.18.0.0/15"), redis_client)
    second = FakeIpDirectory(FakeIpPool("198.18.0.0/15"), redis_client)

    ip = await first.ip_for("media.example.net")
    # The other instance never saw the query but turns the address back into the name.
    assert await second.name_for(str(ip)) == "media.example.net"
    # And hands out the same address for the same name.
    assert await second.ip_for("Media.Example.NET") == ip
    # Different names get different addresses, wherever they are asked.
    other = await second.ip_for("other.example.net")
    assert other != ip
    assert await first.name_for(str(other)) == "other.example.net"


async def test_allocation_skips_taken_offsets(redis_client: RedisClient) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    await redis_client.fakeip_allocate("a.test", pool.capacity)
    # Wind the counter back so the next allocation would land on a's offset.
    await redis_client.client.set(TUNNEL_FAKEIP_COUNTER_KEY, 0)
    offset = await redis_client.fakeip_allocate("b.test", pool.capacity)
    assert offset == 1
    assert await redis_client.fakeip_name_for(0) == "a.test"
    assert await redis_client.fakeip_offset_for("b.test") == 1
    assert await redis_client.fakeip_offset_for("nobody.test") is None
