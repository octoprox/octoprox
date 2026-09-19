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
  fetchSystemStats, fetchSystemHistory, SystemCache, SystemDatabase, SystemInventory, SystemLease,
  SystemMetricsHistory, SystemMetricsPoint, SystemProjectUsage, SystemRedis, SystemRuntime,
  SystemWorkers, SystemWorkerTask, SystemHistoryRange, SYSTEM_HISTORY_RANGES, WorkerState,
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
 * task list describe only the instance that answered - which, behind a load
 * balancer, is whichever one the request landed on. The cards say so.
 */
export default function SystemSection() {
  const { data, error, isLoading, isFetching, dataUpdatedAt, refetch } = useQuery({
    queryKey: ['system-stats'],
    queryFn: fetchSystemStats,
    refetchInterval: REFRESH_MS,
  })

  return (
    <Page
      title="System"
      subtitle="Inventory, storage and background workers for this Octoprox install."
      actions={
        <div className="flex items-center gap-3">
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
          <RuntimeCard runtime={data.runtime} />
          <InventoryTiles inventory={data.inventory} />
          <TrendsSection />

          <div className="grid grid-cols-1 @4xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-4 items-start">
            <WorkersCard workers={data.workers} />
            <div className="flex flex-col gap-4 min-w-0">
              <PoolCard inventory={data.inventory} />
              <ClusterCard workers={data.workers} instanceId={data.runtime.instance_id} />
            </div>
          </div>

          <div className="grid grid-cols-1 @4xl:grid-cols-2 gap-4 items-start">
            <DatabaseCard database={data.database} />
            <RedisCard redis={data.redis} />
          </div>

          <div className="grid grid-cols-1 @4xl:grid-cols-2 gap-4 items-start">
            <CacheCard cache={data.cache} />
            <ProjectsCard projects={data.projects} />
          </div>
        </>
      )}
    </Page>
  )
}

// --- runtime -------------------------------------------------------------------------

function RuntimeCard({ runtime }: { runtime: SystemRuntime }) {
  return (
    <Card className="px-4 py-3">
      <div className="flex items-center gap-2 mb-2.5">
        <Server className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold">This instance</h3>
        <Badge color="gray" className="font-mono text-[11px]">{runtime.instance_id.slice(0, 8)}</Badge>
        <Badge color="blue">{runtime.role}</Badge>
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
      <div className="text-[21px] leading-7 font-semibold tabular-nums">{value.toLocaleString()}</div>
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
        action={<span className="text-xs text-fg-muted tabular-nums">{total.toLocaleString()} live</span>}
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

function WorkersCard({ workers }: { workers: SystemWorkers }) {
  const failed = workers.tasks.filter((t) => t.state !== 'running').length
  return (
    <Card className="px-4 py-3">
      <CardHeader
        title="Background workers"
        action={
          failed === 0
            ? <Badge color="green">all running</Badge>
            : <Badge color="red">{failed} not running</Badge>
        }
        className="mb-1.5"
      />
      <div className="-mx-1">
        {workers.tasks.map((task) => <WorkerRow key={task.name} task={task} />)}
      </div>
      <div className="mt-2.5 pt-2.5 border-t border-line grid grid-cols-2 gap-x-4">
        <Fact
          label="Proxy listener"
          value={workers.proxy_server_listening
            ? `${workers.proxy_server_connections.toLocaleString()} open connections`
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

function WorkerRow({ task }: { task: SystemWorkerTask }) {
  const state = WORKER_STATE[task.state] ?? WORKER_STATE.done
  const Icon = state.icon
  return (
    <div className="flex items-start gap-2.5 px-1 py-1.5 rounded-md hover:bg-surface-raised transition-colors">
      <Icon className={cn('w-3.5 h-3.5 mt-0.5 flex-none', state.className)} />
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] font-medium">{formatWorkerName(task.name)}</div>
        <div className="text-[11px] text-fg-subtle">{task.error ?? task.description}</div>
      </div>
      <span className={cn('text-[11px] flex-none', state.className)}>{state.label}</span>
    </div>
  )
}

function formatWorkerName(name: string): string {
  return name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
}

// --- cluster -------------------------------------------------------------------------

function ClusterCard({ workers, instanceId }: { workers: SystemWorkers; instanceId: string }) {
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
      <div className="flex flex-col gap-1">
        {workers.instances.map((instance) => (
          <div key={instance.instance_id} className="flex items-center gap-2 text-[12.5px]">
            <Network className="w-3.5 h-3.5 text-fg-subtle flex-none" />
            <span className="font-mono text-[11.5px] truncate">{instance.instance_id}</span>
            {instance.is_self && <Badge color="blue" className="px-1.5 py-0 text-[10px]">this one</Badge>}
            <span className="flex-1" />
            <span className="text-fg-subtle">{instance.role}</span>
          </div>
        ))}
      </div>

      <div className="mt-2.5 pt-2.5 border-t border-line">
        <div className="text-[11px] text-fg-muted mb-1">
          Singleton jobs, and which instance currently holds each lease
        </div>
        {workers.leases.length === 0 ? (
          <p className="text-[12px] text-fg-subtle py-1">No leases held right now.</p>
        ) : (
          <div className="flex flex-col gap-1">
            {workers.leases.map((lease) => <LeaseRow key={lease.name} lease={lease} instanceId={instanceId} />)}
          </div>
        )}
      </div>
    </Card>
  )
}

function LeaseRow({ lease, instanceId }: { lease: SystemLease; instanceId: string }) {
  return (
    <div className="flex items-center gap-2 text-[12.5px]" title={`Held by ${lease.holder}`}>
      <Clock className="w-3.5 h-3.5 text-fg-subtle flex-none" />
      <span className="truncate">{lease.kind}</span>
      {lease.target && <span className="font-mono text-[11px] text-fg-subtle truncate">{lease.target.slice(0, 8)}</span>}
      <span className="flex-1" />
      <span className="text-fg-subtle tabular-nums">{(lease.ttl_ms / 1000).toFixed(1)}s left</span>
      <span className="font-mono text-[11px] text-fg-muted">
        {lease.held_by_self ? 'this instance' : `${lease.holder.slice(0, 8)}…`}
      </span>
      <span className={cn('w-2 h-2 rounded-full flex-none', lease.holder === instanceId ? 'bg-success' : 'bg-fg-subtle')} />
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
            <Fact label="Keys" value={redis.total_keys.toLocaleString()} />
            <Fact label="Ops / sec" value={redis.ops_per_sec.toLocaleString()} />
            <Fact label="Clients" value={redis.connected_clients} />
            <Fact label="Hit rate" value={hitRate == null ? '-' : `${hitRate.toFixed(1)}%`} />
            <Fact label="Uptime" value={formatDuration(redis.uptime_seconds)} />
          </div>
          <div className="text-[11px] text-fg-muted mb-1">
            Keys by purpose{redis.truncated && <> · sampled from the first {redis.scanned_keys.toLocaleString()}</>}
          </div>
          {redis.groups.length === 0 ? (
            <p className="text-[12px] text-fg-subtle py-1">Keyspace is empty.</p>
          ) : (
            redis.groups.map((group) => (
              <BarRow
                key={group.label}
                label={group.label}
                fraction={group.keys / max}
                value={group.keys.toLocaleString()}
              />
            ))
          )}
        </>
      )}
    </Card>
  )
}

// --- caches & projects ---------------------------------------------------------------

function CacheCard({ cache }: { cache: SystemCache }) {
  return (
    <Card className="px-4 py-3">
      <div className="flex items-center gap-2 mb-1">
        <Cpu className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold">In-memory caches</h3>
      </div>
      <p className="text-[11px] text-fg-subtle mb-2">
        Held by this instance only. Entity counts should track the database; a lasting gap means a reload is overdue.
      </p>
      <KeyValue label="Projects" value={cache.projects.toLocaleString()} />
      <KeyValue label="Credentials" value={cache.credentials.toLocaleString()} />
      <KeyValue label="Connectors" value={cache.connectors.toLocaleString()} />
      <KeyValue label="Proxies" value={cache.proxies.toLocaleString()} />
      <KeyValue label="Provider types" value={cache.provider_types.toLocaleString()} />
      <KeyValue label="Per-project strategies" value={cache.project_strategies.toLocaleString()} />
      <KeyValue label="Quarantined proxies" value={cache.quarantined_proxies.toLocaleString()} />
      <KeyValue label="TLS contexts" value={cache.tls_contexts.toLocaleString()} />
      <KeyValue label="Geo provisioning locks" value={cache.geo_provision_locks.toLocaleString()} />
      <KeyValue
        label="Metric deltas awaiting flush"
        value={`${cache.pending_proxy_deltas.toLocaleString()} proxy · ${cache.pending_project_deltas.toLocaleString()} project`}
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
                  <td className="py-1.5 pl-3">{project.credentials}</td>
                  <td className="py-1.5 pl-3">{project.connectors}</td>
                  <td className="py-1.5 pl-3 font-medium">{project.proxies}</td>
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
      value: num(last.redis_keys).toLocaleString(),
      delta: signedCount(num(last.redis_keys) - num(first.redis_keys)),
    },
    {
      label: 'Proxies',
      value: num(last.proxies_total).toLocaleString(),
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
  return `${delta > 0 ? '+' : '−'}${Math.abs(delta).toLocaleString()}`
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
