# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The Redis key catalogue, and the one client per process that every role but echo reaches Redis through.

Any key may vanish: readers treat a missing key as empty, and each writer
puts back what it owns on its next cycle.
"""

import asyncio
import json
import secrets
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from typing import Any, NamedTuple, cast
from urllib.parse import urlsplit, urlunsplit

import redis.asyncio as redis
import structlog
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.commands.core import AsyncScript
from redis.exceptions import NoScriptError, ResponseError
from redis.maint_notifications import MaintNotificationsConfig

from api.core import utc_now
from api.core.logging import error_text
from api.core.roles import JobKind
from api.core.stats import (
    DemandCountKey,
    FailureCount,
    FailureCountKey,
    FailureExample,
    FailureKind,
    HostKey,
    MetricDelta,
    TunnelPeerMetricDelta,
    WindowCounts,
)

logger = structlog.get_logger()


def _literal_prefix(key_format: str) -> str:
    """The text of a key format before its first placeholder: every key built from it starts with this."""
    return key_format.partition("{")[0]


# What every key of this release starts with. 5.x writes the same layouts
# unprefixed, so on one Redis 6.0 never reads what 5.x left, and 5.0.11
# redeployed as the way back never reads 6.0's keys.
KEY_PREFIX = "o6:"

# Redis key formats. Keep all of them in this module so the global layout is
# auditable from one place.
PROXY_METRICS_KEY = KEY_PREFIX + "proxy:metrics:{proxy_id}"
PROJECT_METRICS_KEY = KEY_PREFIX + "project:metrics:{project_id}"
CONNECTOR_METRICS_KEY = KEY_PREFIX + "connector:metrics:{connector_id}"
# Per tunnel device (any protocol), the fourth hash in the metrics pipeline;
# carries the two name-resolution counters as well (TunnelPeerMetricDelta).
TUNNEL_PEER_METRICS_KEY = KEY_PREFIX + "tunnel_peer:metrics:{peer_id}"
# Per destination host under one connector of one project, the fifth hash in
# the metrics pipeline (the Hosts page). The host is the last segment so a
# bracketed IPv6 literal's colons do not break the key apart; the two ids
# before it are UUIDs and never contain a colon.
HOST_METRICS_PREFIX = KEY_PREFIX + "host:metrics:"
HOST_METRICS_KEY = HOST_METRICS_PREFIX + "{project_id}:{connector_id}:{host}"
# The proxy a sticky session is bound to, per project and session id (the
# sticky strategy), expiring with the binding.
STICKY_BINDING_KEY = KEY_PREFIX + "sticky:{project_id}:{session_id}"
# A project's MITM captures (api.core.mitm.handler): one stream capped at
# about a thousand entries, kept until they are cleared or the project goes.
MITM_REQUESTS_KEY = KEY_PREFIX + "mitm:requests:{project_id}"
# Each project's failure log (api.core.failure_log), per kind of failure: a
# sorted set of the examples' fingerprints, ranked by when Redis last took a
# gateway's addition to them (its clock, one for every gateway), and a hash
# with each example's description (JSON, under the fingerprint, written
# once) and the tallies every gateway adds to (fingerprint:count, :first and
# :last in epoch ms, :latency and :timed). Trimmed by age and count as it is
# written, so a flood of one kind never pushes out the rare ones.
FAILURE_LOG_KEY = KEY_PREFIX + "failure:log:{project_id}:{kind}"
FAILURE_LOG_DATA_KEY = FAILURE_LOG_KEY + ":data"
# Failure counts by (project, connector, kind, vendor status), the sixth hash
# in the metrics pipeline: written by each gateway's failure log next to its
# examples, taken by the metrics flush into failure_metrics. "-" stands for no
# connector (refused before a proxy was chosen) and for no vendor status.
FAILURE_METRICS_PREFIX = KEY_PREFIX + "failure:metrics:"
FAILURE_METRICS_KEY = FAILURE_METRICS_PREFIX + "{project_id}:{connector_id}:{kind}:{upstream_status}"
# One set per kind of metrics hash (METRIC_WINDOW_KINDS), listing its keys,
# so the metrics flush takes the hashes without walking the keyspace. A walk
# costs a round trip per few hundred keys of the whole keyspace (sticky
# bindings and preflight verdicts included), and on a saturated event loop
# each round trip waits its turn: a 5.x reload walking it took minutes at
# peak. A write adds its key after its increments; the take
# (RedisClient.take_metric_windows) removes a key as it moves the hash out,
# or when it finds the hash gone.
METRICS_INDEX_KEY = KEY_PREFIX + "metrics_index:{kind}"
# A live process's role, rewritten on every heartbeat: the key's TTL is what
# says the process is alive.
INSTANCE_REGISTRY_KEY = KEY_PREFIX + "instance_registry:{instance_id}"
# Every process that has advertised itself, so readers find them without
# walking the keyspace. Liveness is still the registry key's TTL: readers
# skip a member whose key has expired, and the index hygiene job drops it.
INSTANCE_INDEX_KEY = KEY_PREFIX + "instance_index"
# What one process reports about itself (api.models.system.InstanceSnapshot:
# its runtime, readiness, loops and role sections), published on the same
# heartbeat so the System page shows every process, not just the one the load
# balancer picked. A second key rather than a richer registry payload, so a
# process whose snapshot cannot be built stays listed by its registry key.
INSTANCE_STATS_KEY = KEY_PREFIX + "instance_stats:{instance_id}"
# TTL of both instance keys, and the interval on which they are rewritten.
# The interval is half the TTL so one missed write does not declare an
# instance dead. Reading code derives snapshot age from the remaining TTL,
# which avoids comparing clocks across hosts.
INSTANCE_TTL_SECONDS = 10
INSTANCE_HEARTBEAT_INTERVAL = 5
# Per-proxy rate-limit window (api.core.rate_limiter): a sorted set of the
# requests counted in the window, expiring a second after the window does.
PROXY_REQUESTS_KEY = KEY_PREFIX + "proxy:requests:{proxy_id}"
# Lease keys (api.core.leadership): the controller's leadership, and the
# moment's lock while one process creates the MITM CA (api.core.workers.LeaseName).
LEASE_KEY = KEY_PREFIX + "lease:{name}"
# One hash per instance carrying a tunnel protocol (peer key -> JSON status),
# rewritten every TUNNEL_STATUS_INTERVAL seconds and expiring soon after the
# carrier stops, so a dead carrier's peers read as offline. Several carriers
# behind a load balancer each publish their own; readers merge. The protocol
# follows the prefix: ``o6:wireguard:peer_status:...``, ``o6:openvpn:peer_status:...``.
TUNNEL_PEER_STATUS_KEY = KEY_PREFIX + "{protocol}:peer_status:{instance_id}"
# The instances that have published a protocol's statuses, so readers do not
# walk the keyspace for them; a member whose hash has expired is dropped.
TUNNEL_PEER_STATUS_INDEX_KEY = KEY_PREFIX + "{protocol}:peer_status_index"
# The fake-IP mapping the tunnel DNS hands out, shared so a connection that
# lands on a different instance from the one that answered the query still
# knows the name, whichever tunnel protocol carried either. Two keys per
# mapping, both refreshed when read, so a name nobody has asked about for a
# week frees its address.
TUNNEL_FAKEIP_NAME_KEY = KEY_PREFIX + "tunnel:fakeip:name:{name}"
TUNNEL_FAKEIP_ADDR_KEY = KEY_PREFIX + "tunnel:fakeip:addr:{offset}"
TUNNEL_FAKEIP_COUNTER_KEY = KEY_PREFIX + "tunnel:fakeip:next"
TUNNEL_FAKEIP_TTL_SECONDS = 7 * 86400
TUNNEL_STATUS_INTERVAL = 10
TUNNEL_STATUS_TTL_SECONDS = 30
# IP attribution: one preflight verdict per proxy, shared by its sessions.
GEO_PREFLIGHT_KEY = KEY_PREFIX + "geo:preflight:{project_id}:{proxy_id}"
# Sign-in through an identity provider (api.sso). The state key holds what a
# login that left for the provider needs when the browser comes back: which
# provider, the nonce and the PKCE verifier. The login code entry holds the
# bearer token the browser trades its one-time code for on the way back in:
# filed under a digest of the code, never the code, and sealed with a key
# drawn from the code (api.sso.flow), so Redis alone yields no sign-in. Both
# live in Redis rather than in process memory because the callback may land
# on a different instance from the one that started the login.
SSO_STATE_KEY = KEY_PREFIX + "sso:state:{state}"
SSO_STATE_TTL_SECONDS = 600
SSO_LOGIN_CODE_KEY = KEY_PREFIX + "sso:login_code:{digest}"
SSO_LOGIN_CODE_TTL_SECONDS = 60
# The routing feed (api.feed): the definitions the controller compiled from
# Postgres, for gateways, which have no database. The head (a string of
# FeedHead JSON) names the current compile; the manifest hash maps each
# section ("global", "runtime", "connector:<id>") to the HMAC digest of its plaintext;
# the sections hash holds each section compressed and encrypted with a key
# derived from internal_secret, so reading Redis does not reveal the
# definitions (vendor credentials among them). Only the active controller
# writes them, all three in one fenced script, so they never disagree.
# Gateways follow the feed stream (FEED_STREAM_KEY) and fetch only the
# sections whose digest moved. No TTL: a lost key is written again by the
# controller's next publish, and gateways keep routing on their last
# snapshot meanwhile.
FEED_HEAD_KEY = KEY_PREFIX + "feed:head"
FEED_MANIFEST_KEY = KEY_PREFIX + "feed:manifest"
FEED_SECTIONS_KEY = KEY_PREFIX + "feed:sections"
# Requests ("r") and rejections ("x") of one project in one minute, a hash
# per epoch minute (epoch seconds // 60): gateways add their counts every few
# seconds; scale jobs and the api's demand view read the minutes of the demand
# window. Each expires a minute after it leaves the window.
DEMAND_KEY = KEY_PREFIX + "demand:{project_id}:{minute}"
# One cloud connector's autoscaler state (a hash): rotation schedule, demand
# hysteresis, drain baselines and cooldown, read and written by its scale
# job, deleted with the connector. No TTL. Losing it is safe: the job holds
# off scaling in until it has seen a demand window, and takes the drain
# baselines again.
AUTOSCALER_STATE_KEY = KEY_PREFIX + "autoscaler:{connector_id}"
# The cloud instances scale jobs are creating for one connector (a hash:
# creation name -> JSON {"purpose": "scale" or "rotate:<proxy id>", "at":
# epoch seconds}). A scale job writes an entry before each create and clears
# it once the instance's row is saved, so a run that overlaps it counts the
# instance as capacity, and a later run resolves an entry its creator left
# behind. Deleted with the connector.
SCALE_CREATING_KEY = KEY_PREFIX + "scale:creating:{connector_id}"
# Connectors blocked by their traffic limit (a hash: connector id -> the end
# of the block, ISO naive UTC). The traffic job alone writes it, when its
# decision changes, and says so on the runtime stream; the feed's runtime
# section, the scale job, index hygiene and the api read it (api.feed.state).
# An entry goes when a decision says "not blocked", and index hygiene asks for
# a decision of every entry each minute, so a lost or stale entry lasts a
# minute at most.
TRAFFIC_BLOCKED_KEY = KEY_PREFIX + "traffic:blocked"
# Connectors waiting for the traffic decision a reset or a limit edit asked
# for (a hash: connector id -> when it was asked, ISO naive UTC). Written by
# the api, in the MULTI that asks for the decision, and deleted by the traffic
# job with the decision that settles it; read by the traffic job, index
# hygiene and the api (api.feed.state), never by gateways. An entry lasts a
# minute at most, since index hygiene asks for a decision of every entry each
# minute.
TRAFFIC_PENDING_KEY = KEY_PREFIX + "traffic:pending"
# One metrics flush's windows (a hash: window key -> the window's fields as
# JSON), moved out of the live windows under the flush's job entry ID
# (RedisClient.take_metric_windows), committed to Postgres with that ID, then
# deleted. A flush delivered again takes only what metering wrote since, and a
# flush given up before its commit puts them back. Expires a day after the
# take, so one nothing settles cannot stay.
METRICS_TAKEN_KEY = KEY_PREFIX + "metrics:taken:{message_id}"
METRICS_TAKEN_TTL_SECONDS = 86400
# A proxy's soft health (a hash): consecutive failures, the last check's
# latency and when it ran. The health job rewrites it on every check and
# reads it on the next; the api shows it. The status itself is a Postgres
# column. Checks come a minute apart by default, so the day runs out only for
# a proxy no longer checked (deleted, draining, disabled), and nothing has to
# delete its hash.
PROXY_HEALTH_KEY = KEY_PREFIX + "proxy:health:{proxy_id}"
PROXY_HEALTH_TTL_SECONDS = 86400
# Every active quarantine in one sorted set: member the proxy id, score the
# epoch second the quarantine ends. A gateway's rate limiter adds a proxy
# when it trips (ZADD GT, so a longer quarantine is never shortened) and
# says so on the runtime stream (RUNTIME_STREAM_KEY); the leading controller
# folds the ones still in force into the feed's runtime section, the index
# hygiene job trims the ended ones each minute, and the api lists and lifts
# them. No TTL: the trim is the expiry.
QUARANTINE_KEY = KEY_PREFIX + "proxy:quarantined"
# The last exit a proxy was seen handing out, as "<observed_at epoch ms>
# <ip>". The observation ingest compares each observation with it in one
# script per batch: a hand-out counts only for a newer observation of another
# IP, and the record only moves forward, so competing workers and late or
# redelivered batches count each transition once. Its own key rather than a
# field of the health hash, so proxies that are never health checked (dynamic
# gateways) count A -> B -> A like the rest. A missing key falls back to the
# exit-table holder rule, so expiry costs at most one uncounted return; a day
# keeps every active proxy's key, and a late write for a removed proxy just
# expires. Not tied to exit_ip_retention_days, whose default of 0 means
# forever.
PROXY_LAST_EXIT_KEY = KEY_PREFIX + "proxy:last_exit:{proxy_id}"
PROXY_LAST_EXIT_TTL_SECONDS = 86400

# The event streams (api.db.streams), each trimmed to about its MAXLEN as it
# is written. An entry says only what to read again, so a consumer that
# misses some reads in full and loses nothing.
#
# A definition was committed (api.db.streams.ChangeKind). Ten thousand
# entries hold hours of ordinary churn.
CHANGES_STREAM_KEY = KEY_PREFIX + "stream:changes"
CHANGES_STREAM_MAXLEN = 10_000
# A quarantine was tripped or lifted, or a connector's traffic block changed
# (api.db.streams.RuntimeSignal).
RUNTIME_STREAM_KEY = KEY_PREFIX + "stream:runtime"
RUNTIME_STREAM_MAXLEN = 10_000
# The controller published a new feed head (field ``digest``). Gateways
# apply the current head on any entry, so a short stream is enough.
FEED_STREAM_KEY = KEY_PREFIX + "stream:feed"
FEED_STREAM_MAXLEN = 100

# The work streams: one piece of work per entry, taken by the worker
# processes through one consumer group, each process a consumer named by its
# client's writer_id. A worker deletes each entry once it is handled, so a
# stream holds only the work in hand: queued, running, or waiting to be
# reclaimed from a worker that died. The cap only bounds a stream no worker
# takes, dropping its oldest entries.
WORKER_GROUP = "workers"
#
# One job stream per job kind (api.core.roles.JobKind), an entry per run
# asked for (field ``key``: a proxy id, a connector id,
# ``<connector>:<country>``, a reattribute scope, or ``all`` for the
# install-wide kinds). The leading controller's producers append each key of
# a periodic kind once per period, and any role appends a run now. A key
# appended twice runs twice, which every kind's job takes in its stride.
JOB_STREAM_KEY = KEY_PREFIX + "jobs:{kind}:stream"
JOB_STREAM_MAXLEN = 100_000
# Preflight sightings of pooled session slots (SightingMessage, field
# ``item``, JSON), repeats included: gateways append them in batches Redis
# applies once however often they are sent (RedisClient.append_batch), and
# workers attribute them.
SIGHTING_STREAM_KEY = KEY_PREFIX + "stream:sightings"
SIGHTING_STREAM_MAXLEN = 50_000
# IP observations and exit judgements (api.geo.observations, field ``item``,
# JSON) from every api, gateway and worker process, appended the same way.
# Workers ingest them in any order, since every write they make only moves
# forward. Twice the sightings' cap: it holds a Postgres outage, or a while
# with no worker.
OBSERVATION_STREAM_KEY = KEY_PREFIX + "stream:observations"
OBSERVATION_STREAM_MAXLEN = 100_000
# Every work stream by the name the System view shows it under: each job
# kind's, then the sightings and the observations.
WORK_STREAMS: dict[str, str] = {
    **{f"jobs.{kind}": JOB_STREAM_KEY.format(kind=kind) for kind in JobKind},
    "sightings": SIGHTING_STREAM_KEY,
    "observations": OBSERVATION_STREAM_KEY,
}
# The same names by stream key: what a log line about a work stream calls it.
WORK_STREAM_NAMES: dict[str, str] = {key: name for name, key in WORK_STREAMS.items()}

# One process's applied batch marks (BatchId): channel -> the highest
# sequence Redis has applied. Refreshed by every batch, so it goes a day
# after the process's last one.
APPLIED_BATCHES_KEY = KEY_PREFIX + "applied:{writer}"
APPLIED_BATCHES_TTL_SECONDS = 86400

# Logical grouping of the keyspace, used by the admin system view to report
# what Redis memory is being spent on. Built from the formats above; a key
# nothing matches is ``OTHER_KEY_GROUP``, or ``EARLIER_RELEASES_KEY_GROUP``
# when it lacks ``KEY_PREFIX``.
REDIS_KEY_GROUPS: tuple[tuple[str, str], ...] = (
    (_literal_prefix(PROXY_HEALTH_KEY), "Proxy health"),
    (_literal_prefix(PROXY_METRICS_KEY), "Proxy metrics"),
    (QUARANTINE_KEY, "Quarantine"),
    (_literal_prefix(PROXY_REQUESTS_KEY), "Rate-limit windows"),
    (_literal_prefix(PROXY_LAST_EXIT_KEY), "IP attribution"),
    (_literal_prefix(PROJECT_METRICS_KEY), "Project metrics"),
    (_literal_prefix(CONNECTOR_METRICS_KEY), "Connector metrics"),
    (_literal_prefix(TUNNEL_PEER_METRICS_KEY), "Tunnel device metrics"),
    (HOST_METRICS_PREFIX, "Host metrics"),
    (FAILURE_METRICS_PREFIX, "Failure metrics"),
    (_literal_prefix(METRICS_INDEX_KEY), "Metrics indexes"),
    (_literal_prefix(STICKY_BINDING_KEY), "Sticky bindings"),
    (_literal_prefix(MITM_REQUESTS_KEY), "MITM captures"),
    (_literal_prefix(FAILURE_LOG_KEY), "Failure log"),
    (_literal_prefix(INSTANCE_REGISTRY_KEY), "Instance heartbeats"),
    (INSTANCE_INDEX_KEY, "Instance heartbeats"),
    (_literal_prefix(INSTANCE_STATS_KEY), "Instance snapshots"),
    (_literal_prefix(LEASE_KEY), "Leases"),
    (_literal_prefix(AUTOSCALER_STATE_KEY), "Auto-scaler state"),
    (_literal_prefix(SCALE_CREATING_KEY), "Scale"),
    (TRAFFIC_BLOCKED_KEY, "Traffic blocks"),
    (TRAFFIC_PENDING_KEY, "Traffic blocks"),
    (_literal_prefix(METRICS_TAKEN_KEY), "Metrics staging"),
    (_literal_prefix(GEO_PREFLIGHT_KEY), "IP attribution"),
    # TUNNEL_PEER_STATUS_KEY and its index, per protocol.
    (KEY_PREFIX + "wireguard:", "WireGuard"),
    (KEY_PREFIX + "openvpn:", "OpenVPN"),
    # The fake-IP names, addresses and counter.
    (KEY_PREFIX + "tunnel:", "Tunnel devices"),
    (KEY_PREFIX + "sso:", "Sign-in flows"),
    (KEY_PREFIX + "feed:", "Routing feed"),
    (_literal_prefix(JOB_STREAM_KEY), "Job streams"),
    (_literal_prefix(DEMAND_KEY), "Demand"),
    # The two work streams that are no job kind's, as the System page groups them, before the event streams' row,
    # which their keys also match.
    (SIGHTING_STREAM_KEY, "Ingest streams"),
    (OBSERVATION_STREAM_KEY, "Ingest streams"),
    (KEY_PREFIX + "stream:", "Event streams"),
    (_literal_prefix(APPLIED_BATCHES_KEY), "Batch marks"),
)

OTHER_KEY_GROUP = "Other"
# A key without KEY_PREFIX: a leftover of 5.x, which this release never reads.
EARLIER_RELEASES_KEY_GROUP = "Earlier releases"

# How many hashes one pipeline reads or one take script moves, and how many
# items one batch part carries, at a time: bounds one reply when thousands of
# host hashes are read, and how long one script holds Redis, while a page of
# proxies still costs a single round trip.
_HASH_BATCH = 500

# HSCAN's COUNT hint when a flush's staging hash is read back: thousands of
# host windows would otherwise come back as one reply of tens of megabytes.
_STAGING_PAGE = 1000

# How long a pooled connection may sit idle before its next command first
# checks it with a PING, in seconds. A connection closed while it sat idle
# (a NAT or load balancer forgetting the flow, or Redis's own idle timeout)
# then fails the PING and is replaced before the command goes out, instead of
# failing the command. Half a minute is well inside the minutes those keep an
# idle flow, and the check costs a PING only after a quiet spell that long.
IDLE_CONNECTION_CHECK_SECONDS = 30

# How long one reply may take, in seconds, before the command fails and its
# connection is closed: without it a command on a half-open connection waits
# for TCP to give up, about 15 minutes. It outlasts the longest healthy wait,
# a blocked stream read (api.db.streams.STREAM_BLOCK_SECONDS).
REPLY_TIMEOUT_SECONDS = 30.0
# The request deadline when the client is given none: how long the request
# path, each step after a committed write, and a connect wait for Redis. The
# default of Settings.redis_request_timeout, which config.py states on its own.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 2.0

# How many connections one client's pool may open. redis-py 8 caps a pool at
# 100 and refuses the next command at once (MaxConnectionsError) rather than
# waiting for a connection to come free. A busy gateway's request path can
# have more calls than that in flight, and each refused one would fail open:
# a sticky binding read as absent, a preflight verdict checked again. This is
# redis-py 7's bound, which no pool reaches, but the pool does not grow as it
# did on 7.1.1: a command in flight that finds no connection free now opens
# one (7.1.1 opened them one at a time, so a command that finished meanwhile
# handed its own on), and a pool never closes an idle one. So a process keeps
# as many connections as it once had commands in flight: a burst of 150 calls
# leaves 150 open, where 7.1.1 opened 14. Every process's connections count
# against Redis's maxclients (10000 by default).
POOL_MAX_CONNECTIONS = 2**31

# Drops index members whose key is gone. KEYS[1] is the index; ARGV holds
# pairs of member and the key it stands for. Checked and removed in one step:
# a reader that saw a key missing must not unlist one a writer has just
# recreated.
_PRUNE_INDEX_SCRIPT = """
local removed = 0
for i = 1, #ARGV, 2 do
  if redis.call('EXISTS', ARGV[i + 1]) == 0 then
    removed = removed + redis.call('SREM', KEYS[1], ARGV[i])
  end
end
return removed
"""

# Moves metrics windows into a flush's staging hash
# (RedisClient.take_metric_windows). KEYS[1] is the staging hash, KEYS[2] the
# windows' index, KEYS[3..] window keys it lists. A window already staged is
# left alone, so a flush delivered again keeps what it took, and a window
# metering recreated since waits for the next flush. Any other window is
# staged as JSON, deleted and unlisted in one step, so no increment lands
# between its read and its delete; one that is empty or gone is only
# unlisted. Returns how many windows it staged.
_TAKE_SCRIPT = """
local taken = 0
for i = 3, #KEYS do
  local key = KEYS[i]
  if redis.call('HEXISTS', KEYS[1], key) == 0 then
    local values = redis.call('HGETALL', key)
    if #values > 0 then
      local window = {}
      for j = 1, #values, 2 do
        window[values[j]] = values[j + 1]
      end
      redis.call('HSET', KEYS[1], key, cjson.encode(window))
      redis.call('DEL', key)
      taken = taken + 1
    end
    redis.call('SREM', KEYS[2], key)
  end
end
return taken
"""

# The start of every batch script. KEYS[1] is the process's applied hash
# (APPLIED_BATCHES_KEY); ARGV[1] the channel, ARGV[2] the sequence, ARGV[3]
# the hash's TTL. A sequence at or below the channel's mark returns -1 and
# writes nothing. The mark moves before any write, so a script that fails
# part-way is never applied twice. Batch scripts return numbers only: a Lua
# boolean's reply shape differs by protocol.
_BATCH_PROLOGUE = """
local done = tonumber(redis.call('HGET', KEYS[1], ARGV[1]) or '0')
if tonumber(ARGV[2]) <= done then
  return -1
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
"""

# One part of a gateway's metering window (RedisClient.metering_batch), and
# the traffic decisions it asks for. KEYS[2] is the traffic job stream; the
# window hashes, their indexes and the demand hashes follow, each named in
# ARGV by its KEYS position. ARGV[4] updated_at, ARGV[5] the demand TTL,
# ARGV[6] the job stream's MAXLEN, ARGV[7] h, then per window hash: its
# position, its index's position, f, then f triples (field, amount, 'f' or
# 'i'); then d, then d triples (demand hash position, field, count); then t,
# then t connector ids, each appended to the job stream as a ``key`` entry.
# A hash joins its index after its increments, so the take never unlists a
# hash just recreated. The entries come after the bytes they ask a decision
# on, in the same script, so a decision never reads usage without them and a
# part sent again, which the prologue skips, asks for none.
_METERING_SCRIPT = _BATCH_PROLOGUE + """
local i = 7
local h = tonumber(ARGV[i]); i = i + 1
for _ = 1, h do
  local key, index, f = KEYS[tonumber(ARGV[i])], KEYS[tonumber(ARGV[i + 1])], tonumber(ARGV[i + 2])
  i = i + 3
  for _ = 1, f do
    if ARGV[i + 2] == 'f' then
      redis.call('HINCRBYFLOAT', key, ARGV[i], ARGV[i + 1])
    else
      redis.call('HINCRBY', key, ARGV[i], ARGV[i + 1])
    end
    i = i + 3
  end
  redis.call('HSET', key, 'updated_at', ARGV[4])
  redis.call('SADD', index, key)
end
local d = tonumber(ARGV[i]); i = i + 1
for _ = 1, d do
  local key = KEYS[tonumber(ARGV[i])]
  redis.call('HINCRBY', key, ARGV[i + 1], ARGV[i + 2])
  redis.call('EXPIRE', key, ARGV[5])
  i = i + 3
end
local t = tonumber(ARGV[i]); i = i + 1
for _ = 1, t do
  redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[6], '*', 'key', ARGV[i])
  i = i + 1
end
return 1
"""

# Appends items to a work stream (RedisClient.append_batch), an entry each
# with the item in its ``item`` field. KEYS[2] is the stream; ARGV[4] its
# approximate MAXLEN (0: no cap); ARGV[5..] the items, oldest first. Returns
# how many old entries the cap removed.
_APPEND_SCRIPT = _BATCH_PROLOGUE + """
local before = redis.call('XLEN', KEYS[2])
local cap = tonumber(ARGV[4])
for i = 5, #ARGV do
  if cap > 0 then
    redis.call('XADD', KEYS[2], 'MAXLEN', '~', cap, '*', 'item', ARGV[i])
  else
    redis.call('XADD', KEYS[2], '*', 'item', ARGV[i])
  end
end
return math.max(0, before + (#ARGV - 4) - redis.call('XLEN', KEYS[2]))
"""

# One part of a failure log window (RedisClient.failures_batch). The kinds'
# indexes and hashes, the count hashes and their index are KEYS from 2 on,
# each named in ARGV by its position. ARGV[4] the retention in seconds,
# ARGV[5] each index's cap, ARGV[6] e, then per example: its index's
# position, its hash's position, its fingerprint, its description, its count,
# first and last (epoch ms), latency total and how many failures were timed;
# then c, then per count: its hash's position, its index's position, the
# count. An example is ranked by Redis's clock, the same for every gateway,
# so a page can ask what changed since it was read. Examples untouched for
# the retention, then the oldest past the cap, leave the index and the hash
# together.
_FAILURES_SCRIPT = _BATCH_PROLOGUE + """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local i = 6
local e = tonumber(ARGV[i]); i = i + 1
local touched = {}
for _ = 1, e do
  local index, data, fp = KEYS[tonumber(ARGV[i])], KEYS[tonumber(ARGV[i + 1])], ARGV[i + 2]
  local first, last = ARGV[i + 5], ARGV[i + 6]
  if redis.call('HSETNX', data, fp, ARGV[i + 3]) == 1 then
    redis.call('HSET', data, fp .. ':first', first, fp .. ':last', last)
  else
    if tonumber(first) < tonumber(redis.call('HGET', data, fp .. ':first') or first) then
      redis.call('HSET', data, fp .. ':first', first)
    end
    if tonumber(last) > tonumber(redis.call('HGET', data, fp .. ':last') or last) then
      redis.call('HSET', data, fp .. ':last', last)
    end
  end
  redis.call('HINCRBY', data, fp .. ':count', ARGV[i + 4])
  if tonumber(ARGV[i + 8]) > 0 then
    redis.call('HINCRBYFLOAT', data, fp .. ':latency', ARGV[i + 7])
    redis.call('HINCRBY', data, fp .. ':timed', ARGV[i + 8])
  end
  redis.call('ZADD', index, string.format('%.0f', now), fp)
  touched[index] = data
  i = i + 9
end
for index, data in pairs(touched) do
  local cutoff = string.format('%.0f', now - tonumber(ARGV[4]) * 1000)
  local gone = redis.call('ZRANGE', index, '-inf', '(' .. cutoff, 'BYSCORE')
  local over = redis.call('ZCARD', index) - #gone - tonumber(ARGV[5])
  if over > 0 then
    for _, fp in ipairs(redis.call('ZRANGE', index, #gone, #gone + over - 1)) do
      gone[#gone + 1] = fp
    end
  end
  for _, fp in ipairs(gone) do
    redis.call('HDEL', data, fp, fp .. ':count', fp .. ':first', fp .. ':last', fp .. ':latency', fp .. ':timed')
    redis.call('ZREM', index, fp)
  end
  redis.call('EXPIRE', index, ARGV[4])
  redis.call('EXPIRE', data, ARGV[4])
end
local c = tonumber(ARGV[i]); i = i + 1
for _ = 1, c do
  local key, index = KEYS[tonumber(ARGV[i])], KEYS[tonumber(ARGV[i + 1])]
  redis.call('HINCRBY', key, 'failure_count', ARGV[i + 2])
  redis.call('SADD', index, key)
  i = i + 3
end
return 1
"""


class BatchChannel(StrEnum):
    """The loop a batch part comes from: its sequence counts on its own, and it picks the part's script.

    One loop per channel per process: two would interleave their sequences,
    and a part one of them sent again could be skipped as applied.
    """

    METERING = "metering"  # a gateway's request metering (metering_batch)
    FAILURES = "failures"  # a gateway's failure log (failures_batch)
    SIGHTING_APPENDS = "sighting_appends"  # a gateway's sighting publisher (append_batch)
    OBSERVATION_APPENDS = "observation_appends"  # a process's IP observation recorder (append_batch)

    @property
    def script(self) -> str:
        """The Lua source this channel's parts run."""
        return _BATCH_SCRIPTS[self]


_BATCH_SCRIPTS: dict[BatchChannel, str] = {
    BatchChannel.METERING: _METERING_SCRIPT,
    BatchChannel.FAILURES: _FAILURES_SCRIPT,
    BatchChannel.SIGHTING_APPENDS: _APPEND_SCRIPT,
    BatchChannel.OBSERVATION_APPENDS: _APPEND_SCRIPT,
}

# The codes of the error replies Redis gives a batch script it never ran, so
# nothing of it was applied and its mark did not move. OOM is one because
# Redis checks memory at a script's first write, which is the mark.
BATCH_REFUSALS = frozenset({"BUSY", "OOM", "NOSCRIPT", "LOADING", "READONLY", "MASTERDOWN", "MISCONF"})


class BatchId(NamedTuple):
    """Which part a batch part is: its channel, and its sequence there, from 1 in the order parts are prepared."""

    channel: BatchChannel
    sequence: int


class BatchOutcome(StrEnum):
    """What became of one part of a send Redis answered."""

    APPLIED = "applied"  # the script ran now
    REPEATED = "repeated"  # an earlier send of the part was applied; nothing was done again
    # Redis never ran the script (BATCH_REFUSALS). The part goes again
    # renumbered: under its own sequence it would be skipped once a later one ran.
    REFUSED = "refused"
    # The script failed part-way, after its mark moved: the part is dropped,
    # since a resend would be skipped anyway.
    FAILED = "failed"


class BatchReply(NamedTuple):
    """Redis's answer for one part of a send."""

    outcome: BatchOutcome
    # The script's reply when it ran now (an append: how many old entries its
    # cap removed); 0 otherwise.
    value: int = 0

    @classmethod
    def of(cls, reply: Any) -> "BatchReply":
        """Class one script reply of a pipeline executed with ``raise_on_error=False``."""
        if isinstance(reply, Exception):
            return cls(BatchOutcome.REFUSED if cls._code(reply) in BATCH_REFUSALS else BatchOutcome.FAILED)
        value = int(reply)
        return cls(BatchOutcome.REPEATED) if value < 0 else cls(BatchOutcome.APPLIED, value)

    @property
    def landed(self) -> bool:
        """Whether the part's writes are in Redis, from this send or an earlier one."""
        return self.outcome in (BatchOutcome.APPLIED, BatchOutcome.REPEATED)

    @staticmethod
    def _code(error: Exception) -> str:
        """An error reply's code: redis-py keeps the codes it maps to a class in ``status_code``, the rest start the message."""
        code = getattr(error, "status_code", None)
        return code if isinstance(code, str) and code else str(error).partition(" ")[0]


@dataclass(frozen=True)
class BatchPart:
    """One script call of a batch, fixed when it is prepared: sent again, it is the same part, and Redis applies it once."""

    id: BatchId
    # The keys the script writes, after the process's applied hash (KEYS[1]).
    keys: tuple[str, ...]
    # Its arguments, after the identity (ARGV[1..3]).
    args: tuple[str, ...]
    # What it carries, for its writer's counts: window hashes and demand
    # counts, stream items, or examples and counts.
    items: int


class _PartKeys:
    """The KEYS of one batch part as it is built: each key once, named in ARGV by its position."""

    def __init__(self, *keys: str) -> None:
        self.keys: list[str] = []
        self._positions: dict[str, str] = {}
        for key in keys:
            self.position(key)

    def position(self, key: str) -> str:
        """``key``'s KEYS position as a script argument, adding it the first time; KEYS[1] is the applied hash."""
        found = self._positions.get(key)
        if found is None:
            self.keys.append(key)
            found = self._positions[key] = str(len(self.keys) + 1)
        return found


class FailureCursor(NamedTuple):
    """Where a failure log example stands: its rank (when Redis last took an addition to it, epoch ms), kind and fingerprint.

    Pages order by the three, highest first.
    """

    rank: int
    kind: str
    fingerprint: str


@dataclass(frozen=True)
class FailureLogRow:
    """One failure log example as read back: its description (the window that made it) and what every gateway added up."""

    cursor: FailureCursor
    fields: dict[str, Any]
    count: int
    first_ms: int
    last_ms: int
    latency_ms: float | None


class StreamId(NamedTuple):
    """A stream entry ID, ``<milliseconds>-<sequence>``, as numbers that order as Redis orders IDs."""

    ms: int
    seq: int

    @classmethod
    def of(cls, text: str) -> "StreamId":
        """Parse an ID; ValueError for a malformed one."""
        ms, seq = text.split("-")
        return cls(int(ms), int(seq))

    def before(self) -> "StreamId | None":
        """The greatest ID below this one, an exclusive upper bound; None below 0-0."""
        if self.seq > 0:
            return StreamId(self.ms, self.seq - 1)
        if self.ms > 0:
            return StreamId(self.ms - 1, 2**64 - 1)
        return None

    def __str__(self) -> str:
        return f"{self.ms}-{self.seq}"


def is_missing_key(reply: object) -> bool:
    """Whether a reply is the error Redis answers a command on a key that does not exist with."""
    return isinstance(reply, ResponseError) and str(reply).endswith("no such key")


class StreamInfo(NamedTuple):
    """What ``XINFO STREAM`` says of a stream (``entries-added`` needs Redis 7.0; Octoprox requires 7.2)."""

    length: int
    last_id: str
    entries_added: int
    first_id: str | None

    @classmethod
    def of(cls, reply: Any) -> "StreamInfo | None":
        """The info in a reply read with ``raise_on_error=False``; None for a missing stream, any other error raised."""
        if is_missing_key(reply):
            return None
        if isinstance(reply, Exception):
            raise reply
        first = reply.get("first-entry")
        return cls(
            length=int(reply["length"]),
            last_id=str(reply["last-generated-id"]),
            entries_added=int(reply["entries-added"]),
            first_id=str(first[0]) if first else None,
        )


def _decode(value: str | bytes) -> str:
    return value if isinstance(value, str) else value.decode()


def failure_log_keys(project_id: str, kinds: Iterable[str]) -> list[str]:
    """Every key of a project's failure log for ``kinds``: each kind's index and its examples' hash."""
    return [
        key
        for kind in kinds
        for key in (
            FAILURE_LOG_KEY.format(project_id=project_id, kind=kind),
            FAILURE_LOG_DATA_KEY.format(project_id=project_id, kind=kind),
        )
    ]


def tunnel_carrier_key(protocol: str, instance_id: str) -> str:
    """The peer status hash the gateway ``instance_id`` publishes for ``protocol``; its expiry says the carrier stopped."""
    return TUNNEL_PEER_STATUS_KEY.format(protocol=protocol, instance_id=instance_id)


def project_keys(project_id: str) -> list[str]:
    """The Redis keys a project owns that outlive it unless deleted: its MITM captures and its failure log of every kind.

    Every other key naming the project or its entities expires, or goes with the next metrics flush.
    """
    return [MITM_REQUESTS_KEY.format(project_id=project_id), *failure_log_keys(project_id, FailureKind)]


def classify_key(key: str) -> str:
    """The :data:`REDIS_KEY_GROUPS` label a Redis key belongs to; a key without the prefix is an earlier release's."""
    if not key.startswith(KEY_PREFIX):
        return EARLIER_RELEASES_KEY_GROUP
    for prefix, label in REDIS_KEY_GROUPS:
        if key.startswith(prefix):
            return label
    return OTHER_KEY_GROUP


def masked_url(url: str) -> str:
    """``url`` with its password replaced by ``***``, fit for a log line.

    redis-py takes a password from the user part (``redis://:secret@host``)
    and from a ``password`` query parameter, so both are masked.
    """
    parts = urlsplit(url)
    netloc = parts.netloc
    if parts.password is not None:
        userinfo, _, hostinfo = netloc.rpartition("@")
        netloc = f"{userinfo.partition(':')[0]}:***@{hostinfo}"
    query = "&".join(
        "password=***" if param.partition("=")[0] == "password" else param
        for param in parts.query.split("&")
    )
    return urlunsplit(parts._replace(netloc=netloc, query=query))


class MetricWindowKind[E, C: WindowCounts](ABC):
    """One kind of metrics window: a Redis hash of counts per entity, listed in the kind's index for the flush to find.

    Args:
        name: the kind's name in its index key.
        key_format: the format of its windows' keys, one of this module's.
        counts: the type a window's fields read as.
    """

    def __init__(self, name: str, key_format: str, counts: type[C]) -> None:
        self.name = name
        self.key_format = key_format
        self.prefix = _literal_prefix(key_format)
        self.index = METRICS_INDEX_KEY.format(kind=name)
        self.counts = counts

    @abstractmethod
    def key(self, entity: E) -> str:
        """The key of ``entity``'s window."""

    @abstractmethod
    def entity(self, key: str) -> E | None:
        """The entity whose window ``key`` is; None for a key this kind cannot read."""

    def read(self, values: Any) -> C:
        """A window's counts from its fields; TypeError or ValueError when they read as none."""
        return self.counts.from_dict(values)

    def flushes(self, counts: C) -> bool:
        """Whether a flush has anything to write for ``counts``: by default always."""
        return True

    def add(self, pipe: Any, key: str, counts: C, now_iso: str) -> None:
        """Queue ``counts`` onto ``key``'s window as metering adds them: the increments, ``updated_at``, then the index."""
        for name, amount, fraction in counts.increments():
            if fraction:
                pipe.hincrbyfloat(key, name, amount)
            else:
                pipe.hincrby(key, name, amount)
        pipe.hset(key, "updated_at", now_iso)
        # After the increments, so the take can never unlist a hash this
        # write has just recreated (see METRICS_INDEX_KEY).
        pipe.sadd(self.index, key)


class EntityWindows(MetricWindowKind[str, MetricDelta]):
    """Windows keyed by one entity's ID: a proxy's, a project's, a connector's or a tunnel device's."""

    def key(self, entity: str) -> str:
        """The key of the window of the entity with ID ``entity``."""
        return self.prefix + entity

    def entity(self, key: str) -> str | None:
        """The ID after the kind's prefix; None for a key without the prefix."""
        return key.removeprefix(self.prefix) if key.startswith(self.prefix) else None


class TotalledWindows(EntityWindows):
    """Windows of an entity that also keeps running totals for all time: a proxy, a project or a tunnel device."""


class HostWindows(MetricWindowKind[HostKey, MetricDelta]):
    """Windows keyed by (project_id, connector_id, host): a project's requests to one host through one connector."""

    def key(self, entity: HostKey) -> str:
        """The key of the window of ``entity``."""
        project_id, connector_id, host = entity
        return self.key_format.format(project_id=project_id, connector_id=connector_id, host=host)

    def entity(self, key: str) -> HostKey | None:
        """(project_id, connector_id, host) from ``key``; None for a key without all three."""
        if not key.startswith(self.prefix):
            return None
        parts = key.removeprefix(self.prefix).split(":", 2)
        if len(parts) != 3 or not all(parts):
            return None
        return parts[0], parts[1], parts[2]


class FailureWindows(MetricWindowKind[FailureCountKey, FailureCount]):
    """Failure count windows, keyed by ``FailureCountKey`` and added to by the failure log."""

    def key(self, entity: FailureCountKey) -> str:
        """The key of the window of ``entity``."""
        project_id, connector_id, kind, upstream_status = entity
        return self.key_format.format(
            project_id=project_id,
            connector_id=connector_id or "-",
            kind=kind,
            upstream_status="-" if upstream_status is None else upstream_status,
        )

    def entity(self, key: str) -> FailureCountKey | None:
        """The failure count key ``key`` names; None for a malformed one."""
        if not key.startswith(self.prefix):
            return None
        parts = key.removeprefix(self.prefix).split(":")
        if len(parts) != 4 or not all(parts):
            return None
        project_id, connector_id, kind, status = parts
        try:
            upstream_status = None if status == "-" else int(status)
        except ValueError:
            return None
        return project_id, None if connector_id == "-" else connector_id, kind, upstream_status

    def flushes(self, counts: FailureCount) -> bool:
        """Whether the count is above zero: a count at zero or below has nothing to write."""
        return counts.failure_count > 0

    def add(self, pipe: Any, key: str, counts: FailureCount, now_iso: str) -> None:
        """Queue ``counts`` onto ``key``'s window as the failure log adds them: the count, then the index."""
        pipe.hincrby(key, "failure_count", counts.failure_count)
        pipe.sadd(self.index, key)


# The kinds of metrics window, in the order a flush takes them.
PROXY_WINDOWS = TotalledWindows("proxy", PROXY_METRICS_KEY, MetricDelta)
PROJECT_WINDOWS = TotalledWindows("project", PROJECT_METRICS_KEY, MetricDelta)
CONNECTOR_WINDOWS = EntityWindows("connector", CONNECTOR_METRICS_KEY, MetricDelta)
TUNNEL_PEER_WINDOWS = TotalledWindows("tunnel_peer", TUNNEL_PEER_METRICS_KEY, TunnelPeerMetricDelta)
HOST_WINDOWS = HostWindows("host", HOST_METRICS_KEY, MetricDelta)
FAILURE_WINDOWS = FailureWindows("failure", FAILURE_METRICS_KEY, FailureCount)
METRIC_WINDOW_KINDS: tuple[MetricWindowKind[Any, Any], ...] = (
    PROXY_WINDOWS,
    PROJECT_WINDOWS,
    CONNECTOR_WINDOWS,
    TUNNEL_PEER_WINDOWS,
    HOST_WINDOWS,
    FAILURE_WINDOWS,
)


def window_kind(key: str) -> MetricWindowKind[Any, Any] | None:
    """The kind of metrics window ``key`` is, by its prefix; None for a key that is no metrics window."""
    return next((kind for kind in METRIC_WINDOW_KINDS if key.startswith(kind.prefix)), None)


@dataclass
class MetricWindows:
    """Metrics windows by kind and entity: what a gateway's metering and failure log add to Redis, and what a flush takes."""

    proxies: dict[str, MetricDelta] = field(default_factory=dict)
    projects: dict[str, MetricDelta] = field(default_factory=dict)
    connectors: dict[str, MetricDelta] = field(default_factory=dict)
    tunnel_peers: dict[str, TunnelPeerMetricDelta] = field(default_factory=dict)
    # Keyed by (project_id, connector_id, host).
    hosts: dict[HostKey, MetricDelta] = field(default_factory=dict)
    failures: dict[FailureCountKey, FailureCount] = field(default_factory=dict)

    @classmethod
    def of(cls, staged: Mapping[str, str]) -> "MetricWindows":
        """The windows of a staging hash, each window's key to its fields as JSON; one it cannot read is logged and left out."""
        windows = cls()
        for key, text in staged.items():
            try:
                windows._add(key, json.loads(text))
            except (TypeError, ValueError):
                logger.warning("Unreadable metrics window left out of the flush", key=key)
        return windows

    def __len__(self) -> int:
        """How many windows there are, of every kind."""
        return sum(len(windows) for _kind, windows in self.by_kind())

    @property
    def empty(self) -> bool:
        """Whether there is no window of any kind."""
        return not len(self)

    def by_kind(self) -> tuple[tuple[MetricWindowKind[Any, Any], dict[Any, Any]], ...]:
        """Each kind of window with its windows by entity, in ``METRIC_WINDOW_KINDS`` order."""
        return (
            (PROXY_WINDOWS, self.proxies),
            (PROJECT_WINDOWS, self.projects),
            (CONNECTOR_WINDOWS, self.connectors),
            (TUNNEL_PEER_WINDOWS, self.tunnel_peers),
            (HOST_WINDOWS, self.hosts),
            (FAILURE_WINDOWS, self.failures),
        )

    def keyed(self) -> Iterator[tuple[MetricWindowKind[Any, Any], str, WindowCounts]]:
        """Each window with its kind and the key of its live hash, kind by kind."""
        for kind, windows in self.by_kind():
            for entity, counts in windows.items():
                yield kind, kind.key(entity), counts

    def _add(self, key: str, values: Any) -> None:
        """File one window under its kind unless it has nothing to write; ValueError or TypeError for no window."""
        kind = window_kind(key)
        entity = None if kind is None else kind.entity(key)
        if kind is None or entity is None:
            raise ValueError(f"not a metrics window: {key}")
        counts = kind.read(values)
        if kind.flushes(counts):
            dict(self.by_kind())[kind][entity] = counts


class RedisClient:
    """One process's Redis: its connection pool, the multi-key operations the roles share, and its batch identity.

    ``name`` (the node passes its instance id) starts ``writer_id``; a
    connect is bounded by ``request_timeout`` unless ``socket_connect_timeout``
    is given.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        name: str = "process",
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        socket_timeout: float = REPLY_TIMEOUT_SECONDS,
        socket_connect_timeout: float | None = None,
        health_check_interval: int = IDLE_CONNECTION_CHECK_SECONDS,
    ) -> None:
        self._redis_url = redis_url
        self._request_timeout = request_timeout
        self._socket_timeout = socket_timeout
        self._socket_connect_timeout = request_timeout if socket_connect_timeout is None else socket_connect_timeout
        self._health_check_interval = health_check_interval
        self._client: redis.Redis | None = None
        self._prune_script: AsyncScript | None = None
        self._take_script: AsyncScript | None = None
        # Whose applied marks this client's batches move (APPLIED_BATCHES_KEY).
        # The token keeps a restarted process, which may reuse its instance id,
        # off its predecessor's marks while its sequences start again at 1.
        self.writer_id = f"{name}:{secrets.token_hex(4)}"
        # The last sequence handed out per channel (BatchId).
        self._sequences: Counter[BatchChannel] = Counter()
        # Each batch script's SHA1 by source, so a send is one EVALSHA round
        # trip; dropped when Redis answers NOSCRIPT.
        self._script_shas: dict[str, str] = {}

    @property
    def socket_timeout(self) -> float:
        """Seconds one reply may take before its command fails: what a blocking read must stay under."""
        return self._socket_timeout

    def request_deadline(self) -> asyncio.Timeout:
        """A ``request_timeout`` deadline for one call on the request path or after a commit; TimeoutError when it passes.

        redis-py closes the connection of a command it cuts, so no later
        command reads the late reply.
        """
        return asyncio.timeout(self._request_timeout)

    async def after_commit(self, step: Callable[[], Awaitable[object]], what: str, **context: object) -> bool:
        """Run ``step``, one Redis step of a committed write, under the request deadline; False, logged, when it fails.

        The log line names the step by ``what`` and carries ``context``.
        """
        try:
            async with self.request_deadline():
                await step()
        except Exception as exc:
            logger.warning("Redis step after a write failed", step=what, error=error_text(exc), **context)
            return False
        return True

    async def connect(self) -> None:
        """Connect to Redis."""
        self._client = redis.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=self._socket_timeout,
            socket_connect_timeout=self._socket_connect_timeout,
            health_check_interval=self._health_check_interval,
            max_connections=POOL_MAX_CONNECTIONS,
            # Never sent twice by redis-py: a command whose reply was lost may
            # have been applied, and only its caller knows whether a resend is
            # safe. from_url happens to default to this; it is said here.
            retry=Retry(NoBackoff(), 0),
            # On RESP3, redis-py 8's default, each new connection would also
            # ask for maintenance notifications, which only Redis Enterprise
            # and Redis Cloud send: open-source Redis refuses the command and
            # counts an error per connection.
            maint_notifications_config=MaintNotificationsConfig(enabled=False),
        )
        # The URL can carry the password, and logs travel further than secrets should.
        logger.info("Connected to Redis", url=masked_url(self._redis_url))

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.aclose()
            logger.info("Closed Redis connection")

    @property
    def client(self) -> redis.Redis:
        """Get Redis client, raising if not connected."""
        if self._client is None:
            raise RuntimeError("Redis client not connected")
        return self._client

    async def read_hashes(self, keys: Sequence[str]) -> list[dict[str, Any]]:
        """Each of ``keys`` read whole, in order, in pipelined batches: a round trip per batch, not per key.

        A key that does not exist reads as empty.
        """
        hashes: list[dict[str, Any]] = []
        for start in range(0, len(keys), _HASH_BATCH):
            pipe = self.client.pipeline(transaction=False)
            for key in keys[start:start + _HASH_BATCH]:
                pipe.hgetall(key)
            hashes.extend(await pipe.execute())
        return hashes

    async def _read_listed_hashes(
        self, index: str, key_of: Callable[[str], str]
    ) -> list[tuple[str, dict[str, Any]]]:
        """The hash of each member of ``index`` (at ``key_of(member)``) that still exists, as ``(key, fields)``.

        Members whose hash is gone leave the index (best effort: a prune that
        fails is logged and left to the next reader, so it never costs the
        read that found the members gone).
        """
        members = [_decode(member) for member in await self.client.smembers(index)]
        keys = [key_of(member) for member in members]
        found = await self.read_hashes(keys)
        gone = [(member, key) for member, key, data in zip(members, keys, found, strict=True) if not data]
        if gone:
            try:
                await self._prune(index, gone)
            except Exception:
                logger.warning("Index prune failed; left to the next reader", index=index, exc_info=True)
        return [(key, data) for key, data in zip(keys, found, strict=True) if data]

    async def prune_index(self, index: str, key_of: Callable[[str], str]) -> int:
        """Drop the members of ``index`` whose key (``key_of(member)``) is gone, in two round trips; how many went.

        For an index whose members are found through a key that expires, such
        as the instance and tunnel carrier indexes.
        """
        members = [_decode(member) for member in await self.client.smembers(index)]
        if not members:
            return 0
        return await self._prune(index, [(member, key_of(member)) for member in members])

    async def _prune(self, index: str, members: list[tuple[str, str]]) -> int:
        """Drop each ``(member, key)`` from ``index`` whose key is still gone when Redis checks; how many went."""
        if self._prune_script is None:
            self._prune_script = self.client.register_script(_PRUNE_INDEX_SCRIPT)
        removed = 0
        for start in range(0, len(members), _HASH_BATCH):
            args = [part for pair in members[start:start + _HASH_BATCH] for part in pair]
            removed += int(await self._prune_script(keys=[index], args=args))
        return removed

    # Batches: the writes a failure sends again, prepared as parts that carry
    # an identity (the *_batch methods, no I/O) and sent with send_batch.

    def metering_batch(
        self,
        windows: MetricWindows,
        demand: Mapping[DemandCountKey, int] | None = None,
        demand_ttl_seconds: int | None = None,
        *,
        metered: Collection[str] = (),
    ) -> list[BatchPart]:
        """A metering window as parts of at most ``_HASH_BATCH`` hashes; [] when it holds nothing.

        The first part carries every connector window, every demand count and a ``traffic`` job entry for each
        connector of ``metered`` whose window carried bytes, so no decision is asked before the bytes it is about.

        Args:
            metered: the ids of the connectors with a traffic limit; one whose window moved no byte asks for nothing.
        """
        now = utc_now().isoformat()
        traffic = [
            connector_id
            for connector_id, delta in windows.connectors.items()
            if connector_id in metered and (delta.bytes_sent or delta.bytes_received)
        ]
        # Connectors first, so the first part carries every one of them.
        keyed = sorted(windows.keyed(), key=lambda window: window[0] is not CONNECTOR_WINDOWS)
        first_size = max(_HASH_BATCH, len(windows.connectors))
        counts = [
            (DEMAND_KEY.format(project_id=project_id, minute=minute), field, count)
            for (project_id, minute, field), count in (demand or {}).items()
            if count
        ]
        if not keyed and not counts:
            return []
        if counts and demand_ttl_seconds is None:
            raise ValueError("demand counts need demand_ttl_seconds")
        chunks = [keyed[:first_size]] + [
            keyed[start:start + _HASH_BATCH] for start in range(first_size, len(keyed), _HASH_BATCH)
        ]
        return [
            self._metering_part(
                chunk, counts if n == 0 else [], traffic if n == 0 else [], now, demand_ttl_seconds or 0
            )
            for n, chunk in enumerate(chunks)
        ]

    def _metering_part(
        self,
        windows: list[tuple[MetricWindowKind[Any, Any], str, WindowCounts]],
        counts: list[tuple[str, str, int]],
        traffic: list[str],
        now_iso: str,
        demand_ttl_seconds: int,
    ) -> BatchPart:
        """One part of :meth:`metering_batch`: windows (kind, key, counts), demand counts (hash, field, count), then the connectors whose traffic it asks a decision on."""
        keys = _PartKeys(JOB_STREAM_KEY.format(kind=JobKind.TRAFFIC))
        args = [now_iso, str(demand_ttl_seconds), str(JOB_STREAM_MAXLEN)]
        args.append(str(len(windows)))
        for kind, key, delta in windows:
            increments = list(delta.increments())
            args += [keys.position(key), keys.position(kind.index), str(len(increments))]
            for name, amount, fraction in increments:
                args += [name, str(amount), "f" if fraction else "i"]
        args.append(str(len(counts)))
        for key, name, count in counts:
            args += [keys.position(key), name, str(count)]
        args += [str(len(traffic)), *traffic]
        sequence = self._next_id(BatchChannel.METERING)
        return BatchPart(sequence, tuple(keys.keys), tuple(args), len(windows) + len(counts))

    def failures_batch(
        self,
        examples: Sequence[FailureExample],
        counts: Mapping[FailureCountKey, int],
        *,
        max_entries: int,
        retention_seconds: int,
    ) -> list[BatchPart]:
        """A failure log window, its examples and counts, as parts; [] when it holds nothing."""
        if not examples and not counts:
            return []
        tallies = list(counts.items())
        parts = []
        for start in range(0, len(examples) + len(tallies), _HASH_BATCH):
            stop = start + _HASH_BATCH
            keys = _PartKeys()
            chunk = examples[start:stop]
            args = [str(retention_seconds), str(max_entries), str(len(chunk))]
            for example in chunk:
                where = {"project_id": example.project_id, "kind": example.kind}
                args += [
                    keys.position(FAILURE_LOG_KEY.format(**where)),
                    keys.position(FAILURE_LOG_DATA_KEY.format(**where)),
                    example.fingerprint,
                    example.fields,
                    str(example.count),
                    str(example.first_ms),
                    str(example.last_ms),
                    str(example.latency_total),
                    str(example.latency_n),
                ]
            chunk_counts = tallies[max(0, start - len(examples)):max(0, stop - len(examples))]
            args.append(str(len(chunk_counts)))
            for count_key, count in chunk_counts:
                args += [
                    keys.position(FAILURE_WINDOWS.key(count_key)),
                    keys.position(FAILURE_WINDOWS.index),
                    str(count),
                ]
            parts.append(
                BatchPart(
                    self._next_id(BatchChannel.FAILURES), tuple(keys.keys), tuple(args), len(chunk) + len(chunk_counts)
                )
            )
        return parts

    def append_batch(
        self, channel: BatchChannel, key: str, items: Sequence[str], *, max_items: int = 0
    ) -> list[BatchPart]:
        """``items``, oldest first, to append to the stream at ``key`` as one part, an entry each; [] for none.

        The stream is trimmed to about its newest ``max_items`` entries (0: no cap); ValueError for a channel that does
        not append.
        """
        if channel.script is not _APPEND_SCRIPT:
            raise ValueError(f"{channel} is not an append channel")
        if not items:
            return []
        return [BatchPart(self._next_id(channel), (key,), (str(max_items), *items), len(items))]

    def renumber(self, part: BatchPart) -> BatchPart:
        """``part`` under its channel's next sequence: how a part Redis refused goes again."""
        return BatchPart(self._next_id(part.id.channel), part.keys, part.args, part.items)

    def _next_id(self, channel: BatchChannel) -> BatchId:
        """The next identity of ``channel``."""
        self._sequences[channel] += 1
        return BatchId(channel, self._sequences[channel])

    async def send_batch(self, parts: Sequence[BatchPart]) -> list[BatchReply]:
        """Send ``parts`` in one round trip and class Redis's answer for each; raises when no answer came.

        After a raise any part may have been applied: send them again
        unchanged, before anything newer of their channels.
        """
        if not parts:
            return []
        applied = APPLIED_BATCHES_KEY.format(writer=self.writer_id)
        try:
            shas = {source: await self._script_sha(source) for source in {part.id.channel.script for part in parts}}
            pipe = self.client.pipeline(transaction=False)
            for part in parts:
                channel, sequence = part.id
                pipe.evalsha(
                    shas[channel.script], len(part.keys) + 1, applied, *part.keys,
                    channel.value, str(sequence), str(APPLIED_BATCHES_TTL_SECONDS), *part.args,
                )
            replies = await pipe.execute(raise_on_error=False)
        except (Exception, asyncio.CancelledError) as exc:
            # A caller's deadline cuts a send as a lost connection does.
            first = parts[0].id
            logger.warning(
                "Redis batch not confirmed; sending it again",
                channel=first.channel.value,
                sequence=first.sequence,
                parts=len(parts),
                error=error_text(exc),
            )
            raise
        for part, reply in zip(parts, replies, strict=True):
            if isinstance(reply, NoScriptError):
                self._script_shas.pop(part.id.channel.script, None)
        answers = [BatchReply.of(reply) for reply in replies]
        self._log_unapplied(parts, answers, replies)
        return answers

    async def _script_sha(self, source: str) -> str:
        """The SHA1 Redis knows a batch script by, loading it the first time and after a NOSCRIPT."""
        sha = self._script_shas.get(source)
        if sha is None:
            sha = self._script_shas[source] = str(await self.client.script_load(source))
        return sha

    @staticmethod
    def _log_unapplied(parts: Sequence[BatchPart], answers: Sequence[BatchReply], replies: Sequence[Any]) -> None:
        """Log the parts Redis refused and the parts whose script failed, once each per send."""
        sent = list(zip(parts, answers, replies, strict=True))
        refused = [(part, reply) for part, answer, reply in sent if answer.outcome is BatchOutcome.REFUSED]
        failed = [(part, reply) for part, answer, reply in sent if answer.outcome is BatchOutcome.FAILED]
        if refused:
            part, error = refused[0]
            logger.warning(
                "Redis batch refused; sending its content again",
                channel=part.id.channel.value,
                sequence=part.id.sequence,
                parts=len(refused),
                error=error_text(error),
            )
        if failed:
            part, error = failed[0]
            logger.error(
                "Redis batch dropped: its script failed",
                channel=part.id.channel.value,
                sequence=part.id.sequence,
                parts=len(failed),
                error=error_text(error),
            )

    async def stream_infos(self, keys: Sequence[str]) -> list[StreamInfo | None]:
        """``XINFO STREAM`` of each key in one round trip; None for a stream that does not exist."""
        pipe = self.client.pipeline(transaction=False)
        for key in keys:
            pipe.xinfo_stream(key)
        return [StreamInfo.of(reply) for reply in await pipe.execute(raise_on_error=False)]

    async def stream_head(self, key: str) -> str | None:
        """The last entry ID the stream at ``key`` handed out; None when there is no stream."""
        (info,) = await self.stream_infos([key])
        return None if info is None else info.last_id

    # Sticky-session bindings (project-scoped): the sticky strategy keeps a
    # session on its proxy across gateways. On the request path, so each
    # call takes the request deadline and its caller fails open.
    async def get_sticky_binding(self, project_id: str, session_id: str) -> str | None:
        key = STICKY_BINDING_KEY.format(project_id=project_id, session_id=session_id)
        async with self.request_deadline():
            result = await self.client.get(key)
        return None if result is None else _decode(result)

    async def set_sticky_binding(
        self, project_id: str, session_id: str, proxy_id: str, ttl_seconds: int = 300
    ) -> None:
        key = STICKY_BINDING_KEY.format(project_id=project_id, session_id=session_id)
        async with self.request_deadline():
            await self.client.set(key, proxy_id, ex=ttl_seconds)

    # MITM request recording operations
    async def record_mitm_request(
        self,
        project_id: str,
        fields: dict[str, str],
    ) -> None:
        """Append a MITM request record to the project's capped stream, under the request deadline.

        Uses XADD with approximate MAXLEN to keep the last ~1000 entries.
        """
        key = MITM_REQUESTS_KEY.format(project_id=project_id)
        async with self.request_deadline():
            await self.client.xadd(key, fields, maxlen=1000, approximate=True)  # type: ignore[arg-type]

    async def get_mitm_requests(
        self,
        project_id: str,
        count: int = 50,
        before_id: str | None = None,
    ) -> list[dict[str, str]]:
        """At most ``count`` of the project's MITM records, newest first, each its fields and its stream entry ``id``.

        ``before_id``, when given, pages to the records older than that entry.
        """
        key = MITM_REQUESTS_KEY.format(project_id=project_id)
        return await self._stream_page(key, count, before_id)

    async def _stream_page(self, key: str, count: int, before_id: str | None) -> list[dict[str, str]]:
        """Newest-first page of a stream, strictly older than ``before_id`` when given."""
        if before_id:
            before = StreamId.of(before_id).before()
            if before is None:
                return []  # nothing is older than 0-0
            end = str(before)
        else:
            end = "+"

        raw = await cast(
            "Awaitable[list[tuple[str, dict[str, str]]]]",
            self.client.xrevrange(key, max=end, min="-", count=count),
        )
        return [{"id": entry_id, **fields} for entry_id, fields in raw]

    async def clear_mitm_requests(self, project_id: str) -> None:
        """Delete all MITM request records for a project."""
        key = MITM_REQUESTS_KEY.format(project_id=project_id)
        await self.client.delete(key)

    # Failure log operations

    async def get_failures(
        self,
        project_id: str,
        kinds: list[str],
        count: int = 200,
        after: FailureCursor | None = None,
    ) -> list[FailureLogRow]:
        """The examples across the kinds' indexes, by (rank, kind, fingerprint), the ones a gateway added to last first.

        ``after`` is the previous page's last example: this page holds what
        follows it. An example a gateway adds to meanwhile moves up, out of
        the older pages, and is counted as changed (``failures_changed``).
        """
        indexes = [(kind, FAILURE_LOG_KEY.format(project_id=project_id, kind=kind)) for kind in kinds]
        top: int | str = "+inf"
        ties = [0] * len(indexes)
        if after is not None:
            top = after.rank
            # Every example ranked with the cursor is read past, however many share its rank.
            pipe = self.client.pipeline(transaction=False)
            for _kind, index in indexes:
                pipe.zcount(index, after.rank, after.rank)
            ties = [int(n) for n in await pipe.execute()]
        pipe = self.client.pipeline(transaction=False)
        for (_kind, index), tied in zip(indexes, ties, strict=True):
            pipe.zrange(index, top, "-inf", desc=True, withscores=True, byscore=True, offset=0, num=count + tied)
        ranked = [
            FailureCursor(int(score), kind, _decode(member))
            for (kind, _index), raw in zip(indexes, await pipe.execute(), strict=True)
            for member, score in raw
        ]
        if after is not None:
            ranked = [cursor for cursor in ranked if cursor < after]
        ranked = sorted(ranked, reverse=True)[:count]
        pipe = self.client.pipeline(transaction=False)
        for cursor in ranked:
            fp = cursor.fingerprint
            pipe.hmget(
                FAILURE_LOG_DATA_KEY.format(project_id=project_id, kind=cursor.kind),
                [fp, f"{fp}:count", f"{fp}:first", f"{fp}:last", f"{fp}:latency", f"{fp}:timed"],
            )
        rows = []
        for cursor, (description, n, first, last, latency, timed) in zip(ranked, await pipe.execute(), strict=True):
            # None: trimmed between the two reads.
            if description is not None:
                rows.append(FailureLogRow(
                    cursor=cursor,
                    fields=json.loads(description),
                    count=int(n or 0),
                    first_ms=int(first or 0),
                    last_ms=int(last or 0),
                    latency_ms=round(float(latency) / int(timed), 1) if latency and timed and int(timed) else None,
                ))
        return rows

    async def count_failures(self, project_id: str, kinds: list[str]) -> int:
        """How many examples the kinds hold."""
        pipe = self.client.pipeline(transaction=False)
        for kind in kinds:
            pipe.zcard(FAILURE_LOG_KEY.format(project_id=project_id, kind=kind))
        return sum(int(n) for n in await pipe.execute())

    async def failures_changed(self, project_id: str, kinds: list[str], since: int) -> int:
        """How many of the kinds' examples a gateway added to, new ones included, after the rank ``since``."""
        pipe = self.client.pipeline(transaction=False)
        for kind in kinds:
            pipe.zcount(FAILURE_LOG_KEY.format(project_id=project_id, kind=kind), f"({since}", "+inf")
        return sum(int(n) for n in await pipe.execute())

    async def clear_failures(self, project_id: str, kinds: Iterable[str]) -> None:
        """Delete a project's failure log for the kinds, in one DEL. Counts in history stay."""
        await self.client.delete(*failure_log_keys(project_id, kinds))

    # The metrics take: a flush moves every live window into a staging hash
    # named by its job entry ID, commits what it took with that ID, and then
    # clears the staging hash, or puts it back when the flush is given up. A
    # tunnel device that goes takes its live window with it.

    async def take_metric_windows(self, message_id: str) -> MetricWindows:
        """Move every live metrics window into the staging hash of flush ``message_id``; every window staged under it.

        A flush delivered again moves only the windows not staged yet, and
        reads back all it holds, so its commit writes each window once.
        """
        staging = METRICS_TAKEN_KEY.format(message_id=message_id)
        indexes = [kind.index for kind in METRIC_WINDOW_KINDS]
        listing = self.client.pipeline(transaction=False)
        for index in indexes:
            listing.smembers(index)
        listed = await listing.execute()
        if self._take_script is None:
            self._take_script = self.client.register_script(_TAKE_SCRIPT)
        pipe = self.client.pipeline(transaction=False)
        for index, members in zip(indexes, listed, strict=True):
            keys = [_decode(member) for member in members]
            for start in range(0, len(keys), _HASH_BATCH):
                await self._take_script(keys=[staging, index, *keys[start:start + _HASH_BATCH]], client=pipe)
        pipe.expire(staging, METRICS_TAKEN_TTL_SECONDS)
        await pipe.execute()
        return await self._staged_windows(staging)

    async def put_back_metric_windows(self, message_id: str) -> None:
        """Add the windows staged under ``message_id`` back onto the live ones, as metering writes them, and delete the staging hash.

        One MULTI, for a flush given up before its commit: each window is
        listed in its index again, so the next flush takes it.
        """
        staging = METRICS_TAKEN_KEY.format(message_id=message_id)
        windows = await self._staged_windows(staging)
        now = utc_now().isoformat()
        pipe = self.client.pipeline(transaction=True)
        for kind, key, counts in windows.keyed():
            kind.add(pipe, key, counts, now)
        pipe.delete(staging)
        await pipe.execute()

    async def clear_metric_windows(self, message_id: str) -> None:
        """Delete the staging hash of flush ``message_id``, once Postgres holds what it took."""
        await self.client.delete(METRICS_TAKEN_KEY.format(message_id=message_id))

    async def delete_tunnel_peer_window(self, peer_id: str) -> None:
        """Delete a tunnel device's live metrics window, once the device is gone."""
        await self.client.delete(TUNNEL_PEER_WINDOWS.key(peer_id))

    async def _staged_windows(self, staging: str) -> MetricWindows:
        """The windows a staging hash holds, read a page at a time."""
        staged = {
            _decode(key): _decode(text) async for key, text in self.client.hscan_iter(staging, count=_STAGING_PAGE)
        }
        return MetricWindows.of(staged)

    # Instance membership: the registry key proves an instance alive (it
    # expires with the TTL), the snapshot key carries what it reports about
    # itself, and the index lists who to look at.

    async def advertise_instance(self, instance_id: str, role: str, snapshot: str | None) -> None:
        """One heartbeat: the registry and snapshot keys with a fresh expiry, and the instance listed."""
        pipe = self.client.pipeline()
        pipe.set(INSTANCE_REGISTRY_KEY.format(instance_id=instance_id), role, ex=INSTANCE_TTL_SECONDS)
        if snapshot is not None:
            pipe.set(INSTANCE_STATS_KEY.format(instance_id=instance_id), snapshot, ex=INSTANCE_TTL_SECONDS)
        pipe.sadd(INSTANCE_INDEX_KEY, instance_id)
        await pipe.execute()

    async def withdraw_instance(self, instance_id: str) -> None:
        """A graceful departure, so peers see it now rather than when the keys expire."""
        pipe = self.client.pipeline()
        pipe.delete(INSTANCE_REGISTRY_KEY.format(instance_id=instance_id), INSTANCE_STATS_KEY.format(instance_id=instance_id))
        pipe.srem(INSTANCE_INDEX_KEY, instance_id)
        await pipe.execute()

    # Tunnel peer status, per protocol

    async def set_tunnel_peer_status(self, protocol: str, instance_id: str, statuses: dict[str, str]) -> None:
        """Replace this instance's published peer statuses for a protocol (peer key -> JSON) with a fresh expiry."""
        key = tunnel_carrier_key(protocol, instance_id)
        pipe = self.client.pipeline()
        pipe.delete(key)
        if statuses:
            # redis-py types the mapping's keys as a union, and a mapping is invariant in its key type.
            pipe.hset(key, mapping=statuses)  # type: ignore[arg-type]
            # Only with a hash to find: an empty one is gone, and readers would drop it again.
            pipe.sadd(TUNNEL_PEER_STATUS_INDEX_KEY.format(protocol=protocol), instance_id)
        pipe.expire(key, TUNNEL_STATUS_TTL_SECONDS)
        await pipe.execute()

    # The tunnel DNS's fake-IP mapping: on the request path, so each call
    # takes the request deadline and its caller fails open.

    async def fakeip_offset_for(self, name: str) -> int | None:
        """The pool offset a name was given on any instance, refreshing its lease; None if never."""
        name_key = TUNNEL_FAKEIP_NAME_KEY.format(name=name)
        async with self.request_deadline():
            raw = await self.client.get(name_key)
            if raw is None:
                return None
            offset = int(raw)
            pipe = self.client.pipeline()
            pipe.expire(name_key, TUNNEL_FAKEIP_TTL_SECONDS)
            pipe.expire(TUNNEL_FAKEIP_ADDR_KEY.format(offset=offset), TUNNEL_FAKEIP_TTL_SECONDS)
            await pipe.execute()
        return offset

    async def fakeip_name_for(self, offset: int) -> str | None:
        """The name a pool offset was handed out for on any instance, refreshing its lease."""
        addr_key = TUNNEL_FAKEIP_ADDR_KEY.format(offset=offset)
        async with self.request_deadline():
            name = await self.client.get(addr_key)
            if name is None:
                return None
            pipe = self.client.pipeline()
            pipe.expire(addr_key, TUNNEL_FAKEIP_TTL_SECONDS)
            pipe.expire(TUNNEL_FAKEIP_NAME_KEY.format(name=name), TUNNEL_FAKEIP_TTL_SECONDS)
            await pipe.execute()
        return str(name)

    async def fakeip_allocate(self, name: str, capacity: int, attempts: int = 64) -> int:
        """Give ``name`` a free pool offset, cluster-wide, every attempt within one request deadline.

        A shared counter walks the pool past the offsets still leased; two
        instances allocating one name at once both get an offset for it.
        """
        async with self.request_deadline():
            for _ in range(attempts):
                counter = int(await self.client.incr(TUNNEL_FAKEIP_COUNTER_KEY))
                offset = (counter - 1) % capacity
                claimed = await self.client.set(
                    TUNNEL_FAKEIP_ADDR_KEY.format(offset=offset), name, nx=True, ex=TUNNEL_FAKEIP_TTL_SECONDS
                )
                if claimed:
                    await self.client.set(
                        TUNNEL_FAKEIP_NAME_KEY.format(name=name), str(offset), ex=TUNNEL_FAKEIP_TTL_SECONDS
                    )
                    return offset
        raise RuntimeError(f"no free fake IP after {attempts} attempts; the range is exhausted")

    async def get_tunnel_peer_status(self, protocol: str) -> list[dict[str, str]]:
        """What every instance carrying a protocol last published, one hash per carrier; [] when none is."""
        hashes = await self._read_listed_hashes(
            TUNNEL_PEER_STATUS_INDEX_KEY.format(protocol=protocol),
            partial(tunnel_carrier_key, protocol),
        )
        return [data for _key, data in hashes]


class BatchSender:
    """Sends one loop's batch parts so that Redis applies each once: what it has not confirmed goes again first.

    ``on_answer``, when given, sees each send's parts with Redis's answers
    as they come, so a writer counts a send even when a later one fails.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        *,
        on_answer: Callable[[list[tuple[BatchPart, BatchReply]]], None] | None = None,
    ) -> None:
        self._redis = redis_client
        self._on_answer = on_answer
        # Parts Redis has not confirmed: a send it did not answer (unchanged)
        # or parts it refused to run (renumbered), in the order they go again.
        self._owed: list[BatchPart] = []
        # One send at a time, so a channel's sequences reach Redis in order.
        self._lock = asyncio.Lock()

    @property
    def owed(self) -> tuple[BatchPart, ...]:
        """The parts Redis has not confirmed yet."""
        return tuple(self._owed)

    async def send(self, cut: Callable[[], Sequence[BatchPart]]) -> list[tuple[BatchPart, BatchReply]]:
        """Send the owed parts, then, once none is owed, the parts ``cut()`` makes; each part sent, with its answer.

        When Redis does not answer it raises, and the parts it was sent stay owed unchanged.
        """
        async with self._lock:
            answered = await self._send_owed() if self._owed else []
            if self._owed:
                return answered
            self._owed = list(cut())
            if self._owed:
                answered += await self._send_owed()
            return answered

    async def _send_owed(self) -> list[tuple[BatchPart, BatchReply]]:
        """Send every owed part in one round trip; the ones Redis refused are owed again, renumbered."""
        parts = self._owed
        answered = list(zip(parts, await self._redis.send_batch(parts), strict=True))
        self._owed = [self._redis.renumber(part) for part, reply in answered if reply.outcome is BatchOutcome.REFUSED]
        if self._on_answer is not None:
            self._on_answer(answered)
        return answered
