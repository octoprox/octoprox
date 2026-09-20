// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import {
  AlertTriangle, CheckCircle2, CircleOff, Clock, Cpu, Database, HardDrive,
  Network, RefreshCw, Server, TrendingUp,
} from 'lucide-react'
import {
  fetchSystemStats, fetchSystemHistory, SystemCache, SystemDatabase, SystemInstance, SystemInventory,
  SystemLease, SystemMetricsHistory, SystemMetricsPoint, SystemProjectUsage, SystemRedis, SystemRuntime,
  SystemStats, SystemWorkers, SystemWorkerTask, SystemHistoryRange, SYSTEM_HISTORY_RANGES, WorkerState,
} from '../../api/client'
import { Page } from '../../components/layout/Page'
import { useTheme } from '../../contexts/ThemeContext'
import { formatBytes, parseApiDate, relativeTime } from '../../utils/format'
import { Alert, Badge, Button, Card, CardHeader, KeyValue, Segmented } from '../../components/ui'
import { cn } from '../../utils/cn'

const REFRESH_MS = 30_000

/**
 * Instance and install-wide statistics for admins: what exists, what it costs
 * in Postgres and Redis, and which background workers are alive.
 *
 * Postgres-derived numbers describe the whole install. Runtime, caches and the
 * task list describe a single process, and which one is a choice: the instance
 * that answered the request, or any peer, from the snapshot that peer publishes
 * on its own heartbeat. The cards say which, and how old the numbers are.
 */
export default function SystemSection() {
  const { data, error, isLoading, isFetching, dataUpdatedAt, refetch } = useQuery({
    queryKey: ['system-stats'],
    queryFn: fetchSystemStats,
    refetchInterval: REFRESH_MS,
  })

  // Null means "whichever instance answered", so a load balancer moving us to
  // a different backend keeps showing a live view rather than pinning to an id
  // that is now a peer.
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const view = data ? instanceView(data, selectedId) : null

  return (
    <Page
      title="System"
      subtitle="Inventory, storage and background workers for this Octoprox install."
      actions={
        <div className="flex items-center gap-3 flex-wrap justify-end">
          {data && (
            <InstancePicker
              instances={data.workers.instances}
              selectedId={selectedId ?? data.runtime.instance_id}
              onSelect={setSelectedId}
            />
          )}
          {dataUpdatedAt > 0 && (
            <span className="text-xs text-fg-muted tabular-nums">
              Updated {relativeTime(new Date(dataUpdatedAt).toISOString())}
            </span>
          )}
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={cn('w-3.5 h-3.5', isFetching && 'animate-spin')} /> Refresh
          </Button>
        </div>
      }
    >
      {error && <Alert variant="error">{(error as Error).message || 'Failed to load system statistics'}</Alert>}
      {!data ? (
        <p className="text-sm text-fg-muted py-8 text-center">{isLoading ? 'Loading…' : 'No data'}</p>
      ) : (
        <>
          {view
            ? <RuntimeCard view={view} />
            : <NoInstanceViewNotice
                instanceId={selectedId ?? ''}
                stillAMember={data.workers.instances.some((i) => i.instance_id === selectedId)}
                onBack={() => setSelectedId(null)}
              />}
          <InventoryTiles inventory={data.inventory} />
          <TrendsSection />

          <div className="grid grid-cols-1 @4xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-4 items-start">
            {view ? <WorkersCard view={view} /> : <div />}
            <div className="flex flex-col gap-4 min-w-0">
              <PoolCard inventory={data.inventory} />
              <ClusterCard
                workers={data.workers}
                instanceId={data.runtime.instance_id}
                selectedId={selectedId ?? data.runtime.instance_id}
                onSelect={setSelectedId}
              />
            </div>
          </div>

          <div className="grid grid-cols-1 @4xl:grid-cols-2 gap-4 items-start">
            <DatabaseCard database={data.database} />
            <RedisCard redis={data.redis} />
          </div>

          <div className="grid grid-cols-1 @4xl:grid-cols-2 gap-4 items-start">
            {view ? <CacheCard view={view} /> : <div />}
            <ProjectsCard projects={data.projects} />
          </div>
        </>
      )}
    </Page>
  )
}

// --- instance selection ----------------------------------------------------------------

/**
 * The three sections that describe one process, resolved to the instance the
 * admin picked.
 *
 * `ageSeconds` is null for the instance that served the request, whose numbers
 * are collected live, and a few seconds for a peer, whose numbers come from its
 * last heartbeat. Kept apart deliberately: a stale snapshot and a worker that
 * has stopped counting look identical unless the page says which it is.
 */
interface InstanceView {
  instanceId: string
  isSelf: boolean
  runtime: SystemRuntime
  cache: SystemCache
  workers: SystemWorkers
  ageSeconds: number | null
}

/**
 * Null when the chosen peer is in the registry but has published nothing
 * readable, which normally means it runs a version older than snapshots -
 * membership and snapshot are published together.
 */
function instanceView(data: SystemStats, selectedId: string | null): InstanceView | null {
  if (selectedId === null || selectedId === data.runtime.instance_id) {
    return {
      instanceId: data.runtime.instance_id,
      isSelf: true,
      runtime: data.runtime,
      cache: data.cache,
      workers: data.workers,
      ageSeconds: null,
    }
  }
  const peer = data.workers.instances.find((i) => i.instance_id === selectedId)
  if (!peer?.snapshot) return null
  return {
    instanceId: peer.instance_id,
    isSelf: false,
    runtime: peer.snapshot.runtime,
    cache: peer.snapshot.cache,
    // Leases and membership are cluster-wide, so they come from the response
    // itself; only the per-process fields are swapped for the peer's.
    workers: {
      ...data.workers,
      tasks: peer.snapshot.tasks,
      proxy_server_listening: peer.snapshot.proxy_server_listening,
      proxy_server_connections: peer.snapshot.proxy_server_connections,
      geo_lookup_enabled: peer.snapshot.geo_lookup_enabled,
      geo_lookups_in_flight: peer.snapshot.geo_lookups_in_flight,
    },
    ageSeconds: peer.age_seconds,
  }
}

function InstancePicker({ instances, selectedId, onSelect }: {
  instances: SystemInstance[]
  selectedId: string
  onSelect: (id: string | null) => void
}) {
  // Nothing to switch between on a single-instance install, which is most of
  // them - the control appears only once a cluster exists.
  if (instances.length < 2) return null
  return (
    <Segmented
      size="sm"
      value={selectedId}
      onChange={(id) => onSelect(instances.find((i) => i.instance_id === id)?.is_self ? null : id)}
      options={instances.map((i) => ({
        value: i.instance_id,
        label: i.is_self ? 'This instance' : shortId(i.instance_id),
      }))}
    />
  )
}

/**
 * Two ways a chosen instance can have nothing to show, which are worth telling
 * apart: it left the cluster, or it is a member that publishes no snapshot.
 * Neither switches the view back on its own - a card quietly becoming a
 * different instance's is worse than a sentence saying what happened.
 */
function NoInstanceViewNotice({ instanceId, stillAMember, onBack }: {
  instanceId: string
  stillAMember: boolean
  onBack: () => void
}) {
  return (
    <Alert variant="info">
      <div className="flex items-center gap-3 flex-wrap">
        <span>
          {stillAMember ? (
            <>
              Instance <span className="font-mono">{shortId(instanceId)}</span> is in the cluster but
              has not published what it sees. Instances publish that alongside their membership, so
              this one is most likely running a version older than instance snapshots.
            </>
          ) : (
            <>
              Instance <span className="font-mono">{shortId(instanceId)}</span> has left the cluster.
              Its heartbeat stopped, so it is no longer reporting.
            </>
          )}
        </span>
        <Button variant="outline" size="sm" onClick={onBack}>Back to this instance</Button>
      </div>
    </Alert>
  )
}

/**
 * Instance ids are UUIDs unless the deployment sets OCTOPROX_INSTANCE_ID, so a
 * long one is cut to the same prefix the rest of the page shows it by.
 */
function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 8)}…` : id
}

/** How recent a peer's snapshot is, phrased for a heartbeat that runs every few seconds. */
function snapshotAge(seconds: number | null): string {
  if (seconds === null || seconds < 1) return 'just now'
  return `${Math.round(seconds)}s ago`
}

// --- runtime -------------------------------------------------------------------------

function RuntimeCard({ view }: { view: InstanceView }) {
  const { runtime, isSelf, ageSeconds } = view
  return (
    <Card className="px-4 py-3">
      <div className="flex items-center gap-2 mb-2.5 flex-wrap">
        <Server className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold">{isSelf ? 'This instance' : 'Peer instance'}</h3>
        <Badge color="gray" className="font-mono text-[11px]">{runtime.instance_id.slice(0, 8)}</Badge>
        <Badge color="blue">{runtime.role}</Badge>
        {/* A peer reports on its own heartbeat, so say how old the reading is
            rather than letting it pass for a live one. */}
        {!isSelf && (
          <Badge
            color="gray"
            title="Peers publish what they see every few seconds; this is their last publication."
          >
            as of {snapshotAge(ageSeconds)}
          </Badge>
        )}
        <span className="flex-1" />
        <span className="text-xs text-fg-muted">
          Octoprox <b className="text-fg font-semibold">{runtime.version}</b>
        </span>
      </div>
      <div className="grid grid-cols-2 @lg:grid-cols-3 @3xl:grid-cols-6 gap-x-6 gap-y-2">
        <Fact label="Uptime" value={formatDuration(runtime.uptime_seconds)} />
        <Fact label="Environment" value={runtime.environment} />
        <Fact label="Log level" value={runtime.log_level} />
        <Fact label="API / proxy port" value={`${runtime.api_port} / ${runtime.proxy_port}`} />
        <Fact label="Python" value={runtime.python_version} hint={runtime.platform} />
        <Fact label="PID" value={runtime.pid} />
        <Fact label="Health checks" value={formatInterval(runtime.health_check_interval)} />
        <Fact label="Metrics flush" value={formatInterval(runtime.metrics_flush_interval)} />
        <Fact label="IP refresh" value={formatInterval(runtime.ip_refresh_interval)} />
      </div>
    </Card>
  )
}

function Fact({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="min-w-0" title={hint}>
      <div className="text-[11px] text-fg-muted truncate">{label}</div>
      <div className="text-[13px] font-medium tabular-nums truncate">{value}</div>
    </div>
  )
}

// --- inventory -----------------------------------------------------------------------

function InventoryTiles({ inventory }: { inventory: SystemInventory }) {
  const providersSub = inventory.providers_custom > 0
    ? `${inventory.providers_builtin} built-in · ${inventory.providers_custom} custom`
    : `${inventory.providers_builtin} built-in`
  return (
    <Card className="grid grid-cols-2 @lg:grid-cols-3 @5xl:grid-cols-6 gap-px bg-line overflow-hidden [&>*]:bg-surface">
      <Tile label="Projects" value={inventory.projects} />
      <Tile label="Connectors" value={inventory.connectors} sub={connectorSub(inventory)} />
      <Tile label="Credentials" value={inventory.credentials} />
      <Tile label="Proxies" value={inventory.proxies} />
      <Tile label="Users" value={inventory.users} sub={roleSummary(inventory)} />
      <Tile label="Providers" value={inventory.providers_total} sub={providersSub} />
    </Card>
  )
}

function connectorSub(inventory: SystemInventory): string {
  const disabled = inventory.connectors - inventory.connectors_enabled
  const parts = [`${inventory.connectors_enabled} enabled`]
  if (disabled > 0) parts.push(`${disabled} off`)
  if (inventory.connectors_failing > 0) parts.push(`${inventory.connectors_failing} erroring`)
  return parts.join(' · ')
}

function roleSummary(inventory: SystemInventory): string {
  const roles = Object.entries(inventory.users_by_role)
  if (roles.length === 0) return ''
  return roles.map(([role, n]) => `${n} ${role}`).join(' · ')
}

function Tile({ label, value, sub }: { label: string; value: number; sub?: string }) {
  return (
    <div className="px-3 py-2.5 @lg:px-4 @lg:py-3 min-w-0">
      <div className="text-xs text-fg-muted truncate">{label}</div>
      <div
        className="text-[21px] leading-7 font-semibold tabular-nums"
        title={value.toLocaleString()}
      >
        {formatCount(value)}
      </div>
      {sub && <div className="text-[11px] text-fg-subtle truncate" title={sub}>{sub}</div>}
    </div>
  )
}

// --- proxy pool ----------------------------------------------------------------------

// Reserved status colours, matching the pool health bar on the project Overview.
const STATUS_COLORS: Record<string, string> = {
  healthy: 'bg-success',
  degraded: 'bg-warning',
  unhealthy: 'bg-danger',
  initializing: 'bg-primary',
  draining: 'bg-orange-500',
  terminating: 'bg-purple-500',
  unknown: 'bg-fg-subtle/40',
}
const STATUS_ORDER = ['healthy', 'degraded', 'unhealthy', 'initializing', 'draining', 'terminating', 'unknown']

/** Statuses the API gains later sort to the end rather than to the front. */
function statusRank(status: string): number {
  const i = STATUS_ORDER.indexOf(status)
  return i === -1 ? STATUS_ORDER.length : i
}

function PoolCard({ inventory }: { inventory: SystemInventory }) {
  const entries = Object.entries(inventory.proxies_by_status)
    .filter(([, n]) => n > 0)
    .sort((a, b) => statusRank(a[0]) - statusRank(b[0]))
  const total = entries.reduce((sum, [, n]) => sum + n, 0)

  return (
    <Card className="px-4 py-3">
      <CardHeader
        title="Proxy pool"
        action={
          <span className="text-xs text-fg-muted tabular-nums" title={total.toLocaleString()}>
            {formatCount(total)} live
          </span>
        }
        className="mb-2.5"
      />
      {total === 0 ? (
        <p className="text-xs text-fg-muted py-2">No proxies in the pool.</p>
      ) : (
        <>
          <div className="flex h-2.5 rounded-md overflow-hidden gap-0.5 bg-surface-raised">
            {entries.map(([status, n]) => (
              <div
                key={status}
                style={{ flex: n }}
                className={cn('rounded-sm first:rounded-l-md last:rounded-r-md', STATUS_COLORS[status] ?? 'bg-fg-subtle/40')}
              />
            ))}
          </div>
          <div className="flex items-center gap-x-4 gap-y-1 flex-wrap mt-2.5 text-[12.5px] text-fg-muted tabular-nums">
            {entries.map(([status, n]) => (
              <span key={status} className="inline-flex items-center gap-1.5">
                <span className={cn('w-2 h-2 rounded-full', STATUS_COLORS[status] ?? 'bg-fg-subtle/40')} />
                <b className="text-fg font-semibold">{n}</b> {status}
              </span>
            ))}
          </div>
        </>
      )}
      <p className="text-[11px] text-fg-subtle mt-2.5">
        Health status is live pool state held by this instance, not a Postgres count.
      </p>
    </Card>
  )
}

// --- workers -------------------------------------------------------------------------

const WORKER_STATE: Record<WorkerState, { icon: typeof CheckCircle2; className: string; label: string }> = {
  running: { icon: CheckCircle2, className: 'text-success', label: 'running' },
  failed: { icon: AlertTriangle, className: 'text-danger', label: 'failed' },
  cancelled: { icon: CircleOff, className: 'text-fg-subtle', label: 'cancelled' },
  done: { icon: CircleOff, className: 'text-warning', label: 'stopped' },
}

function WorkersCard({ view }: { view: InstanceView }) {
  const { workers, isSelf, instanceId } = view
  // Two counts per worker, because "no lease here" and "no lease anywhere"
  // mean different things and only the pair can tell them apart. A
  // per-resource worker (auto-scaler, provider sync) can hold several at once,
  // one per connector, so both are counts rather than flags. Matched on the
  // holder rather than `held_by_self`, because the instance shown is not
  // necessarily the one that answered the request.
  const leasesHeld = new Map<string, number>()
  const leasesAnywhere = new Map<string, number>()
  for (const lease of workers.leases) {
    leasesAnywhere.set(lease.worker, (leasesAnywhere.get(lease.worker) ?? 0) + 1)
    if (lease.holder === instanceId) leasesHeld.set(lease.worker, (leasesHeld.get(lease.worker) ?? 0) + 1)
  }
  const stopped = workers.tasks.filter((t) => t.state !== 'running').length
  // A loop that is still alive but failing every cycle looks fine from the
  // task state alone; the run counters are what surface it.
  const failing = workers.tasks.filter((t) => t.state === 'running' && t.consecutive_failures > 0).length
  // Behind *now*, not "was behind once" - a blip during startup should not
  // colour the card for the rest of the process's life.
  const behind = workers.tasks.filter((t) => t.state === 'running' && t.consecutive_overruns > 0).length

  return (
    <Card className="px-4 py-3">
      <CardHeader
        title={isSelf ? 'Background workers' : `Background workers on ${shortId(instanceId)}`}
        action={
          stopped > 0
            ? <Badge color="red">{stopped} not running</Badge>
            : failing > 0
              ? <Badge color="yellow">{failing} failing</Badge>
              : behind > 0
                ? <Badge color="yellow">{behind} over cadence</Badge>
                : <Badge color="green">all running</Badge>
        }
        className="mb-1.5"
      />
      <div className="-mx-1">
        {workers.tasks.map((task) => (
          <WorkerRow
            key={task.name}
            task={task}
            leasesHeld={leasesHeld.get(task.name) ?? 0}
            leasesAnywhere={leasesAnywhere.get(task.name) ?? 0}
            isSelf={isSelf}
          />
        ))}
      </div>
      <p className="text-[11px] text-fg-subtle mt-2">
        Run counts are {isSelf ? "this instance's" : "that instance's"}, since it started, and each
        worker is listed with the cadence it was started on - a cycle slower than its cadence delays
        the next one. Singleton workers only run on the instance holding their lease - see Cluster.
        {!isSelf && ' These were published on that instance\'s own heartbeat, so they are a few seconds behind.'}
      </p>
      <div className="mt-2.5 pt-2.5 border-t border-line grid grid-cols-2 gap-x-4">
        <Fact
          label="Proxy listener"
          value={workers.proxy_server_listening
            ? `${formatCount(workers.proxy_server_connections)} open connections`
            : 'not listening'}
        />
        <Fact
          label="Exit-location lookups"
          value={workers.geo_lookup_enabled ? `${workers.geo_lookups_in_flight} in flight` : 'disabled'}
        />
      </div>
    </Card>
  )
}

/**
 * Where an elected worker stands right now. Three states, not two: held here,
 * held elsewhere, and held by nobody - which the previous two-state badge
 * reported as "standing by", claiming a peer was running a job that nothing
 * was running.
 *
 * Nobody holding it means opposite things for the two lease shapes. A global
 * singleton is meant to be held continuously, so an unheld one is a failover
 * gap that closes in seconds. A per-resource worker only holds a lease while
 * it works on a connector, so no lease is its resting state between ticks -
 * the run counters underneath, not this badge, say whether it is working.
 */
function LeaseBadge({ task, leasesHeld, leasesAnywhere, isSelf }: {
  task: SystemWorkerTask
  leasesHeld: number
  leasesAnywhere: number
  isSelf: boolean
}) {
  const here = isSelf ? 'This instance' : 'That instance'
  const elsewhere = leasesAnywhere - leasesHeld

  const { label, color, title } = task.lease_per_resource
    ? {
        color: leasesHeld > 0 ? 'green' as const : 'gray' as const,
        label: leasesHeld > 0 ? `per connector · ${leasesHeld} here` : 'per connector',
        title: leasesHeld > 0
          ? `${here} is working on ${leasesHeld} connector(s) right now, holding one ${task.lease} lease for each.`
          + (elsewhere > 0 ? ` Other instances hold ${elsewhere}.` : '')
          : `Takes one ${task.lease} lease per connector, only while it works on that connector, so`
            + ' between ticks no lease is held by anyone - that is idle, not standing by.'
            + (elsewhere > 0 ? ` Other instances are working on ${elsewhere} right now.` : '')
            + ' The run counts below say whether it is ticking.',
      }
    : leasesHeld > 0
      ? {
          color: 'green' as const,
          label: 'singleton · runs here',
          title: `${here} holds the ${task.lease} lease, so it runs the job.`,
        }
      : leasesAnywhere > 0
        ? {
            color: 'gray' as const,
            label: 'singleton · standing by',
            title: `Another instance holds the ${task.lease} lease; ${here.toLowerCase()} stands by, ready to take over within seconds if that one dies.`,
          }
        : {
            color: 'gray' as const,
            label: 'singleton · unclaimed',
            title: `No instance holds the ${task.lease} lease right now. This is the gap between a holder dying and a peer claiming it, which lasts a few seconds.`,
          }

  return <Badge color={color} className="px-1.5 py-0 text-[10px] font-normal" title={title}>{label}</Badge>
}

function WorkerRow({ task, leasesHeld, leasesAnywhere, isSelf }: {
  task: SystemWorkerTask
  leasesHeld: number
  leasesAnywhere: number
  isSelf: boolean
}) {
  const state = WORKER_STATE[task.state] ?? WORKER_STATE.done
  const Icon = state.icon
  // The exception that ended the loop outranks one it recovered from, and a
  // recovered one still outranks the description while it keeps recurring.
  const problem = task.state !== 'running'
    ? task.error
    : task.consecutive_failures > 0 ? task.last_error : null

  return (
    <div className="flex items-start gap-2.5 px-1 py-1.5 rounded-md hover:bg-surface-raised transition-colors">
      <Icon className={cn('w-3.5 h-3.5 mt-0.5 flex-none', state.className)} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-[12.5px] font-medium">{formatWorkerName(task.name)}</span>
          {task.scope === 'singleton' && (
            <LeaseBadge
              task={task}
              leasesHeld={leasesHeld}
              leasesAnywhere={leasesAnywhere}
              isSelf={isSelf}
            />
          )}
        </div>
        <div className={cn('text-[11px]', problem ? 'text-danger' : 'text-fg-subtle')}>
          {problem ?? task.description}
        </div>
        <WorkerRuns task={task} isSelf={isSelf} />
      </div>
      <span className={cn('text-[11px] flex-none', state.className)}>{state.label}</span>
    </div>
  )
}

/** Cycle counters: what the loop has actually done, as opposed to whether it exists. */
function WorkerRuns({ task, isSelf }: { task: SystemWorkerTask; isSelf: boolean }) {
  const cadence = task.interval_seconds !== null ? formatInterval(task.interval_seconds) : 'on each peer message'
  if (task.runs === 0) {
    const scope = task.scope === 'singleton'
      ? `no runs on ${isSelf ? 'this' : 'that'} instance`
      : 'no runs yet'
    return <div className="text-[11px] text-fg-subtle">{cadence}, {scope}</div>
  }
  // Both counters below are coloured on the streak, not the lifetime total: a
  // worker that hit one slow cycle hours ago and has been fine since is not a
  // problem now, and saying otherwise trains people to ignore the colour.
  const failingNow = task.consecutive_failures > 0
  const behindNow = task.consecutive_overruns > 0
  // Ticks that found nothing to do. Shown only where they happen, so the rows
  // for loops that always have work stay as short as they were - and where
  // they do happen, the run count alone is misleading: an idle install's delta
  // publisher reaches five figures a day without a single flush. Neither
  // number is a problem on its own, so this stays uncoloured.
  const workingRuns = task.runs - task.idle_runs
  return (
    <div className="flex items-center gap-x-2 gap-y-0.5 flex-wrap text-[11px] text-fg-subtle tabular-nums">
      <span>{cadence}</span>
      <span title={`${task.runs.toLocaleString()} runs`}>
        {formatCount(task.runs)} {task.runs === 1 ? 'run' : 'runs'}
      </span>
      {task.failures > 0 && (
        <span
          className={cn(failingNow && 'text-danger')}
          title={failingNow
            ? `${task.failures.toLocaleString()} failed cycles. ${task.last_error ?? ''}`
            : `${task.failures.toLocaleString()} failed cycles, last ${relativeTime(task.last_error_at)}, recovered since`}
        >
          {formatCount(task.failures)} failed
          {failingNow && ` (${formatCount(task.consecutive_failures)} in a row)`}
        </span>
      )}
      {task.idle_runs > 0 && (
        <span
          title={workingRuns === 0
            ? `All ${task.runs.toLocaleString()} cycles so far returned early with nothing to do. The loop is running; there has just been no work for it.`
            : `${workingRuns.toLocaleString()} of ${task.runs.toLocaleString()} cycles had something to do. The other ${task.idle_runs.toLocaleString()} returned early, and are left out of the timings.`}
        >
          {workingRuns === 0 ? 'nothing to do yet' : `${formatCount(workingRuns)} with work`}
        </span>
      )}
      {task.avg_duration_ms !== null && (
        <span title={timingScope('avg', workingRuns, task.idle_runs)}>
          {formatMs(task.avg_duration_ms)} avg
        </span>
      )}
      {task.max_duration_ms !== null && (
        <span title={timingScope('max', workingRuns, task.idle_runs)}>
          {formatMs(task.max_duration_ms)} max
        </span>
      )}
      {task.overruns > 0 && (
        <span
          className={cn(behindNow && 'text-warning')}
          title={behindNow
            ? `The last ${task.consecutive_overruns} cycles ran longer than the ${cadence.replace('every ', '')} cadence, so the next run starts late`
            : `${task.overruns.toLocaleString()} cycles ran long, most recently ${relativeTime(task.last_overrun_at)}; back on cadence since`}
        >
          {formatCount(task.overruns)} over cadence
          {behindNow && ` (${formatCount(task.consecutive_overruns)} in a row)`}
        </span>
      )}
      {task.last_run_at && <span>last {relativeTime(task.last_run_at)}</span>}
    </div>
  )
}

/** Says which cycles a timing covers, for the loops where that is not all of them. */
function timingScope(kind: 'avg' | 'max', workingRuns: number, idleRuns: number): string {
  const label = kind === 'avg' ? 'Mean' : 'Longest'
  return idleRuns === 0
    ? `${label} cycle duration`
    : `${label} duration of the ${workingRuns.toLocaleString()} ${workingRuns === 1 ? 'cycle' : 'cycles'} that did work - the idle ticks would only average it towards zero`
}

function formatWorkerName(name: string): string {
  return name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
}

function formatMs(ms: number): string {
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms / 60_000)}m`
}

// --- cluster -------------------------------------------------------------------------

function ClusterCard({ workers, instanceId, selectedId, onSelect }: {
  workers: SystemWorkers
  instanceId: string
  selectedId: string
  onSelect: (id: string | null) => void
}) {
  return (
    <Card className="px-4 py-3">
      <CardHeader
        title="Cluster"
        action={
          <span className="text-xs text-fg-muted tabular-nums">
            {workers.instances.length} {workers.instances.length === 1 ? 'instance' : 'instances'}
          </span>
        }
        className="mb-1.5"
      />
      {/* The second way to switch instances, for when the list is longer than
          the header control comfortably holds. */}
      <div className="flex flex-col gap-1">
        {workers.instances.map((instance) => (
          <button
            key={instance.instance_id}
            type="button"
            onClick={() => onSelect(instance.is_self ? null : instance.instance_id)}
            aria-pressed={instance.instance_id === selectedId}
            title={instance.snapshot
              ? `Show what ${instance.instance_id} reports about itself`
              : `${instance.instance_id} has not published what it sees yet`}
            className={cn(
              'flex items-center gap-2 text-[12.5px] w-full text-left px-1 -mx-1 py-0.5 rounded-md transition-colors',
              instance.instance_id === selectedId ? 'bg-surface-raised' : 'hover:bg-surface-raised'
            )}
          >
            <Network className="w-3.5 h-3.5 text-fg-subtle flex-none" />
            <span className="font-mono text-[11.5px] truncate">{instance.instance_id}</span>
            {instance.is_self && <Badge color="blue" className="px-1.5 py-0 text-[10px]">this one</Badge>}
            <span className="flex-1" />
            <span className="text-fg-subtle">{instance.role}</span>
          </button>
        ))}
      </div>

      <div className="mt-2.5 pt-2.5 border-t border-line">
        <div className="text-[11px] text-fg-muted mb-1">
          Singleton jobs: which instance's worker is running each one right now
        </div>
        {workers.leases.length === 0 ? (
          <p className="text-[12px] text-fg-subtle py-1">No leases held right now.</p>
        ) : (
          <div className="flex flex-col gap-1.5">
            {workers.leases.map((lease) => <LeaseRow key={lease.name} lease={lease} instanceId={instanceId} />)}
          </div>
        )}
      </div>
    </Card>
  )
}

function LeaseRow({ lease, instanceId }: { lease: SystemLease; instanceId: string }) {
  const mine = lease.holder === instanceId
  return (
    <div className="flex items-start gap-2" title={`Held by ${lease.holder}`}>
      <Clock className="w-3.5 h-3.5 mt-0.5 text-fg-subtle flex-none" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-[12.5px]">
          <span className="truncate">{lease.kind}</span>
          {lease.target && (
            <span className="font-mono text-[11px] text-fg-subtle truncate">{lease.target.slice(0, 8)}</span>
          )}
          <span className="flex-1" />
          <span className="text-[11px] text-fg-subtle tabular-nums flex-none">
            {(lease.ttl_ms / 1000).toFixed(1)}s left
          </span>
          <span className={cn('w-2 h-2 rounded-full flex-none', mine ? 'bg-success' : 'bg-fg-subtle')} />
        </div>
        {/* Names the worker, so this row and the Background workers list line up. */}
        <div className="text-[11px] text-fg-subtle truncate">
          {formatWorkerName(lease.worker || lease.name)} on{' '}
          {lease.held_by_self
            ? 'this instance'
            : <span className="font-mono">{lease.holder.slice(0, 8)}…</span>}
        </div>
      </div>
    </div>
  )
}

// --- postgres ------------------------------------------------------------------------

function DatabaseCard({ database }: { database: SystemDatabase }) {
  const max = Math.max(1, ...database.tables.map((t) => t.total_bytes))
  return (
    <Card className="px-4 py-3">
      <div className="flex items-center gap-2 mb-2.5">
        <Database className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold">PostgreSQL</h3>
        <span className="flex-1" />
        <span className="text-xs text-fg-muted">{database.name}</span>
      </div>
      {database.error ? (
        <Alert variant="error">{database.error}</Alert>
      ) : (
        <>
          <div className="grid grid-cols-2 @lg:grid-cols-3 gap-x-4 gap-y-2 mb-3">
            <Fact label="Database size" value={formatBytes(database.size_bytes)} />
            <Fact label="Server connections" value={database.backends ?? '-'} />
            <Fact
              label="Pool in use"
              value={database.pool_size != null ? `${database.pool_checked_out ?? 0} / ${database.pool_size}` : '-'}
              hint="Connections checked out of this instance's pool"
            />
          </div>
          <div className="text-[11px] text-fg-muted mb-1">Table size, indexes included</div>
          {database.tables.map((table) => (
            <BarRow
              key={table.name}
              label={table.name}
              fraction={table.total_bytes / max}
              value={formatBytes(table.total_bytes)}
              hint={`${formatBytes(table.table_bytes)} data + ${formatBytes(table.index_bytes)} indexes` +
                (table.row_estimate != null ? ` · ~${table.row_estimate.toLocaleString()} rows` : ' · row count not analysed yet')}
            />
          ))}
        </>
      )}
    </Card>
  )
}

// --- redis ---------------------------------------------------------------------------

function RedisCard({ redis }: { redis: SystemRedis }) {
  const max = Math.max(1, ...redis.groups.map((g) => g.keys))
  const lookups = redis.keyspace_hits + redis.keyspace_misses
  const hitRate = lookups > 0 ? (redis.keyspace_hits / lookups) * 100 : null
  return (
    <Card className="px-4 py-3">
      <div className="flex items-center gap-2 mb-2.5">
        <HardDrive className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold">Redis</h3>
        <span className="flex-1" />
        <span className="text-xs text-fg-muted">{redis.version && `v${redis.version}`}</span>
      </div>
      {redis.error ? (
        <Alert variant="error">{redis.error}</Alert>
      ) : (
        <>
          <div className="grid grid-cols-2 @lg:grid-cols-3 gap-x-4 gap-y-2 mb-3">
            <Fact
              label="Memory in use"
              value={formatBytes(redis.used_memory_bytes)}
              hint={`Peak ${formatBytes(redis.used_memory_peak_bytes)}, RSS ${formatBytes(redis.used_memory_rss_bytes)}`}
            />
            <Fact label="Keys" value={formatCount(redis.total_keys)} hint={redis.total_keys.toLocaleString()} />
            <Fact label="Ops / sec" value={formatCount(redis.ops_per_sec)} hint={redis.ops_per_sec.toLocaleString()} />
            <Fact label="Clients" value={redis.connected_clients} />
            <Fact label="Hit rate" value={hitRate == null ? '-' : `${hitRate.toFixed(1)}%`} />
            <Fact label="Uptime" value={formatDuration(redis.uptime_seconds)} />
          </div>
          <div className="text-[11px] text-fg-muted mb-1">
            Keys by purpose{redis.truncated && (
              <span title={`${redis.scanned_keys.toLocaleString()} keys scanned`}>
                {' '}· sampled from the first {formatCount(redis.scanned_keys)}
              </span>
            )}
          </div>
          {redis.groups.length === 0 ? (
            <p className="text-[12px] text-fg-subtle py-1">Keyspace is empty.</p>
          ) : (
            redis.groups.map((group) => (
              <BarRow
                key={group.label}
                label={group.label}
                fraction={group.keys / max}
                value={formatCount(group.keys)}
                hint={`${group.keys.toLocaleString()} keys`}
              />
            ))
          )}
        </>
      )}
    </Card>
  )
}

// --- caches & projects ---------------------------------------------------------------

function CacheCard({ view }: { view: InstanceView }) {
  const { cache, isSelf, instanceId } = view
  return (
    <Card className="px-4 py-3">
      <div className="flex items-center gap-2 mb-1">
        <Cpu className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold">
          In-memory caches{!isSelf && ` on ${shortId(instanceId)}`}
        </h3>
      </div>
      <p className="text-[11px] text-fg-subtle mb-2">
        Held by {isSelf ? 'this' : 'that'} instance only. Entity counts should track the database;
        a lasting gap means a reload is overdue.
      </p>
      <KeyValue label="Projects" value={formatCount(cache.projects)} />
      <KeyValue label="Credentials" value={formatCount(cache.credentials)} />
      <KeyValue label="Connectors" value={formatCount(cache.connectors)} />
      <KeyValue label="Proxies" value={formatCount(cache.proxies)} />
      <KeyValue label="Provider types" value={formatCount(cache.provider_types)} />
      <KeyValue label="Per-project strategies" value={formatCount(cache.project_strategies)} />
      <KeyValue label="Quarantined proxies" value={formatCount(cache.quarantined_proxies)} />
      <KeyValue label="TLS contexts" value={formatCount(cache.tls_contexts)} />
      <KeyValue label="Geo provisioning locks" value={formatCount(cache.geo_provision_locks)} />
      <KeyValue
        label="Metric deltas awaiting flush"
        value={`${formatCount(cache.pending_proxy_deltas)} proxy · ${formatCount(cache.pending_project_deltas)} project`}
      />
    </Card>
  )
}

function ProjectsCard({ projects }: { projects: SystemProjectUsage[] }) {
  return (
    <Card className="px-4 py-3">
      <CardHeader
        title="Per project"
        action={<span className="text-xs text-fg-muted tabular-nums">{projects.length}</span>}
        className="mb-1.5"
      />
      {projects.length === 0 ? (
        <p className="text-xs text-fg-muted py-2">No projects yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[12.5px]">
            <thead>
              <tr className="text-[11px] text-fg-muted text-right">
                <th className="font-normal text-left pb-1">Project</th>
                <th className="font-normal pb-1 pl-3">Credentials</th>
                <th className="font-normal pb-1 pl-3">Connectors</th>
                <th className="font-normal pb-1 pl-3">Proxies</th>
              </tr>
            </thead>
            <tbody>
              {projects.map((project) => (
                <tr key={project.id} className="border-t border-line text-right tabular-nums">
                  <td className="text-left py-1.5 pr-3 max-w-[220px] truncate" title={project.name}>{project.name}</td>
                  <td className="py-1.5 pl-3">{formatCount(project.credentials)}</td>
                  <td className="py-1.5 pl-3">{formatCount(project.connectors)}</td>
                  <td className="py-1.5 pl-3 font-medium" title={project.proxies.toLocaleString()}>
                    {formatCount(project.proxies)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

// --- trends --------------------------------------------------------------------------

/*
 * Series colours are literal, matching the convention on the project Overview:
 * charts should read the same under every theme, so they do not follow the
 * theme tokens. The categorical set below was checked for colour-vision
 * separation in both light and dark surfaces - worst adjacent pair is well
 * clear of the floor. Blue and purple must never end up adjacent: they are
 * near-identical under deuteranopia, which is why the order here is fixed.
 */
const SERIES = {
  db: '#2563eb',
  redis: '#ea580c',
  teal: '#0d9488',
  purple: '#7c3aed',
  // Reserved status colours, same meaning as everywhere else in the app.
  healthy: '#16a34a',
  unhealthy: '#dc2626',
  other: '#9ca3af',
} as const

type TrendPoint = Record<string, number | null> & { time: number }

/**
 * Gauge history: how storage, the pool and the inventory have moved.
 *
 * Fed by the `system_snapshotter` worker rather than by this page, so the
 * series is identical from every instance - unlike the live cards above,
 * which describe whichever instance answered.
 */
function TrendsSection() {
  const [range, setRange] = useState<SystemHistoryRange>('24h')
  const { data, isLoading } = useQuery({
    queryKey: ['system-history', range],
    queryFn: () => fetchSystemHistory(range),
    refetchInterval: REFRESH_MS * 2,
  })

  const points = useMemo(() => buildTrendPoints(data), [data])
  const hasData = points.length > 1

  return (
    <Card className="px-4 py-3">
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <TrendingUp className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold">Trends</h3>
        {data?.bucket_seconds ? (
          <span className="text-[11px] text-fg-subtle">
            averaged into {formatInterval(data.bucket_seconds).replace('every ', '')} buckets
          </span>
        ) : null}
        <span className="flex-1" />
        <Segmented
          options={SYSTEM_HISTORY_RANGES.map((r) => ({ value: r, label: r }))}
          value={range}
          onChange={setRange}
        />
      </div>

      {!hasData ? (
        <p className="text-xs text-fg-muted py-8 text-center">
          {isLoading
            ? 'Loading…'
            : data && data.snapshots.length > 0
              ? 'Only one snapshot so far — a trend needs at least two.'
              : `No snapshots in this range yet. The first arrives within ${formatDuration(data?.interval_seconds ?? 300)}.`}
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          <DeltaStrip points={points} range={range} />
          <TrendChart
            title="Storage"
            points={points}
            height={200}
            series={[
              { key: 'database_size_bytes', name: 'Database', color: SERIES.db },
              { key: 'redis_memory_bytes', name: 'Redis memory', color: SERIES.redis },
            ]}
            formatValue={formatBytes}
            formatTick={bytesShort}
            tickWidth={56}
          />
          <div className="grid grid-cols-1 @3xl:grid-cols-2 gap-4">
            <TrendChart
              title="Proxy pool"
              points={points}
              height={150}
              stacked
              series={[
                { key: 'proxies_healthy', name: 'Healthy', color: SERIES.healthy },
                { key: 'proxies_unhealthy', name: 'Unhealthy', color: SERIES.unhealthy },
                { key: 'proxies_other', name: 'Other', color: SERIES.other },
              ]}
            />
            <TrendChart
              title="Inventory"
              points={points}
              height={150}
              series={[
                { key: 'projects', name: 'Projects', color: SERIES.db },
                { key: 'connectors', name: 'Connectors', color: SERIES.redis },
                { key: 'credentials', name: 'Credentials', color: SERIES.teal },
                { key: 'users', name: 'Users', color: SERIES.purple },
              ]}
            />
          </div>
          {data && data.table_growth.length > 0 && <TableGrowthList history={data} range={range} />}
        </div>
      )}
    </Card>
  )
}

/**
 * What moved across the window, as plain signed numbers.
 *
 * Deliberately not colour-coded: growth is not an error, and the status
 * palette is reserved for things that actually mean good or bad.
 */
function DeltaStrip({ points, range }: { points: TrendPoint[]; range: SystemHistoryRange }) {
  const real = points.filter((p) => p.database_size_bytes != null)
  if (real.length < 2) return null
  const first = real[0]
  const last = real[real.length - 1]

  const items: { label: string; value: string; delta: string }[] = [
    {
      label: 'Database',
      value: formatBytes(num(last.database_size_bytes)),
      delta: signedBytes(num(last.database_size_bytes) - num(first.database_size_bytes)),
    },
    {
      label: 'Redis memory',
      value: formatBytes(num(last.redis_memory_bytes)),
      delta: signedBytes(num(last.redis_memory_bytes) - num(first.redis_memory_bytes)),
    },
    {
      label: 'Redis keys',
      value: formatCount(num(last.redis_keys)),
      delta: signedCount(num(last.redis_keys) - num(first.redis_keys)),
    },
    {
      label: 'Proxies',
      value: formatCount(num(last.proxies_total)),
      delta: signedCount(num(last.proxies_total) - num(first.proxies_total)),
    },
  ]

  return (
    <div className="grid grid-cols-2 @lg:grid-cols-4 gap-px bg-line rounded-md overflow-hidden [&>*]:bg-surface">
      {items.map((item) => (
        <div key={item.label} className="px-3 py-2 min-w-0">
          <div className="text-[11px] text-fg-muted truncate">{item.label}</div>
          <div className="text-[15px] font-semibold tabular-nums truncate">{item.value}</div>
          <div className="text-[11px] text-fg-subtle tabular-nums truncate">{item.delta} over {range}</div>
        </div>
      ))}
    </div>
  )
}

function num(value: number | null | undefined): number {
  return value ?? 0
}

function signedBytes(delta: number): string {
  if (delta === 0) return 'no change'
  return `${delta > 0 ? '+' : '−'}${formatBytes(Math.abs(delta))}`
}

function signedCount(delta: number): string {
  if (delta === 0) return 'no change'
  return `${delta > 0 ? '+' : '−'}${formatCount(Math.abs(delta))}`
}

/**
 * Snapshots to chart points, inserting a null marker wherever the gap between
 * readings exceeds 2.5x the expected spacing, so a window where nothing was
 * recorded (instance down, snapshotter off) breaks the line instead of being
 * drawn through as if it were real.
 */
function buildTrendPoints(history: SystemMetricsHistory | undefined): TrendPoint[] {
  if (!history || history.snapshots.length === 0) return []
  const spacing = (history.bucket_seconds ?? history.interval_seconds ?? 300) * 1000
  const threshold = spacing * 2.5

  const out: TrendPoint[] = []
  let prev: number | null = null
  for (const snapshot of history.snapshots) {
    const time = parseApiDate(snapshot.timestamp)?.getTime()
    if (time == null) continue
    if (prev != null && time - prev > threshold) out.push(gapPoint(prev + 1))
    out.push(toPoint(snapshot, time))
    prev = time
  }
  return out
}

const TREND_KEYS = [
  'database_size_bytes', 'redis_memory_bytes', 'redis_keys', 'projects', 'credentials',
  'connectors', 'users', 'proxies_total', 'proxies_healthy', 'proxies_unhealthy', 'proxies_other',
] as const

function toPoint(snapshot: SystemMetricsPoint, time: number): TrendPoint {
  return {
    time,
    database_size_bytes: snapshot.database_size_bytes,
    redis_memory_bytes: snapshot.redis_memory_bytes,
    redis_keys: snapshot.redis_keys,
    projects: snapshot.projects,
    credentials: snapshot.credentials,
    connectors: snapshot.connectors,
    users: snapshot.users,
    proxies_total: snapshot.proxies_total,
    proxies_healthy: snapshot.proxies_healthy,
    proxies_unhealthy: snapshot.proxies_unhealthy,
    // Everything that is neither healthy nor unhealthy: initializing,
    // degraded, draining, terminating, unknown.
    proxies_other: Math.max(
      0,
      snapshot.proxies_total - snapshot.proxies_healthy - snapshot.proxies_unhealthy
    ),
  }
}

function gapPoint(time: number): TrendPoint {
  return { time, ...Object.fromEntries(TREND_KEYS.map((k) => [k, null])) } as TrendPoint
}

interface TrendSeries {
  key: string
  name: string
  color: string
}

function TrendChart({
  title, points, series, height, stacked, formatValue, formatTick, tickWidth,
}: {
  title: string
  points: TrendPoint[]
  series: TrendSeries[]
  height: number
  stacked?: boolean
  formatValue?: (v: number) => string
  formatTick?: (v: number) => string
  tickWidth?: number
}) {
  const { isDark } = useTheme()
  const gridColor = isDark ? '#374151' : '#e5e7eb'
  const tickColor = '#9ca3af'
  const tooltipStyle = isDark
    ? { backgroundColor: '#1f2937', border: '1px solid #374151', color: '#f3f4f6', borderRadius: 8, fontSize: 12 }
    : { borderRadius: 8, fontSize: 12, border: '1px solid #e5e7eb' }

  return (
    <div className="min-w-0">
      <CardHeader
        title={<span className="text-[13px]">{title}</span>}
        action={<ChartLegend items={series.map((s) => [s.name, s.color] as [string, string])} small />}
        className="mb-1"
      />
      <ResponsiveContainer width="100%" height={height}>
        <AreaChart data={points} margin={{ top: 6, right: 6, left: -12, bottom: 0 }}>
          <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
          <XAxis
            dataKey="time"
            type="number"
            scale="time"
            domain={['dataMin', 'dataMax']}
            tickFormatter={formatTrendTick}
            tick={{ fontSize: 10, fill: tickColor }}
            axisLine={false}
            tickLine={false}
            minTickGap={48}
          />
          <YAxis
            tick={{ fontSize: 10, fill: tickColor }}
            axisLine={false}
            tickLine={false}
            allowDecimals={false}
            width={tickWidth}
            tickFormatter={formatTick}
          />
          <Tooltip
            labelFormatter={(v: number) => new Date(v).toLocaleString()}
            formatter={(v: number, name: string) => [formatValue ? formatValue(v) : v.toLocaleString(), name]}
            contentStyle={tooltipStyle}
          />
          {series.map((s) => (
            <Area
              key={s.key}
              connectNulls={false}
              type="monotone"
              dataKey={s.key}
              name={s.name}
              stackId={stacked ? 'a' : undefined}
              stroke={s.color}
              strokeWidth={2}
              fill={s.color}
              fillOpacity={stacked ? 0.18 : 0.08}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function ChartLegend({ items, small }: { items: [string, string][]; small?: boolean }) {
  return (
    <div className={cn('flex items-center gap-3 flex-wrap', small ? 'text-[11px]' : 'text-xs', 'text-fg-muted')}>
      {items.map(([label, color]) => (
        <span key={label} className="inline-flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-0.5 rounded" style={{ background: color }} />
          {label}
        </span>
      ))}
    </div>
  )
}

/**
 * Which tables actually grew over the window. A difference between the two
 * window edges, so it needs no bucketing - and it is the question the storage
 * chart above raises as soon as the line slopes up.
 */
function TableGrowthList({ history, range }: { history: SystemMetricsHistory; range: SystemHistoryRange }) {
  const movers = history.table_growth.filter((t) => t.delta_bytes !== 0)
  if (movers.length === 0) {
    return (
      <p className="text-[11px] text-fg-subtle pt-1 border-t border-line">
        No table changed size over the last {range}.
      </p>
    )
  }
  const max = Math.max(...movers.map((t) => Math.abs(t.delta_bytes)))
  return (
    <div className="pt-2 border-t border-line">
      <div className="text-[11px] text-fg-muted mb-1">Table growth over the last {range}</div>
      {movers.map((table) => (
        <div key={table.name} className="flex items-center gap-3 py-[3px] text-[12.5px]">
          <span className="w-[88px] @lg:w-[128px] flex-none truncate text-fg-muted">{table.name}</span>
          <div className="flex-1 min-w-[24px] h-2 rounded-sm bg-surface-raised">
            <div
              className={cn('h-full rounded-r-[4px]', table.delta_bytes > 0 ? 'bg-primary' : 'bg-success')}
              style={{ width: `${Math.max((Math.abs(table.delta_bytes) / max) * 100, 2)}%` }}
            />
          </div>
          <span
            className="w-[92px] flex-none text-right font-medium tabular-nums"
            title={`${formatBytes(table.first_bytes)} → ${formatBytes(table.last_bytes)}`}
          >
            {table.delta_bytes > 0 ? '+' : '−'}{formatBytes(Math.abs(table.delta_bytes))}
          </span>
        </div>
      ))}
    </div>
  )
}

function formatTrendTick(epoch: number): string {
  const d = new Date(epoch)
  const sameDay = new Date().toDateString() === d.toDateString()
  return sameDay
    ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

/** Short byte format for axis ticks: 1 decimal, no trailing zero. */
function bytesShort(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(sizes.length - 1, Math.floor(Math.log(bytes) / Math.log(k)))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`
}

// --- shared bits ---------------------------------------------------------------------

/**
 * One row of a ranked magnitude list: name, a bar scaled to the largest value
 * in the list, and the value itself. A single measure, so every bar wears the
 * same hue - length is the encoding, colour would only repeat it.
 */
function BarRow({ label, fraction, value, hint }: { label: string; fraction: number; value: string; hint?: string }) {
  const pct = fraction > 0 ? Math.max(fraction * 100, 2) : 0
  return (
    <div className="flex items-center gap-3 py-[3px] text-[12.5px]" title={hint}>
      <span className="w-[88px] @lg:w-[128px] flex-none truncate text-fg-muted">{label}</span>
      <div className="flex-1 min-w-[24px] h-2 rounded-sm bg-surface-raised">
        <div className="h-full bg-primary rounded-r-[4px]" style={{ width: `${pct}%` }} />
      </div>
      <span className="w-[72px] @lg:w-[80px] flex-none text-right font-medium tabular-nums">{value}</span>
    </div>
  )
}

/*
 * Counts here are unbounded: a worker on a long-lived instance reaches millions
 * of runs, and the metric tables reach millions of rows. The rows they sit in
 * are laid out for a handful of characters, so past BAND_FROM the exact digits
 * stop being information and start being a layout problem. Exact below it,
 * banded above; callers keep the exact figure in a title so nothing is lost.
 */
const BAND_FROM = 100_000
const bandFormatter = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 })

function formatCount(value: number): string {
  return Math.abs(value) < BAND_FROM ? value.toLocaleString() : bandFormatter.format(value)
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

function formatInterval(seconds: number): string {
  if (seconds % 3600 === 0) return `every ${seconds / 3600}h`
  if (seconds % 60 === 0) return `every ${seconds / 60}m`
  return `every ${seconds}s`
}
