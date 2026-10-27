// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import {
  AlertTriangle, CheckCircle2, CircleOff, Cog, Crown, Database, GitBranch, Globe, HardDrive, ListChecks,
  MonitorSmartphone, Network, RefreshCw, Server, TrendingUp, Users,
} from 'lucide-react'
import {
  fetchGeoDatabases, fetchSystemStats, fetchSystemHistory, fetchSystemTopology, parseStreamId, streamIdBefore, FullReadReason,
  JOB_KINDS, JobKind, StreamId, SystemApiSnapshot, SystemControllerSnapshot, SystemControllerStatus, SystemDatabase, SystemGatewayFeed,
  SystemGatewaySnapshot, SystemInstance, SystemInstanceRuntime, SystemInstanceSnapshot, SystemInventory,
  SystemMetricsHistory, SystemMetricsPoint, SystemProjectUsage, SystemRedis, SystemStreamState, SystemTopology,
  SystemWorkStream, SystemWorkerSnapshot, SystemWorkerTask, SystemHistoryRange, SYSTEM_HISTORY_RANGES,
  TunnelEndpointState, WorkerState,
} from '../../api/client'
import { KIND_INFO } from '../../components/failures/FailureDetail'
import { Page } from '../../components/layout/Page'
import { useTheme } from '../../contexts/ThemeContext'
import { useTimeRange } from '../../contexts/TimeRangeContext'
import { bytesShort, elideMiddle, formatBytes, formatDateTime, parseApiDate, plural, relativeTime } from '../../utils/format'
import {
  Alert, Badge, Button, Card, CardHeader, FlowDiagram, FlowNode, Inspector, InspectorSection, KeyValue, Segmented,
  type FlowAnchor, type FlowEdge, type FlowTone,
} from '../../components/ui'
import { cn } from '../../utils/cn'

const REFRESH_MS = 30_000
// The topology reads only the heartbeats, the lease, the feed head and the
// work streams, so it can follow the heartbeats' own 5 s cadence.
const TOPOLOGY_REFRESH_MS = 5_000

/**
 * The install for admins: every process by its role, the controller and the
 * routing feed it publishes, the work streams the workers take, and what
 * exists and what it costs in Postgres and Redis.
 *
 * Nothing here describes the api process that answered: the same page comes
 * back from every one of them. Processes are shown from the snapshot each
 * publishes on its own heartbeat, so they are a few seconds old, and the
 * page says how old; picking one opens its whole snapshot beside the list.
 */
export default function SystemSection() {
  const queryClient = useQueryClient()
  const [view, setView] = useClusterView()
  const { data, error, isLoading, isFetching, dataUpdatedAt, refetch } = useQuery({
    queryKey: ['system-stats'],
    queryFn: fetchSystemStats,
    refetchInterval: REFRESH_MS,
  })
  const topologyQuery = useQuery({
    queryKey: ['system-topology'],
    queryFn: fetchSystemTopology,
    refetchInterval: TOPOLOGY_REFRESH_MS,
    enabled: view === 'topology',
  })
  // The cluster as the view on screen shows it: the topology's own reading,
  // fresher than the page's, until it arrives.
  const cluster = view === 'topology' ? topologyQuery.data ?? data : data
  const updatedAt = view === 'topology' ? topologyQuery.dataUpdatedAt : dataUpdatedAt

  // Kept by id rather than as the object, so the panel follows the process
  // across refreshes and says so when it has left rather than showing another.
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = cluster && selectedId ? allProcesses(cluster).find((i) => i.instance_id === selectedId) ?? null : null

  return (
    <Page
      title="System"
      subtitle="Every process of this Octoprox install by role, the routing feed, the work streams, and what Postgres and Redis hold."
      belowTitle={
        <Segmented
          size="sm"
          options={[{ value: 'list', label: 'List' }, { value: 'topology', label: 'Topology' }]}
          value={view}
          onChange={(next) => {
            // A reading kept from an earlier visit would seed the rates and the backlog ages across the gap.
            if (next === 'topology' && view !== 'topology') queryClient.resetQueries({ queryKey: ['system-topology'], exact: true })
            setView(next)
          }}
        />
      }
      actions={
        <div className="flex items-center gap-3 flex-wrap justify-end">
          {updatedAt > 0 && (
            <span className="text-xs text-fg-muted tabular-nums">
              Updated {relativeTime(new Date(updatedAt).toISOString())}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              refetch()
              if (view === 'topology') topologyQuery.refetch()
            }}
            disabled={isFetching}
          >
            {/* The page's own query only: the topology's 5 s polls would spin it and disable it every few seconds. */}
            <RefreshCw className={cn('w-3.5 h-3.5', isFetching && 'animate-spin')} /> Refresh
          </Button>
        </div>
      }
      panel={cluster && selectedId ? (
        <ProcessPanel
          key={selectedId}
          instanceId={selectedId}
          instance={selected}
          data={cluster}
          onClose={() => setSelectedId(null)}
        />
      ) : null}
    >
      {error && <Alert variant="error">{(error as Error).message || 'Failed to load system statistics'}</Alert>}
      {view === 'topology' && (
        <TopologyCard
          data={topologyQuery.data}
          error={topologyQuery.error}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
      )}
      {!data ? (
        <p className="text-sm text-fg-muted py-8 text-center">{isLoading ? 'Loading…' : 'No data'}</p>
      ) : (
        <>
          {view === 'list' && (
            <>
              <ProcessesCard data={data} selectedId={selectedId} onSelect={setSelectedId} />

              <div className="grid grid-cols-1 @4xl:grid-cols-2 gap-4 items-start">
                <ControllerCard data={data} onSelect={setSelectedId} />
                <FeedCard data={data} onSelect={setSelectedId} />
              </div>

              <WorkStreamsCard data={data} />
            </>
          )}

          <InventoryTiles inventory={data.inventory} />
          <TrendsSection />

          <div className="grid grid-cols-1 @4xl:grid-cols-2 gap-4 items-start">
            <PoolCard inventory={data.inventory} />
            <ProjectsCard projects={data.projects} />
          </div>

          <div className="grid grid-cols-1 @4xl:grid-cols-2 gap-4 items-start">
            <DatabaseCard database={data.database} />
            <RedisCard redis={data.redis} />
          </div>
        </>
      )}
    </Page>
  )
}

type ClusterView = 'list' | 'topology'

const VIEW_KEY = 'octoprox_system_view'

/** Which view of the cluster the page shows, remembered in this browser. */
function useClusterView(): [ClusterView, (view: ClusterView) => void] {
  const [view, setView] = useState<ClusterView>(() => {
    try { return localStorage.getItem(VIEW_KEY) === 'topology' ? 'topology' : 'list' } catch { return 'list' }
  })
  const choose = (next: ClusterView) => {
    setView(next)
    try { localStorage.setItem(VIEW_KEY, next) } catch { /* ignore */ }
  }
  return [view, choose]
}

// --- roles and processes -----------------------------------------------------------------

// The roles in the order the page lists them; a role a newer release adds goes last.
const ROLE_ORDER = ['all', 'api', 'gateway', 'controller', 'worker']

// How the page names each role: a group of its processes, one of them, and what they do.
const ROLE_INFO: Record<string, { label: string; one: string; description: string }> = {
  all: {
    label: 'All in one',
    one: 'All in one',
    description: 'Every runtime in one process: api, gateway, controller and worker',
  },
  api: {
    label: 'API',
    one: 'API',
    description: 'Serve the web UI and the admin API, from Postgres and Redis; any one answers alike',
  },
  gateway: {
    label: 'Gateways',
    one: 'Gateway',
    description: 'Carry proxy and tunnel traffic, routing on the feed the controller publishes',
  },
  controller: {
    label: 'Controllers',
    one: 'Controller',
    description: 'One leads: compiles the routing feed and publishes the periodic jobs; the others stand by',
  },
  worker: {
    label: 'Workers',
    one: 'Worker',
    description: 'Run every job (health checks, syncs, scaling, metrics, housekeeping) and ingest sightings and observations',
  },
}

/** A role's group heading. */
function roleLabel(role: string): string {
  return ROLE_INFO[role]?.label ?? role
}

/** One process's role. */
function roleName(role: string): string {
  return ROLE_INFO[role]?.one ?? role
}

/** The roles with live processes, in page order. */
function roleGroups(data: SystemTopology): [string, SystemInstance[]][] {
  const rank = (role: string) => {
    const i = ROLE_ORDER.indexOf(role)
    return i === -1 ? ROLE_ORDER.length : i
  }
  return Object.entries(data.instances).sort((a, b) => rank(a[0]) - rank(b[0]))
}

function allProcesses(data: SystemTopology): SystemInstance[] {
  return roleGroups(data).flatMap(([, list]) => list)
}

/** A process id short enough for its row, keeping the tail that tells two pods apart. */
function shortId(id: string, max = 44): string {
  return elideMiddle(id, max)
}

/** How recent a snapshot is, phrased for a heartbeat that runs every few seconds. */
function snapshotAge(seconds: number | null): string {
  if (seconds === null || seconds < 1) return 'just now'
  return `${Math.round(seconds)}s ago`
}

type Health = 'ok' | 'unready' | 'failing' | 'unknown'

const HEALTH_STYLE: Record<Health, { icon: typeof CheckCircle2; className: string }> = {
  ok: { icon: CheckCircle2, className: 'text-success' },
  unready: { icon: AlertTriangle, className: 'text-warning' },
  failing: { icon: AlertTriangle, className: 'text-danger' },
  unknown: { icon: CircleOff, className: 'text-fg-subtle' },
}

/**
 * What a process's row leads with. A loop that is gone or failing every cycle
 * outranks unreadiness, because a process can be ready while one of its
 * loops has quietly stopped working, and only the counters say so.
 */
function processHealth(instance: SystemInstance): { health: Health; title: string } {
  const snapshot = instance.snapshot
  if (!snapshot) {
    return {
      health: 'unknown',
      title: 'Listed by its heartbeat, but its snapshot could not be read: it could not build one, or a process of another release wrote it.',
    }
  }
  const stopped = snapshot.tasks.filter((t) => t.state !== 'running')
  if (stopped.length > 0) return { health: 'failing', title: `${stopped.length} ${plural(stopped.length, 'loop', 'loops')} not running` }
  if (!snapshot.ready) return { health: 'unready', title: `Not ready: ${snapshot.reasons.join('; ') || 'no reason given'}` }
  const failing = snapshot.tasks.filter((t) => t.consecutive_failures > 0)
  if (failing.length > 0) return { health: 'failing', title: `${failing.length} ${plural(failing.length, 'loop', 'loops')} failing` }
  return { health: 'ok', title: 'Ready, every loop running' }
}

const TUNNEL_LABEL: Record<string, string> = { wireguard: 'WireGuard', openvpn: 'OpenVPN' }

/** Where a gateway stands against the head, in a few words. */
function feedLabel(gateway: SystemGatewaySnapshot, feed: SystemGatewayFeed | undefined): string {
  if (gateway.feed_digest == null) return 'feed not applied yet'
  if (!feed) return 'feed applied'
  return feed.in_sync ? 'feed in sync' : 'feed behind'
}

/** The few facts a process's row shows, by the runtimes it runs. */
function processFacts(instance: SystemInstance, feed: SystemGatewayFeed | undefined): string[] {
  const snapshot = instance.snapshot
  if (!snapshot) return []
  const { api, gateway, controller, worker } = snapshot
  const facts: string[] = []
  if (controller) {
    facts.push(!controller.is_leader
      ? 'controller standing by'
      : controller.changes == null
        ? 'not compiled yet'
        : `compiled ${throughText(controller.changes)}, ${formatCount(controller.sections)} ${plural(controller.sections, 'section', 'sections')}`)
  }
  if (gateway) {
    facts.push(gateway.listening
      ? `${formatCount(gateway.active_connections)} ${plural(gateway.active_connections, 'proxy connection', 'proxy connections')}`
      : 'not listening')
    facts.push(`${formatCount(gateway.in_flight)} in flight`)
    facts.push(feedLabel(gateway, feed))
    for (const tunnel of gateway.tunnels) facts.push(`${TUNNEL_LABEL[tunnel.protocol] ?? tunnel.protocol} ${tunnel.state}`)
  }
  if (worker) {
    const inFlight = jobsInFlight(worker)
    facts.push(`${formatCount(inFlight)} ${plural(inFlight, 'job', 'jobs')} in flight`)
    const kinds = Object.keys(worker.jobs)
    if (kinds.length === 0) facts.push('takes no jobs')
    else if (kinds.length <= 3) facts.push(`takes ${kinds.join(', ')}`)
    else if (kinds.length < JOB_KINDS.length) facts.push(`takes ${kinds.length} of ${JOB_KINDS.length} job kinds`)
  }
  if (api) facts.push(`pool ${api.pool_checked_out ?? '-'} of ${api.pool_size ?? '-'} in use`)
  return facts
}

/** The entries a worker holds now, over every job kind it takes. */
function jobsInFlight(worker: SystemWorkerSnapshot): number {
  return Object.values(worker.jobs).reduce((sum, stats) => sum + (stats?.in_flight ?? 0), 0)
}

function ProcessesCard({ data, selectedId, onSelect }: {
  data: SystemTopology
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const groups = roleGroups(data)
  const processes = groups.flatMap(([, list]) => list)
  const feeds = new Map(data.feed.gateways.map((g) => [g.instance_id, g]))

  return (
    <Card className="px-4 py-3">
      <CardHeader
        title={<span className="inline-flex items-center gap-2"><Server className="w-4 h-4 text-fg-muted" />Processes</span>}
        action={<HealthBadges processes={processes} />}
        className="mb-2"
      />
      {processes.length === 0 ? (
        <p className="text-xs text-fg-muted py-2">
          {data.controller.error
            ? `No process can be listed: ${data.controller.error}`
            : 'No process is publishing a heartbeat right now.'}
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {groups.map(([role, list]) => (
            <section key={role} className="min-w-0">
              <div className="flex items-baseline gap-2 min-w-0">
                <h4 className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle flex-none">
                  {roleLabel(role)} <span className="tabular-nums">{list.length}</span>
                </h4>
                {ROLE_INFO[role] && (
                  <span className="text-[11px] text-fg-subtle truncate" title={ROLE_INFO[role].description}>
                    {ROLE_INFO[role].description}
                  </span>
                )}
              </div>
              <div className="flex flex-col -mx-1 mt-0.5">
                {list.map((instance) => (
                  <ProcessRow
                    key={instance.instance_id}
                    instance={instance}
                    feed={feeds.get(instance.instance_id)}
                    selected={instance.instance_id === selectedId}
                    onSelect={onSelect}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
      <p className="text-[11px] text-fg-subtle mt-2.5">
        Each process publishes a snapshot of itself every 5 seconds and leaves this list within about 10 seconds
        of stopping; echo processes publish none. Pick one to see its snapshot: its loops, its traffic and what it
        is waiting for.
      </p>
    </Card>
  )
}

/** How many processes have a problem, by kind, or that all are ready; and how many are live. */
function HealthBadges({ processes }: { processes: SystemInstance[] }) {
  const health = processes.map(processHealth)
  const failing = health.filter((h) => h.health === 'failing').length
  const unready = health.filter((h) => h.health === 'unready').length
  const unknown = health.filter((h) => h.health === 'unknown').length
  return (
    <div className="flex items-center gap-2 flex-wrap">
      {failing > 0 && <Badge color="red">{failing} with failing loops</Badge>}
      {unready > 0 && <Badge color="yellow">{unready} not ready</Badge>}
      {unknown > 0 && <Badge color="gray">{unknown} without a snapshot</Badge>}
      {processes.length > 0 && failing + unready + unknown === 0 && <Badge color="green">all ready</Badge>}
      <span className="text-xs text-fg-muted tabular-nums">{processes.length} live</span>
    </div>
  )
}

function ProcessRow({ instance, feed, selected, onSelect }: {
  instance: SystemInstance
  feed: SystemGatewayFeed | undefined
  selected: boolean
  onSelect: (id: string) => void
}) {
  const snapshot = instance.snapshot
  const { health, title } = processHealth(instance)
  const style = HEALTH_STYLE[health]
  const Icon = style.icon
  const facts = processFacts(instance, feed)
  const lag = snapshot?.runtime.loop_lag_max_ms ?? null

  return (
    <button
      type="button"
      onClick={() => onSelect(instance.instance_id)}
      aria-pressed={selected}
      className={cn(
        'flex items-start gap-2.5 w-full text-left px-1 py-1.5 rounded-md transition-colors',
        selected ? 'bg-surface-raised' : 'hover:bg-surface-raised'
      )}
    >
      <span title={title} className="flex-none mt-0.5"><Icon className={cn('w-3.5 h-3.5', style.className)} /></span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0">
          <span className="font-mono text-[12px] truncate" title={instance.instance_id}>{shortId(instance.instance_id)}</span>
          {snapshot?.controller?.is_leader && (
            <Badge color="green" className="px-1.5 py-0 text-[10px] inline-flex items-center gap-1 flex-none">
              <Crown className="w-2.5 h-2.5" /> leader
            </Badge>
          )}
        </div>
        <div className="text-[11px] text-fg-subtle truncate tabular-nums">
          {!snapshot
            ? 'No readable snapshot'
            : !snapshot.ready
              ? <span className="text-warning">{snapshot.reasons[0] ?? 'Not ready'}</span>
              : facts.join(' · ')}
          {lag != null && lag >= LOOP_LAG_WARN_MS && <span className="text-warning"> · loop {formatLag(lag)} behind</span>}
        </div>
      </div>
      <div className="flex-none text-right text-[11px] text-fg-subtle tabular-nums leading-[18px]">
        {snapshot && <div>v{snapshot.runtime.version} · up {formatDuration(snapshot.runtime.uptime_seconds)}</div>}
        <div title="Snapshots are published every 5 seconds; this is the last one.">
          {snapshot ? `as of ${snapshotAge(instance.age_seconds)}` : instance.role}
        </div>
      </div>
    </button>
  )
}

// --- one process ---------------------------------------------------------------------

/** Everything one process last published about itself, docked beside the page. */
function ProcessPanel({ instanceId, instance, data, onClose }: {
  instanceId: string
  instance: SystemInstance | null
  data: SystemTopology
  onClose: () => void
}) {
  if (!instance) {
    return (
      <Inspector title={shortId(instanceId, 40)} subtitle="No longer listed" onClose={onClose}>
        <Alert variant="info">
          This process is no longer listed: its heartbeat stopped, so it has stopped or is restarting. Once its
          heartbeat resumes it shows here again if OCTOPROX_INSTANCE_ID gives it a fixed id, as the cluster compose
          files do; with the default id, generated at each boot, it comes back under a new one.
        </Alert>
      </Inspector>
    )
  }
  const snapshot = instance.snapshot
  const handover = takeover(data.controller)
  return (
    <Inspector
      title={shortId(instance.instance_id, 40)}
      subtitle={snapshot
        ? `${roleName(instance.role)} · Octoprox ${snapshot.runtime.version} · as of ${snapshotAge(instance.age_seconds)}`
        : roleName(instance.role)}
      onClose={onClose}
    >
      {!snapshot ? (
        <Alert variant="info">
          The process renews its heartbeat but its snapshot could not be read: it could not build one, or a process
          of another release wrote it, as happens during an upgrade.
        </Alert>
      ) : (
        <>
          {!snapshot.ready && (
            <Alert variant="warning" className="text-sm">
              Not ready, so its <span className="font-mono">/ready</span> answers 503: {snapshot.reasons.join('; ') || 'no reason given'}.
            </Alert>
          )}
          <ProcessFacts instance={instance} snapshot={snapshot} />
          {snapshot.controller && <ControllerSection controller={snapshot.controller} handover={handover} />}
          {snapshot.gateway && (
            <GatewaySection
              gateway={snapshot.gateway}
              feed={data.feed.gateways.find((g) => g.instance_id === instance.instance_id)}
            />
          )}
          {snapshot.worker && <WorkerSection worker={snapshot.worker} />}
          {snapshot.api && <ApiSection api={snapshot.api} />}
          {(snapshot.api || snapshot.worker) && (
            <DefinitionsSection changes={snapshot.streams.changes} instance={instance} data={data} />
          )}
          <LoopsSection snapshot={snapshot} handover={handover} />
        </>
      )}
    </Inspector>
  )
}

function ProcessFacts({ instance, snapshot }: { instance: SystemInstance; snapshot: SystemInstanceSnapshot }) {
  const { runtime } = snapshot
  return (
    <InspectorSection title="Process">
      <div>
        <KeyValue label="Instance" value={<span title={instance.instance_id}>{instance.instance_id}</span>} mono />
        <KeyValue label="Role" value={roleName(instance.role)} />
        <KeyValue label="Version" value={runtime.version} />
        <KeyValue label="PID" value={runtime.pid} />
        <KeyValue label="Started" value={formatDateTime(runtime.started_at)} />
        <KeyValue label="Uptime" value={formatDuration(runtime.uptime_seconds)} />
        <KeyValue label="Event loop lag" value={<LoopLag runtime={runtime} />} />
        <KeyValue label="Ready" value={snapshot.ready ? 'yes' : <span className="text-warning">no</span>} />
      </div>
    </InspectorSection>
  )
}

// Over a second behind, background jobs made of many awaits (flushes, compiles)
// stretch from seconds to minutes; the server warns in its log at the same mark.
const LOOP_LAG_WARN_MS = 1000

function LoopLag({ runtime }: { runtime: SystemInstanceRuntime }) {
  const max = runtime.loop_lag_max_ms
  if (max == null) return <span title="Measured over a minute: no full minute yet.">-</span>
  return (
    <span
      className={cn(max >= LOOP_LAG_WARN_MS && 'text-warning')}
      title="Worst and average delay of a timer over the last minute. Every await waits about this long for its turn."
    >
      {formatLag(max)} worst, {formatLag(runtime.loop_lag_mean_ms ?? 0)} average
    </span>
  )
}

function formatLag(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`
}

function ControllerSection({ controller, handover }: { controller: SystemControllerSnapshot; handover: string }) {
  return (
    <InspectorSection
      title="Controller"
      action={controller.is_leader
        ? <Badge color="green" className="px-1.5 py-0 text-[10px]">leader</Badge>
        : <Badge color="gray" className="px-1.5 py-0 text-[10px]">standby</Badge>}
    >
      {!controller.is_leader && (
        <p className="text-xs text-fg-muted">
          Another controller holds the lease. This one runs nothing until it takes it over, {handover}; what follows is
          what it saw while it last led, if it ever did.
        </p>
      )}
      {controller.compile_error && <Alert variant="error" className="text-xs wrap-break-word">Compile failing: {controller.compile_error}</Alert>}
      {controller.publish_error && <Alert variant="error" className="text-xs wrap-break-word">Publish failing: {controller.publish_error}</Alert>}
      <div>
        <KeyValue label="Lease taken" value={`${controller.lease_terms} ${plural(controller.lease_terms, 'time', 'times')} since start`} />
        <KeyValue
          label="Last compile"
          value={controller.compiled_at
            ? `${relativeTime(controller.compiled_at)}${controller.compile_ms != null ? `, ${formatMs(controller.compile_ms)}` : ''}`
            : '-'}
        />
        <KeyValue
          label="Changes compiled"
          value={<span title={controller.changes ?? undefined}>{throughText(controller.changes)}</span>}
        />
        <KeyValue label="Compiles" value={formatCount(controller.compiles)} />
        <KeyValue
          label="Last publish"
          value={controller.published_at
            ? `${relativeTime(controller.published_at)}${controller.publish_ms != null ? `, ${formatMs(controller.publish_ms)}` : ''}`
            : '-'}
        />
        <KeyValue label="Publishes" value={formatCount(controller.publishes)} />
        <KeyValue
          label="Feed sections"
          value={<span title="Sections the feed holds now; sections publishes wrote (new or changed) and removed since this process started">
            {formatCount(controller.sections)} held · {formatCount(controller.sections_written)} written · {formatCount(controller.sections_removed)} removed
          </span>}
        />
        <KeyValue label="Quarantined proxies" value={formatCount(controller.quarantined)} />
        <KeyValue label="Connectors over their limit" value={formatCount(controller.blocked_connectors)} />
      </div>
    </InspectorSection>
  )
}

function GatewaySection({ gateway, feed }: { gateway: SystemGatewaySnapshot; feed: SystemGatewayFeed | undefined }) {
  // The feed status's age counts the snapshot's own age in; the snapshot's stopped when it was published.
  const applied = feed?.age_seconds ?? gateway.feed_age_seconds
  const failures = Object.entries(gateway.failures_by_kind)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
  return (
    <InspectorSection title="Gateway">
      <div>
        <KeyValue
          label="Proxy listener"
          value={gateway.listening
            ? `port ${gateway.listener_port}, ${formatCount(gateway.active_connections)} ${plural(gateway.active_connections, 'connection', 'connections')}`
            : <span className="text-warning">not listening (port {gateway.listener_port})</span>}
        />
        <KeyValue
          label={<span title="Relays open through this gateway now, from the proxy port and the tunnels, idle tunnel connections included.">Transfers in flight</span>}
          value={formatCount(gateway.in_flight)}
        />
        <KeyValue
          label="Routing feed"
          value={gateway.feed_digest == null
            ? <span className="text-warning">not applied yet</span>
            : <>
                {!feed ? 'applied' : feed.in_sync ? 'in sync' : <span className="text-warning">behind the head</span>}
                {applied != null && `, applied ${formatDuration(applied)} ago`}
              </>}
        />
        {gateway.feed_digest != null && (
          <KeyValue
            label="Applied feed"
            value={<span title={`Head ${gateway.feed_digest}, compiled from the change stream ${throughText(gateway.feed_changes)} (${gateway.feed_changes ?? 'no position'})`}>
              <span className="font-mono text-xs">{gateway.feed_digest.slice(0, 12)}</span>, compiled {throughText(gateway.feed_changes)}
            </span>}
          />
        )}
        <KeyValue
          label="Requests since start"
          value={`${formatCount(gateway.requests)} · ${formatCount(gateway.successes)} ok · ${formatCount(gateway.failures)} failed`}
        />
        <KeyValue label="Bytes since start" value={`${formatBytes(gateway.bytes_sent)} sent · ${formatBytes(gateway.bytes_received)} received`} />
        <KeyValue
          label="Kept out on its own"
          value={<span title="Retired session slots, and quarantines this gateway tripped that the feed does not carry yet">
            {formatCount(gateway.retired_slots)} retired · {formatCount(gateway.local_quarantines)} quarantined
          </span>}
        />
        <KeyValue label="Preflight" value={`${formatCount(gateway.preflight_checks)} checks · ${formatCount(gateway.preflight_rejections)} rejected`} />
      </div>
      {failures.length > 0 && (
        <div>
          <div className="text-[11px] text-fg-muted mb-0.5">Failed and refused requests since start</div>
          {failures.map(([kind, n]) => (
            <KeyValue key={kind} label={KIND_INFO[kind]?.label ?? kind} value={formatCount(n)} />
          ))}
        </div>
      )}
      <GatewayDatabases gateway={gateway} />
      <div>
        <div className="text-[11px] text-fg-muted mb-0.5">On the way to Redis</div>
        <KeyValue
          label="Metric entries"
          value={<span title="Entries waiting for the next write, writes Redis did not take, and rate-limit windows that could not be counted">
            {formatCount(gateway.pending_metric_deltas)} pending
            {gateway.metric_flush_failures > 0 && <span className="text-warning"> · {formatCount(gateway.metric_flush_failures)} failed writes</span>}
            {gateway.rate_limit_errors > 0 && <span className="text-warning"> · {formatCount(gateway.rate_limit_errors)} rate-limit errors</span>}
          </span>}
        />
        <BufferRow label="IP observations" pending={gateway.observations_pending} dropped={gateway.observations_dropped} />
        <BufferRow label="Preflight sightings" pending={gateway.sightings_pending} dropped={gateway.sightings_dropped} />
        <BufferRow label="Failure log" dropped={gateway.failure_log_dropped} />
        <BufferRow label="MITM captures" dropped={gateway.mitm_captures_dropped} />
      </div>
      {gateway.tunnels.length > 0 && (
        <div>
          <div className="text-[11px] text-fg-muted mb-0.5">Tunnels</div>
          {gateway.tunnels.map((tunnel) => <TunnelRow key={tunnel.protocol} tunnel={tunnel} />)}
        </div>
      )}
    </InspectorSection>
  )
}

function BufferRow({ label, pending, dropped }: { label: string; pending?: number; dropped: number }) {
  return (
    <KeyValue
      label={label}
      value={<>
        {pending !== undefined && `${formatCount(pending)} pending`}
        {pending !== undefined && ' · '}
        <span className={cn(dropped > 0 && 'text-warning')}>{formatCount(dropped)} dropped</span>
      </>}
    />
  )
}

/**
 * The IP databases a gateway has open and the ones it still waits for. The
 * heartbeat names them by id, so the names come from the database list, which
 * the IP attribution page keeps in the same cache.
 */
function GatewayDatabases({ gateway }: { gateway: SystemGatewaySnapshot }) {
  const errors = Object.entries(gateway.geo_database_errors)
  const missing = gateway.geo_databases_missing
  const { data: databases } = useQuery({
    queryKey: ['geo-databases'],
    queryFn: fetchGeoDatabases,
    staleTime: 60_000,
    enabled: errors.length > 0 || missing.length > 0,
  })
  const nameOf = (id: string) => databases?.find((d) => d.id === id)?.name ?? id.slice(0, 8)
  return (
    <div>
      <div className="text-[11px] text-fg-muted mb-0.5">IP databases</div>
      <KeyValue label="Open" value={formatCount(gateway.geo_databases_loaded.length)} />
      {missing.length > 0 && (
        <KeyValue
          label="Waiting for"
          value={<span className="text-warning" title="Listed by the feed, not fetched yet; retried every 30 seconds">{missing.map(nameOf).join(', ')}</span>}
        />
      )}
      {errors.map(([id, reason]) => (
        <KeyValue key={id} label={nameOf(id)} value={<span className="text-danger" title={reason}>{reason}</span>} />
      ))}
    </div>
  )
}

const TUNNEL_STATE_COLOR: Record<string, 'green' | 'yellow' | 'red' | 'gray'> = {
  running: 'green', starting: 'yellow', failed: 'red', stopped: 'gray',
}

function TunnelRow({ tunnel }: { tunnel: TunnelEndpointState }) {
  return (
    <div className="py-1.5 border-b border-line last:border-b-0 text-[13px]">
      <div className="flex items-center justify-between gap-3">
        <span className="text-fg-muted">{TUNNEL_LABEL[tunnel.protocol] ?? tunnel.protocol}</span>
        <span className="flex items-center gap-2 min-w-0">
          <span className="text-xs text-fg-subtle truncate tabular-nums">
            {[tunnel.interface, tunnel.listen_port != null ? `port ${tunnel.listen_port}` : null, tunnel.daemon_version]
              .filter(Boolean).join(' · ')}
          </span>
          <Badge color={TUNNEL_STATE_COLOR[tunnel.state] ?? 'gray'} className="px-1.5 py-0 text-[10px] flex-none">{tunnel.state}</Badge>
        </span>
      </div>
      {tunnel.error && <div className="text-xs text-danger wrap-break-word whitespace-pre-wrap mt-1">{tunnel.error}</div>}
    </div>
  )
}

function WorkerSection({ worker }: { worker: SystemWorkerSnapshot }) {
  // A kind a newer release adds goes last, under its own name.
  const reported = Object.keys(worker.jobs) as JobKind[]
  const kinds = [...JOB_KINDS.filter((kind) => worker.jobs[kind]), ...reported.filter((kind) => !JOB_KINDS.includes(kind))]
  return (
    <InspectorSection title="Worker">
      {kinds.length === 0 ? (
        <p className="text-xs text-fg-muted">Takes no job kind (OCTOPROX_WORKER_JOBS is empty).</p>
      ) : (
        <table className="w-full text-[12.5px]">
          <thead>
            <tr className="text-[11px] text-fg-muted text-right">
              <th className="font-normal text-left pb-1">Job</th>
              <th className="font-normal pb-1 pl-3">Runs</th>
              <th className="font-normal pb-1 pl-3">Failed</th>
              <th className="font-normal pb-1 pl-3">Avg</th>
              <th className="font-normal pb-1 pl-3">In flight</th>
            </tr>
          </thead>
          <tbody>
            {kinds.map((kind) => {
              const stats = worker.jobs[kind]!
              return (
                <tr key={kind} className="border-t border-line text-right tabular-nums">
                  <td className="text-left py-1.5 pr-3">{JOB_INFO[kind]?.label ?? kind}</td>
                  <td className="py-1.5 pl-3" title={`${stats.runs.toLocaleString()} batches`}>{formatCount(stats.runs)}</td>
                  <td className={cn('py-1.5 pl-3', stats.failures > 0 && 'text-danger')}>{formatCount(stats.failures)}</td>
                  <td className="py-1.5 pl-3">{stats.runs > 0 ? formatMs((stats.duration_seconds / stats.runs) * 1000) : '-'}</td>
                  <td className="py-1.5 pl-3">{formatCount(stats.in_flight)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      <div>
        <IngestRow label="Preflight sightings" done="attributed" consumed={worker.sightings_consumed} dropped={worker.sightings_dropped} />
        <IngestRow label="IP observations" done="ingested" consumed={worker.observations_consumed} dropped={worker.observations_dropped} />
      </div>
    </InspectorSection>
  )
}

/** What a worker took off a work stream since it started, and lost with batches given up after five deliveries. */
function IngestRow({ label, done, consumed, dropped }: { label: string; done: string; consumed: number; dropped: number }) {
  return (
    <KeyValue
      label={label}
      value={<>
        {formatCount(consumed)} {done}
        {dropped > 0 && <span className="text-warning" title="Lost with batches given up after five deliveries"> · {formatCount(dropped)} lost</span>}
      </>}
    />
  )
}

function ApiSection({ api }: { api: SystemApiSnapshot }) {
  return (
    <InspectorSection title="API">
      <div>
        <KeyValue
          label="Postgres pool"
          value={api.pool_size != null
            ? `${api.pool_checked_out ?? 0} of ${api.pool_size} in use${api.pool_overflow ? `, ${api.pool_overflow} over` : ''}`
            : '-'}
        />
      </div>
    </InspectorSection>
  )
}

// How each kind of full read is counted, in the order the page lists them.
const FULL_READS: { reason: FullReadReason; one: string; many: string }[] = [
  { reason: 'start', one: 'at start', many: 'at start' },
  { reason: 'reconcile', one: 'reconcile', many: 'reconciles' },
  { reason: 'gap', one: 'after a gap', many: 'after gaps' },
  { reason: 'reconnect', one: 'after a failed read', many: 'after failed reads' },
]

/**
 * The definitions an api process or a worker validates and judges with: how
 * far it has read the change stream against the stream's head, and how often
 * it read them in full instead.
 */
function DefinitionsSection({ changes, instance, data }: {
  changes: SystemStreamState | undefined
  instance: SystemInstance
  data: SystemTopology
}) {
  const at = standing(changes?.position ?? null, data.controller.changes_head, snapshotMs(data, instance))
  const words = at ? standingText(at, 'Read') : null
  const fullReads = FULL_READS
    .map(({ reason, one, many }) => [changes?.full_reads[reason] ?? 0, one, many] as const)
    .filter(([n]) => n > 0)
    .map(([n, one, many]) => `${formatCount(n)} ${plural(n, one, many)}`)
  return (
    <InspectorSection title="Definitions">
      <div>
        <KeyValue
          label="Change stream"
          value={words
            ? <span className={cn(at?.late && 'text-warning')} title={words.title}>{words.text}</span>
            : 'not read yet'}
        />
        <KeyValue
          label="Read in full"
          value={<span title={changes?.last_full_read_at ? `Last full read ${formatDateTime(changes.last_full_read_at)}` : undefined}>
            {fullReads.length > 0 ? fullReads.join(' · ') : 'never'}
          </span>}
        />
      </div>
      <p className="text-[11px] text-fg-subtle">
        Provider descriptors, IP attribution settings and the IP database list, read again when the change stream
        announces a change to one of them. A follower reads in full at start, every reconcile interval, after a gap
        (it fell behind the stream's cap, or Redis lost the stream) and after a failed read.
      </p>
    </InspectorSection>
  )
}

// --- loops of one process --------------------------------------------------------------

const WORKER_STATE: Record<WorkerState, { icon: typeof CheckCircle2; className: string; label: string }> = {
  running: { icon: CheckCircle2, className: 'text-success', label: 'running' },
  failed: { icon: AlertTriangle, className: 'text-danger', label: 'failed' },
  cancelled: { icon: CircleOff, className: 'text-fg-subtle', label: 'cancelled' },
  done: { icon: CircleOff, className: 'text-warning', label: 'stopped' },
}

function LoopsSection({ snapshot, handover }: { snapshot: SystemInstanceSnapshot; handover: string }) {
  const { tasks } = snapshot
  // Only the controller's loops are singletons; they run in the process holding its lease.
  const leads = snapshot.controller?.is_leader ?? false
  const stopped = tasks.filter((t) => t.state !== 'running').length
  // A loop that is still alive but failing every cycle looks fine from the
  // task state alone; the run counters are what surface it.
  const failing = tasks.filter((t) => t.state === 'running' && t.consecutive_failures > 0).length
  // Behind *now*, not "was behind once" - a blip during startup should not
  // colour the section for the rest of the process's life.
  const behind = tasks.filter((t) => t.state === 'running' && t.consecutive_overruns > 0).length

  return (
    <InspectorSection
      title="Background loops"
      action={
        stopped > 0
          ? <Badge color="red">{stopped} not running</Badge>
          : failing > 0
            ? <Badge color="yellow">{failing} failing</Badge>
            : behind > 0
              ? <Badge color="yellow">{behind} over cadence</Badge>
              : <Badge color="green">all running</Badge>
      }
    >
      <div className="-mx-1">
        {tasks.map((task) => <WorkerRow key={task.name} task={task} leads={leads} handover={handover} />)}
      </div>
      <p className="text-[11px] text-fg-subtle">
        Run counts are this process's, since it started, and each loop is listed with the cadence it was started on -
        a cycle slower than its cadence delays the next one. The controller's loops run only in the controller holding
        its lease. Published on the process's heartbeat, so a few seconds behind.
      </p>
    </InspectorSection>
  )
}

/**
 * Where a singleton stands. Every singleton in this release is one of the
 * controller's loops, which all run in the one controller holding the
 * controller lease; any other controller's copies stand by.
 */
function LeaseBadge({ leads, handover }: { leads: boolean; handover: string }) {
  return leads
    ? (
      <Badge color="green" className="px-1.5 py-0 text-[10px] font-normal" title="This process holds the controller lease, so it runs the loop.">
        leader · runs here
      </Badge>
    )
    : (
      <Badge color="gray" className="px-1.5 py-0 text-[10px] font-normal" title={`Another controller holds the lease; this one stands by and takes it over ${handover}.`}>
        standing by
      </Badge>
    )
}

function WorkerRow({ task, leads, handover }: { task: SystemWorkerTask; leads: boolean; handover: string }) {
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
          {task.scope === 'singleton' && <LeaseBadge leads={leads} handover={handover} />}
        </div>
        <div className={cn('text-[11px]', problem ? 'text-danger' : 'text-fg-subtle')}>
          {problem ?? task.description}
        </div>
        <WorkerRuns task={task} leads={leads} />
      </div>
      <span className={cn('text-[11px] flex-none', state.className)}>{state.label}</span>
    </div>
  )
}

/** Cycle counters: what the loop has actually done, as opposed to whether it exists. */
function WorkerRuns({ task, leads }: { task: SystemWorkerTask; leads: boolean }) {
  const cadence = task.interval_seconds !== null ? formatInterval(task.interval_seconds) : 'no fixed cadence'
  if (task.runs === 0) {
    const scope = task.scope === 'singleton' && !leads ? 'standing by, no runs' : 'no runs yet'
    return <div className="text-[11px] text-fg-subtle">{cadence}, {scope}</div>
  }
  // Both counters below are coloured on the streak, not the lifetime total: a
  // loop that hit one slow cycle hours ago and has been fine since is not a
  // problem now, and saying otherwise trains people to ignore the colour.
  const failingNow = task.consecutive_failures > 0
  const behindNow = task.consecutive_overruns > 0
  // Ticks that found nothing to do. Shown only where they happen, so the rows
  // for loops that always have work stay as short as they were - and where
  // they do happen, the run count alone is misleading: an idle gateway's
  // request metering reaches five figures a day without writing anything.
  // Neither number is a problem on its own, so this stays uncoloured.
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

// --- controller and feed -------------------------------------------------------------

/**
 * How soon a standby controller takes the lease over, from the numbers the
 * controllers run with: within a poll of a clean stop, which gives the lease
 * up, and within the lease's TTL and a poll of a failure, which leaves it to
 * run out. Rounded up, so neither reads shorter than it is.
 */
function takeover(status: SystemControllerStatus): string {
  const seconds = (ms: number) => `${Math.ceil(ms / 1000)}s`
  return `within ${seconds(status.standby_poll_ms)} of the leader stopping cleanly, or within ${seconds(status.lease_ttl_ms + status.standby_poll_ms)} of it failing`
}

function ControllerCard({ data, onSelect }: { data: SystemTopology; onSelect: (id: string) => void }) {
  const { controller } = data
  const processes = allProcesses(data)
  const leader = controller.leader ? processes.find((i) => i.instance_id === controller.leader) ?? null : null
  const led = leader?.snapshot?.controller ?? null
  const standbys = processes.filter((i) => i.snapshot?.controller && i.instance_id !== controller.leader).length
  // The leader's compile against the change stream's head: behind for a moment
  // after every save, a problem when it stays.
  const compiled = leader ? standing(controller.changes, controller.changes_head, snapshotMs(data, leader)) : null
  const words = compiled ? standingText(compiled, 'Compiled') : null
  const handover = takeover(controller)
  const failing = led?.compile_error ?? led?.publish_error ?? null
  const status = controller.error
    ? <Badge color="red">unreadable</Badge>
    : !controller.leader
      ? <Badge color="red" title="No controller holds the lease: none is running, or a failover is under way.">no leader</Badge>
      : failing
        ? <Badge color="red" title={failing}>failing</Badge>
        : <Badge color="green">leading</Badge>

  return (
    <Card className="px-4 py-3">
      <CardHeader
        title={<span className="inline-flex items-center gap-2"><Crown className="w-4 h-4 text-fg-muted" />Controller</span>}
        action={status}
        className="mb-2.5"
      />
      {controller.error && <Alert variant="error" className="mb-2.5">{controller.error}</Alert>}
      {failing && <Alert variant="error" className="mb-2.5 text-xs wrap-break-word">{failing}</Alert>}
      <div className="grid grid-cols-2 @lg:grid-cols-3 gap-x-4 gap-y-2">
        <Fact
          label="Leader"
          value={controller.leader ? (
            <button type="button" className="font-mono text-[12px] text-primary hover:brightness-110 truncate max-w-full" onClick={() => onSelect(controller.leader!)} title={controller.leader}>
              {shortId(controller.leader, 24)}
            </button>
          ) : 'none'}
        />
        <Fact label="Standing by" value={standbys} hint={`Controllers ready to take the lease over, ${handover}`} />
        <Fact
          label="Changes"
          value={words
            ? <span className={cn(compiled?.late && 'text-warning')}>{words.text}</span>
            : controller.leader && !controller.error ? 'not compiled yet' : '-'}
          hint={`${words ? `${words.title} ` : ''}A save is compiled when its change arrives on the change stream, at most once per compile interval, and the leader's heartbeat reports the compile within 5 seconds.`}
        />
        <Fact label="Compiled" value={relativeTime(controller.compiled_at)} hint={formatDateTime(controller.compiled_at)} />
        <Fact
          label="Last publish"
          value={led?.published_at ? relativeTime(led.published_at) : '-'}
          hint={led?.publish_ms != null ? `Took ${formatMs(led.publish_ms)}` : undefined}
        />
        <Fact label="Feed sections" value={led ? formatCount(led.sections) : '-'} />
        <Fact label="Quarantined proxies" value={led ? formatCount(led.quarantined) : '-'} />
        <Fact label="Over traffic limit" value={led ? `${led.blocked_connectors} ${plural(led.blocked_connectors, 'connector', 'connectors')}` : '-'} />
      </div>
      <p className="text-[11px] text-fg-subtle mt-2.5">
        One controller holds the lease: it compiles the routing feed and publishes the periodic jobs the workers run,
        nothing else. Another takes it over {handover}.
      </p>
    </Card>
  )
}

function FeedCard({ data, onSelect }: { data: SystemTopology; onSelect: (id: string) => void }) {
  const { feed } = data
  const inSync = feed.gateways.filter((g) => g.in_sync).length
  const total = feed.gateways.length
  const status = feed.error
    ? <Badge color="red">unreadable</Badge>
    : !feed.digest
      ? <Badge color="red" title="No controller has published a feed yet, or Redis lost it and the leader has not written it again yet.">no feed</Badge>
      : total === 0
        ? <Badge color="gray">no gateways</Badge>
        : <Badge color={inSync === total ? 'green' : 'yellow'}>{inSync} of {total} in sync</Badge>

  return (
    <Card className="px-4 py-3">
      <CardHeader
        title={<span className="inline-flex items-center gap-2"><GitBranch className="w-4 h-4 text-fg-muted" />Routing feed</span>}
        action={status}
        className="mb-2.5"
      />
      {feed.error && <Alert variant="error" className="mb-2.5">{feed.error}</Alert>}
      <div className="grid grid-cols-2 @lg:grid-cols-4 gap-x-4 gap-y-2 mb-2.5">
        <Fact label="Head" value={<span className="font-mono text-[12px]">{feed.digest ? feed.digest.slice(0, 12) : '-'}</span>} hint={feed.digest ?? undefined} />
        <Fact
          label="Changes"
          value={throughText(feed.changes)}
          hint={feed.changes ? `How far the change stream had been read by the compile that made the head (${feed.changes})` : undefined}
        />
        <Fact label="Compiled" value={relativeTime(feed.compiled_at)} hint={formatDateTime(feed.compiled_at)} />
        <Fact label="Sections" value={feed.sections != null ? formatCount(feed.sections) : '-'} />
      </div>
      {total > 0 ? (
        <div className="flex flex-col -mx-1">
          {feed.gateways.map((gateway) => <GatewayFeedRow key={gateway.instance_id} gateway={gateway} onSelect={onSelect} />)}
        </div>
      ) : !feed.error && (
        <p className="text-xs text-fg-muted py-1">No gateway is publishing a heartbeat, so nothing routes on the feed right now.</p>
      )}
      <p className="text-[11px] text-fg-subtle mt-2.5">
        Gateways follow the feed stream and fetch only the sections that changed. One reads behind after each publish
        until its next heartbeat reports the apply; one that stays behind says why on its own snapshot.
      </p>
    </Card>
  )
}

function GatewayFeedRow({ gateway, onSelect }: { gateway: SystemGatewayFeed; onSelect: (id: string) => void }) {
  const never = gateway.digest === null
  return (
    <button
      type="button"
      onClick={() => onSelect(gateway.instance_id)}
      className="flex items-center gap-2 w-full text-left px-1 py-1 rounded-md hover:bg-surface-raised transition-colors text-[12.5px]"
    >
      <span className={cn('w-2 h-2 rounded-full flex-none', never ? 'bg-fg-subtle/40' : gateway.in_sync ? 'bg-success' : 'bg-warning')} />
      <span className="font-mono text-[12px] truncate min-w-0" title={gateway.instance_id}>{shortId(gateway.instance_id)}</span>
      <span className="flex-1" />
      <span className="text-[11px] text-fg-subtle tabular-nums flex-none" title={gateway.changes ?? undefined}>
        {never
          ? 'not applied yet'
          : `${gateway.in_sync ? 'in sync' : behindText(parseStreamId(gateway.changes))}${gateway.age_seconds != null ? ` · applied ${formatDuration(gateway.age_seconds)} ago` : ''}`}
      </span>
    </button>
  )
}

// --- change stream positions ---------------------------------------------------------

/*
 * A position in a stream is an entry ID, which carries the milliseconds Redis
 * stamped the entry with, so the page shows it as that time. 0-0 is a
 * position taken while there was no stream: before any change was announced.
 */

/** A position as the time its ID carries: the clock time, with the date when it is not today. */
function positionClock(position: StreamId): string {
  const at = new Date(position.ms)
  return at.toDateString() === new Date().toDateString()
    ? at.toLocaleTimeString()
    : at.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' })
}

function isStreamStart(position: StreamId): boolean {
  return position.ms === 0 && position.seq === 0
}

/** How far a read or a compile got, after its verb: "through 14:03:12", or "before any change". */
function positionText(position: StreamId): string {
  return isStreamStart(position) ? 'before any change' : `through ${positionClock(position)}`
}

/** A position as positionText words it; '-' for none. */
function throughText(position: string | null): string {
  const at = parseStreamId(position)
  return at ? positionText(at) : '-'
}

/** A position behind the head, as a row says it: "behind, at 14:03:12". */
function behindText(position: StreamId | null): string {
  return position && !isStreamStart(position) ? `behind, at ${positionClock(position)}` : 'behind'
}

// A follower reads a change within a second and the controller compiles it
// within a compile interval and the compile itself, and either reports it on
// its next heartbeat: a position still before a change this much older than
// the snapshot is a problem, not the heartbeat's age.
const BEHIND_WARN_SECONDS = 30

/** Where a position stands against the change stream's head. */
interface Standing {
  position: StreamId
  /** The stream's newest entry; null while there is no stream, so nothing is behind it. */
  head: StreamId | null
  behind: boolean
  /** Behind a change it has had time to read: one older than its snapshot by BEHIND_WARN_SECONDS. */
  late: boolean
}

/** A process's position against the head, its snapshot taken at `snapshotAt` (ms); null before its first read. */
function standing(position: string | null, head: string | null, snapshotAt: number): Standing | null {
  const at = parseStreamId(position)
  if (!at) return null
  const newest = parseStreamId(head)
  if (!newest || !streamIdBefore(at, newest)) return { position: at, head: newest, behind: false, late: false }
  return { position: at, head: newest, behind: true, late: newest.ms < snapshotAt - BEHIND_WARN_SECONDS * 1000 }
}

/** A standing in words, "in sync" or "behind, at <time>", and the title that gives both positions. */
function standingText(at: Standing, verb: string): { text: string; title: string } {
  const done = `${verb} ${positionText(at.position)}`
  if (!at.head) return { text: 'in sync', title: `${done}; there is no change stream yet, so nothing is behind it.` }
  if (!at.behind) return { text: 'in sync', title: `${done}, the newest change.` }
  const gap = Math.max(0, (at.head.ms - at.position.ms) / 1000)
  return {
    text: behindText(at.position),
    title: `${done}; the newest change is from ${positionClock(at.head)}${isStreamStart(at.position) ? '' : `, ${formatDuration(gap)} later`}.`,
  }
}

/** When the api made this answer (ms, its clock); the browser's clock if the time cannot be read. */
function readingMs(data: SystemTopology): number {
  return parseApiDate(data.generated_at)?.getTime() ?? Date.now()
}

/** When a process published the snapshot this answer carries (ms, the api's clock). */
function snapshotMs(data: SystemTopology, instance: SystemInstance): number {
  return readingMs(data) - (instance.age_seconds ?? 0) * 1000
}

// --- work streams --------------------------------------------------------------------

/**
 * Each job kind's stream: what the page calls it, and what one entry asks a
 * worker to do. How often the controller publishes a periodic kind is the
 * job producer's description, in the controller's panel.
 */
const JOB_INFO: Record<JobKind, { label: string; description: string }> = {
  health: { label: 'Health checks', description: 'One per proxy each period, spread over it, and one for each new proxy' },
  sync: { label: 'Provider syncs', description: 'One per syncable connector each period and after an edit: discovery and IP refresh' },
  scale: { label: 'Scale checks', description: 'One per cloud connector each period and after an edit: scaling, rotation, drains' },
  provision: { label: 'Provisioning', description: 'Country slot groups gateways asked for on demand' },
  reattribute: { label: 'Re-attribution', description: 'Every proxy, a project or a connector, re-judged after a location, setting or IP database change' },
  enrich: { label: 'Exit lookups', description: 'Newly added static proxies, looked up through themselves' },
  traffic: { label: 'Traffic decisions', description: 'Traffic-limited connectors to decide on: after metering carried their bytes, after a limit edit or a reset, and again while blocked or pending' },
  metrics_flush: { label: 'Metrics flushes', description: 'The request counters moved from Redis into Postgres' },
  compaction: { label: 'Compaction', description: 'Old metric rows rolled up and the retentions applied' },
  snapshot: { label: 'System snapshots', description: "The install's gauges recorded for the trend charts" },
  geo_update: { label: 'IP database updates', description: 'The scheduled IP database downloads that are due' },
  tunnel_last_seen: { label: 'Tunnel last seen', description: 'When each tunnel device was last seen, recorded on its row' },
  index_hygiene: { label: 'Index hygiene', description: 'Stopped processes, workers and ended quarantines pruned, and blocked or pending connectors decided again' },
  update_check: { label: 'Update check', description: 'The release notes octoprox.com lists, fetched once a day to tell admins about a newer release' },
}

/** The work streams that are not a job kind's. */
const INGEST_INFO: Record<string, { label: string; description: string }> = {
  sightings: { label: 'Preflight sightings', description: 'Exits preflight saw on pooled session slots, for the workers to attribute' },
  observations: { label: 'IP observations', description: 'IP observations and exit judgements from every process, for the workers to write to Postgres' },
}

const JOB_STREAM_PREFIX = 'jobs.'

/** A work stream's label and description, by its name; a name this page does not know shows as it is. */
function streamInfo(name: string): { label: string; description: string } {
  const info = name.startsWith(JOB_STREAM_PREFIX)
    ? JOB_INFO[name.slice(JOB_STREAM_PREFIX.length) as JobKind]
    : INGEST_INFO[name]
  return info ?? { label: name, description: '' }
}

const NO_WORKER = 'queued with no live worker: no worker takes this kind'
const TOO_FEW_WORKERS = 'queued stays above zero: too few workers for this kind'

// A backlog in every reading for this long is one the workers are not keeping
// up with, rather than a burst of periodic jobs a reading happened to land in.
const BACKLOG_STAYS_SECONDS = 60

interface StreamHint {
  text: string
  tone: 'danger' | 'warning'
}

/**
 * What a work stream says about its workers, given how many live workers take
 * it and how long its backlog has lasted (seconds); null when nothing. No
 * consumer in the group, or none of the live workers, means no worker takes it.
 */
function streamHint(stream: SystemWorkStream, takers: number, queuedFor: number): StreamHint | null {
  if (stream.queued === 0) return null
  if (stream.consumers === 0 || takers === 0) return { text: NO_WORKER, tone: 'danger' }
  return queuedFor >= BACKLOG_STAYS_SECONDS ? { text: TOO_FEW_WORKERS, tone: 'warning' } : null
}

/** The heartbeats of the live workers, every process running a worker runtime whose snapshot the page can read. */
function liveWorkers(data: SystemTopology): SystemWorkerSnapshot[] {
  return allProcesses(data).flatMap((instance) => (instance.snapshot?.worker ? [instance.snapshot.worker] : []))
}

/**
 * How many live workers take a work stream: for a job kind's stream those whose
 * heartbeat lists the kind, for any other every one, since each worker ingests
 * the sightings and the observations. A worker that stops leaves the count when
 * its heartbeat ends, while the group keeps it as a consumer until it is pruned.
 */
function takersOf(workers: SystemWorkerSnapshot[], name: string): number {
  if (!name.startsWith(JOB_STREAM_PREFIX)) return workers.length
  const kind = name.slice(JOB_STREAM_PREFIX.length) as JobKind
  return workers.filter((worker) => worker.jobs[kind] != null).length
}

/**
 * Since when (ms, the api's clock) each work stream has had entries queued in
 * every reading this page made; a stream with none queued in the latest
 * reading is absent.
 */
function useQueuedSince(data: SystemTopology): Record<string, number> {
  const [seen, setSeen] = useState(() => ({ at: data.generated_at, since: queuedSince({}, data) }))
  if (seen.at === data.generated_at) return seen.since
  // A new reading: carry each backlog's start over while it lasts (React's
  // pattern for state that follows a prop).
  const next = { at: data.generated_at, since: queuedSince(seen.since, data) }
  setSeen(next)
  return next.since
}

function queuedSince(previous: Record<string, number>, data: SystemTopology): Record<string, number> {
  const at = readingMs(data)
  const since: Record<string, number> = {}
  for (const [name, stream] of Object.entries(data.streams.by_name)) {
    if (stream.queued > 0) since[name] = previous[name] ?? at
  }
  return since
}

function WorkStreamsCard({ data }: { data: SystemTopology }) {
  const { streams } = data
  const rows = streamRows(data, useQueuedSince(data))
  const groups = [
    { label: 'Jobs', rows: rows.filter((row) => row.name.startsWith(JOB_STREAM_PREFIX)) },
    { label: 'Ingest', rows: rows.filter((row) => !row.name.startsWith(JOB_STREAM_PREFIX)) },
  ].filter((group) => group.rows.length > 0)
  const noWorker = rows.filter((row) => row.hint?.tone === 'danger').length
  const tooFew = rows.filter((row) => row.hint?.tone === 'warning').length

  return (
    <Card className="px-4 py-3">
      <CardHeader
        title={<span className="inline-flex items-center gap-2"><ListChecks className="w-4 h-4 text-fg-muted" />Work streams</span>}
        action={(noWorker > 0 || tooFew > 0) && (
          <div className="flex items-center gap-2 flex-wrap">
            {noWorker > 0 && <Badge color="red">{noWorker} with no worker</Badge>}
            {tooFew > 0 && <Badge color="yellow">{tooFew} short of workers</Badge>}
          </div>
        )}
        className="mb-1.5"
      />
      {streams.error ? (
        <Alert variant="error">{streams.error}</Alert>
      ) : rows.length === 0 ? (
        <p className="text-xs text-fg-muted py-2">Redis reported no work stream.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[12.5px]">
            <thead>
              <tr className="text-[11px] text-fg-muted text-right">
                <th className="font-normal text-left pb-1">Stream</th>
                <th className="font-normal pb-1 pl-3" title="Entries on the stream: queued and running, since a worker deletes each entry once it has handled it">Length</th>
                <th className="font-normal pb-1 pl-3" title="Delivered to a worker and not acknowledged yet: running, or waiting to be reclaimed (a worker stopped, a batch failed, or its acknowledgement was refused)">Pending</th>
                <th className="font-normal pb-1 pl-3" title="Not delivered to any worker yet: the backlog">Queued</th>
                <th className="font-normal pb-1 pl-3" title="Worker processes in the group; one that stopped counts until index hygiene prunes it, within the hour">Consumers</th>
                <th className="font-normal pb-1 pl-3" title="Age of the oldest entry, queued or running, by Redis's clock">Oldest</th>
              </tr>
            </thead>
            {groups.map((group) => (
              <tbody key={group.label}>
                <tr>
                  <td colSpan={6} className="pt-2 pb-0.5 text-[11px] font-semibold uppercase tracking-wider text-fg-subtle">{group.label}</td>
                </tr>
                {group.rows.map((row) => <WorkStreamRow key={row.name} {...row} />)}
              </tbody>
            ))}
          </table>
        </div>
      )}
      <p className="text-[11px] text-fg-subtle mt-2">
        Read live from Redis. Workers take every stream through one consumer group and delete each entry once they
        have handled it, so a stream holds only the work in hand. Queued stays above zero: too few workers for this
        kind. Queued with no live worker: no worker takes this kind, by the heartbeats of the workers running now,
        while the group keeps a stopped worker as a consumer until index hygiene prunes it.
      </p>
    </Card>
  )
}

function WorkStreamRow({ name, stream, hint }: { name: string; stream: SystemWorkStream; hint: StreamHint | null }) {
  const info = streamInfo(name)
  const tone = hint ? (hint.tone === 'danger' ? 'text-danger' : 'text-warning') : null
  return (
    <tr className={cn('border-t border-line text-right tabular-nums align-top', stream.length === 0 && 'text-fg-subtle')}>
      <td className="text-left py-1.5 pr-3 min-w-0" title={info.description ? `${name}: ${info.description}` : name}>
        <div>{info.label}</div>
        {hint && <div className={cn('text-[11px]', tone)}>{hint.text}</div>}
      </td>
      <td className="py-1.5 pl-3" title={stream.length.toLocaleString()}>{formatCount(stream.length)}</td>
      <td className="py-1.5 pl-3" title={stream.pending.toLocaleString()}>{formatCount(stream.pending)}</td>
      <td className={cn('py-1.5 pl-3 font-medium', tone)} title={stream.queued.toLocaleString()}>{formatCount(stream.queued)}</td>
      <td className={cn('py-1.5 pl-3', hint?.tone === 'danger' && 'text-danger')}>{stream.consumers}</td>
      <td className="py-1.5 pl-3">{stream.oldest_seconds == null ? '-' : formatAge(stream.oldest_seconds)}</td>
    </tr>
  )
}

/** An entry's age: under a second reads as such rather than as 0s. */
function formatAge(seconds: number): string {
  return seconds < 1 ? '<1s' : formatDuration(seconds)
}

// --- topology ------------------------------------------------------------------------

/*
 * The cluster as a picture: a box per role with its processes, and arrows for
 * what passes between them. No process calls another, so every arrow between
 * two roles is a Redis stream or Postgres, and the ones that matter say what
 * this reading found on them.
 */

// The runtimes a process can run, one box of the picture each.
const RUNTIMES = ['api', 'controller', 'gateway', 'worker'] as const
type Runtime = (typeof RUNTIMES)[number]

/**
 * The boxes a process sits in: each runtime its snapshot carries, so a process
 * of role 'all' shows in all four; by its role while it has no snapshot, or
 * one that carries no section yet.
 */
function runtimesOf(instance: SystemInstance): Runtime[] {
  const snapshot = instance.snapshot
  const carried = snapshot ? RUNTIMES.filter((runtime) => snapshot[runtime] != null) : []
  if (carried.length > 0) return carried
  if (instance.role === 'all') return [...RUNTIMES]
  return RUNTIMES.filter((runtime) => runtime === instance.role)
}

/** A process's counters per second, between two of its snapshots. */
interface ProcessRates {
  requests: number
  bytes: number
  runs: number
}

interface RateSample extends ProcessRates {
  uptime: number
}

interface RateState {
  at: string | null
  samples: Record<string, RateSample>
  rates: Record<string, ProcessRates>
}

// Two snapshots closer than this on the process's clock are one snapshot read
// twice: the topology polls at about the heartbeat's own cadence.
const SAME_SNAPSHOT_SECONDS = 1

function rateSample(snapshot: SystemInstanceSnapshot): RateSample {
  const gateway = snapshot.gateway
  return {
    uptime: snapshot.runtime.uptime_seconds,
    requests: gateway?.requests ?? 0,
    bytes: (gateway?.bytes_sent ?? 0) + (gateway?.bytes_received ?? 0),
    runs: Object.values(snapshot.worker?.jobs ?? {}).reduce((sum, stats) => sum + (stats?.runs ?? 0), 0),
  }
}

/**
 * Each process's rates, from counters that run since it started: the change
 * between two of its snapshots over the change in its uptime, so they are
 * timed on the process's own clock and a snapshot read twice counts once. A
 * process has none until its second snapshot, and starts over after a restart.
 */
function useProcessRates(data: SystemTopology): Record<string, ProcessRates> {
  const [seen, setSeen] = useState(() => nextRates({ at: null, samples: {}, rates: {} }, data))
  if (seen.at === data.generated_at) return seen.rates
  // A new reading (React's pattern for state that follows a prop).
  const next = nextRates(seen, data)
  setSeen(next)
  return next.rates
}

function nextRates(seen: RateState, data: SystemTopology): RateState {
  const samples: Record<string, RateSample> = {}
  const rates: Record<string, ProcessRates> = {}
  for (const instance of allProcesses(data)) {
    if (!instance.snapshot) continue
    const id = instance.instance_id
    const sample = rateSample(instance.snapshot)
    const prev = seen.samples[id]
    const span = prev ? sample.uptime - prev.uptime : -1
    if (prev && span >= 0 && span < SAME_SNAPSHOT_SECONDS) {
      samples[id] = prev
      if (seen.rates[id]) rates[id] = seen.rates[id]
    } else if (prev && span > 0 && sample.requests >= prev.requests && sample.bytes >= prev.bytes && sample.runs >= prev.runs) {
      samples[id] = sample
      rates[id] = {
        requests: (sample.requests - prev.requests) / span,
        bytes: (sample.bytes - prev.bytes) / span,
        runs: (sample.runs - prev.runs) / span,
      }
    } else {
      samples[id] = sample
    }
  }
  return { at: data.generated_at, samples, rates }
}

/** A rate in a few characters: one decimal below 10, whole above. */
function formatRate(perSecond: number): string {
  if (perSecond <= 0) return '0'
  if (perSecond < 0.1) return '<0.1'
  return perSecond < 10 ? perSecond.toFixed(1).replace(/\.0$/, '') : compactCount(Math.round(perSecond))
}

function formatByteRate(perSecond: number): string {
  return `${bytesShort(Math.round(perSecond))}/s`
}

/** The sum of one rate over the processes that have one; null when none has yet. */
function sumRates(instances: SystemInstance[], rates: Record<string, ProcessRates>, key: keyof ProcessRates): number | null {
  const known = instances.flatMap((instance) => (rates[instance.instance_id] ? [rates[instance.instance_id][key]] : []))
  return known.length > 0 ? known.reduce((sum, n) => sum + n, 0) : null
}

function TopologyCard({ data, error, selectedId, onSelect }: {
  data: SystemTopology | undefined
  error: unknown
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  return (
    <Card className="px-4 py-3">
      <CardHeader
        title={<span className="inline-flex items-center gap-2"><Network className="w-4 h-4 text-fg-muted" />Topology</span>}
        action={data && <HealthBadges processes={allProcesses(data)} />}
        className="mb-3"
      />
      {error != null && <Alert variant="error" className="mb-3">{(error as Error).message || 'Failed to load the topology'}</Alert>}
      {data?.controller.error && <Alert variant="error" className="mb-3">{data.controller.error}</Alert>}
      {!data ? (
        <p className="text-xs text-fg-muted py-8 text-center">{error ? 'No data' : 'Loading…'}</p>
      ) : (
        <Topology data={data} selectedId={selectedId} onSelect={onSelect} />
      )}
      <p className="text-[11px] text-fg-subtle mt-3">
        Every arrow between two roles is a Redis stream or Postgres: no process calls another. Read every 5 seconds; a rate is the change
        between two heartbeats of a process, so it shows within 10 seconds of opening the view. Pick a process to see
        its snapshot.
      </p>
    </Card>
  )
}

// Where the arrows between boxes in a row meet them: the middle of the box's
// header, which every box has the same height of, so those arrows run level.
const HEADER_MID = 19

// The work streams box lists the busiest streams, this many at most.
const STREAM_ROWS = 6

type TopologyLayout = 'wide' | 'tall'

// Five columns with labelled arrows between them need about this much room.
// Below it the same picture turns a quarter and runs top to bottom, three
// columns wide, so no box is left out and still no arrow crosses another.
const WIDE_MIN_PX = 1040

type BoxId = 'admins' | 'api' | 'controller' | 'gateway' | 'clients' | 'streams' | 'postgres' | 'worker' | 'upstream'

const GRID: Record<TopologyLayout, string> = {
  wide: 'grid-cols-[96px_1fr_1fr_1fr_112px] gap-x-[84px]',
  tall: 'grid-cols-3 gap-x-10',
}

// Where each box sits. The tall layout reads the wide one's rows as columns.
const PLACE: Record<TopologyLayout, Record<BoxId, string>> = {
  wide: {
    admins: 'col-start-1 row-start-1',
    api: 'col-start-2 row-start-1',
    controller: 'col-start-3 row-start-1',
    gateway: 'col-start-4 row-start-1',
    clients: 'col-start-5 row-start-1',
    streams: 'col-start-3 row-start-2',
    postgres: 'col-start-2 row-start-3',
    worker: 'col-start-3 row-start-3',
    upstream: 'col-start-5 row-start-3',
  },
  tall: {
    admins: 'col-start-1 row-start-1',
    api: 'col-start-1 row-start-2',
    controller: 'col-start-1 row-start-3',
    gateway: 'col-start-1 row-start-4',
    clients: 'col-start-1 row-start-5',
    streams: 'col-start-2 row-start-3',
    postgres: 'col-start-3 row-start-2',
    worker: 'col-start-3 row-start-3',
    upstream: 'col-start-3 row-start-5',
  },
}

type EdgeId =
  | 'admins' | 'changes' | 'feed' | 'clients' | 'upstream' | 'run-now' | 'periodic' | 'ingest' | 'work'
  | 'api-postgres' | 'worker-postgres' | 'worker-upstream'

/** An arrow end level with the box's header, for arrows between boxes of one row. */
function header(node: BoxId, side: FlowAnchor['side']): FlowAnchor {
  return { node, side, px: HEADER_MID }
}

/** An arrow end along a side, as a fraction of it (the middle by default). */
function along(node: BoxId, side: FlowAnchor['side'], at?: number): FlowAnchor {
  return { node, side, at }
}

// Each arrow's ends in each layout: in the tall one, every side is the wide one's turned a quarter.
const ANCHORS: Record<TopologyLayout, Record<EdgeId, Pick<FlowEdge, 'from' | 'to'>>> = {
  wide: {
    admins: { from: header('admins', 'right'), to: header('api', 'left') },
    changes: { from: header('api', 'right'), to: header('controller', 'left') },
    feed: { from: header('controller', 'right'), to: header('gateway', 'left') },
    clients: { from: header('clients', 'left'), to: header('gateway', 'right') },
    upstream: { from: along('gateway', 'bottom', 0.85), to: along('upstream', 'top') },
    'run-now': { from: along('api', 'bottom', 0.7), to: header('streams', 'left') },
    periodic: { from: along('controller', 'bottom'), to: along('streams', 'top') },
    ingest: { from: along('gateway', 'bottom', 0.3), to: header('streams', 'right') },
    work: { from: along('streams', 'bottom'), to: along('worker', 'top') },
    'api-postgres': { from: along('api', 'bottom', 0.3), to: along('postgres', 'top', 0.3) },
    'worker-postgres': { from: header('worker', 'left'), to: header('postgres', 'right') },
    'worker-upstream': { from: header('worker', 'right'), to: header('upstream', 'left') },
  },
  tall: {
    admins: { from: along('admins', 'bottom'), to: along('api', 'top') },
    changes: { from: along('api', 'bottom'), to: along('controller', 'top') },
    feed: { from: along('controller', 'bottom'), to: along('gateway', 'top') },
    clients: { from: along('clients', 'top'), to: along('gateway', 'bottom') },
    upstream: { from: along('gateway', 'right', 0.85), to: header('upstream', 'left') },
    'run-now': { from: along('api', 'right', 0.7), to: along('streams', 'top') },
    periodic: { from: header('controller', 'right'), to: header('streams', 'left') },
    ingest: { from: along('gateway', 'right', 0.3), to: along('streams', 'bottom') },
    work: { from: header('streams', 'right'), to: header('worker', 'left') },
    'api-postgres': { from: header('api', 'right'), to: header('postgres', 'left') },
    'worker-postgres': { from: along('worker', 'top'), to: along('postgres', 'bottom') },
    'worker-upstream': { from: along('worker', 'bottom'), to: along('upstream', 'top') },
  },
}

/** An element's width, kept current as it resizes; measured before the first paint. */
function useWidth(): [RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const read = () => setWidth(el.clientWidth)
    read()
    const observer = new ResizeObserver(read)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return [ref, width]
}

function Topology({ data, selectedId, onSelect }: {
  data: SystemTopology
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const [ref, width] = useWidth()
  const layout: TopologyLayout = width >= WIDE_MIN_PX ? 'wide' : 'tall'
  const place = PLACE[layout]
  const rates = useProcessRates(data)
  const since = useQueuedSince(data)
  const processes = allProcesses(data)
  const boxes = Object.fromEntries(
    RUNTIMES.map((runtime) => [runtime, processes.filter((instance) => runtimesOf(instance).includes(runtime))]),
  ) as Record<Runtime, SystemInstance[]>
  const others = processes.filter((instance) => runtimesOf(instance).length === 0)
  const streams = streamRows(data, since)
  const feeds = new Map(data.feed.gateways.map((g) => [g.instance_id, g]))
  const tunnels = boxes.gateway.some((instance) => (instance.snapshot?.gateway?.tunnels.length ?? 0) > 0)
  const box = { rates, feeds, selectedId, onSelect }

  return (
    <div ref={ref} className="overflow-x-auto -mx-1 px-1 py-1">
      <FlowDiagram
        edges={topologyEdges(data, layout, boxes, rates, streams)}
        className={cn('grid min-w-[480px] gap-y-12 items-start', GRID[layout])}
      >
        <ExternalBox id="admins" icon={Users} lines={['Admins', 'API clients']} className={place.admins} />
        <RoleBox runtime="api" icon={Server} instances={boxes.api} className={place.api} {...box} />
        <RoleBox
          runtime="controller"
          icon={Crown}
          instances={boxes.controller}
          status={!data.controller.leader && !data.controller.error && (
            <Badge color="red" className="px-1.5 py-0 text-[10px]" title="No controller holds the lease: none is running, or a failover is under way.">no leader</Badge>
          )}
          className={place.controller}
          {...box}
        />
        <RoleBox
          runtime="gateway"
          icon={Network}
          instances={boxes.gateway}
          footer={gatewayTotals(boxes.gateway, rates)}
          className={place.gateway}
          {...box}
        />
        <ExternalBox
          id="clients"
          icon={MonitorSmartphone}
          lines={tunnels ? ['Proxy clients', 'Tunnel devices'] : ['Proxy clients']}
          className={place.clients}
        />
        <StreamsBox rows={streams} error={data.streams.error} className={place.streams} />
        <TopologyBox id="postgres" icon={Database} title="Postgres" className={place.postgres}>
          <p className="text-[11px] text-fg-subtle px-1.5 py-1">
            Definitions, proxy status and history. The controller reads it; gateways hold no credentials for it.
          </p>
        </TopologyBox>
        <RoleBox
          runtime="worker"
          icon={Cog}
          instances={boxes.worker}
          footer={workerTotals(boxes.worker, rates)}
          className={place.worker}
          {...box}
        />
        <ExternalBox id="upstream" icon={Globe} lines={['Upstream proxies', 'Vendor and cloud APIs']} className={place.upstream} />
      </FlowDiagram>
      {others.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap mt-3 text-[11px] text-fg-subtle">
          Also live:
          {others.map((instance) => (
            <button
              key={instance.instance_id}
              type="button"
              onClick={() => onSelect(instance.instance_id)}
              className="font-mono text-[11.5px] text-primary hover:brightness-110"
              title={`${instance.instance_id} (role ${instance.role})`}
            >
              {shortId(instance.instance_id, 28)}
            </button>
          ))}
        </div>
      )}
      {processes.some((instance) => instance.role === 'all') && (
        <p className="text-[11px] text-fg-subtle mt-3">
          A process of role all runs every runtime, so it shows in each of the four boxes.
        </p>
      )}
    </div>
  )
}

function TopologyBox({ id, icon: Icon, title, count, status, footer, className, children }: {
  id: string
  icon: typeof Server
  title: string
  count?: number
  status?: ReactNode
  footer?: ReactNode
  className?: string
  children: ReactNode
}) {
  return (
    <FlowNode id={id} className={cn('min-w-0 rounded-lg border border-line bg-surface shadow-xs', className)}>
      <div className="flex items-center gap-1.5 h-9 px-2.5 border-b border-line min-w-0">
        <Icon className="w-3.5 h-3.5 text-fg-muted flex-none" />
        <span className="text-[12.5px] font-semibold truncate">{title}</span>
        {count !== undefined && <span className="text-[11px] text-fg-subtle tabular-nums flex-none">{count}</span>}
        <span className="flex-1" />
        {status}
      </div>
      <div className="p-1">{children}</div>
      {footer && <div className="px-2.5 py-1.5 border-t border-line text-[11px] text-fg-subtle tabular-nums">{footer}</div>}
    </FlowNode>
  )
}

/** Where traffic and requests come from or go to, outside Octoprox. */
function ExternalBox({ id, icon: Icon, lines, className }: { id: string; icon: typeof Server; lines: string[]; className?: string }) {
  const [first, ...rest] = lines
  return (
    <FlowNode id={id} className={cn('min-w-0 rounded-lg border border-dashed border-line-strong text-[11px] leading-tight text-fg-muted', className)}>
      <div className="flex items-center gap-1.5 h-9 px-2 min-w-0">
        <Icon className="w-3.5 h-3.5 flex-none" />
        <span className="min-w-0">{first}</span>
      </div>
      {rest.map((line) => <div key={line} className="px-2 pb-2 -mt-1">{line}</div>)}
    </FlowNode>
  )
}

function RoleBox({ runtime, icon, instances, status, footer, className, rates, feeds, selectedId, onSelect }: {
  runtime: Runtime
  icon: typeof Server
  instances: SystemInstance[]
  status?: ReactNode
  footer?: ReactNode
  className?: string
  rates: Record<string, ProcessRates>
  feeds: Map<string, SystemGatewayFeed>
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  return (
    <TopologyBox id={runtime} icon={icon} title={roleLabel(runtime)} count={instances.length} status={status} footer={footer} className={className}>
      {instances.length === 0 ? (
        <p className="text-[11px] text-fg-subtle px-1.5 py-1">None live</p>
      ) : (
        instances.map((instance) => (
          <TopologyProcess
            key={instance.instance_id}
            instance={instance}
            runtime={runtime}
            fact={topologyFact(instance, runtime, rates[instance.instance_id], feeds.get(instance.instance_id))}
            selected={instance.instance_id === selectedId}
            onSelect={onSelect}
          />
        ))
      )}
    </TopologyBox>
  )
}

function TopologyProcess({ instance, runtime, fact, selected, onSelect }: {
  instance: SystemInstance
  runtime: Runtime
  fact: TopologyFact
  selected: boolean
  onSelect: (id: string) => void
}) {
  const { health, title } = processHealth(instance)
  const style = HEALTH_STYLE[health]
  const Icon = style.icon
  const leads = runtime === 'controller' && (instance.snapshot?.controller?.is_leader ?? false)
  return (
    <button
      type="button"
      onClick={() => onSelect(instance.instance_id)}
      aria-pressed={selected}
      title={`${instance.instance_id}: ${title}`}
      className={cn(
        'flex items-start gap-1.5 w-full min-w-0 text-left px-1.5 py-1 rounded-md transition-colors',
        selected ? 'bg-surface-raised' : 'hover:bg-surface-raised',
      )}
    >
      <Icon className={cn('w-3 h-3 mt-[3px] flex-none', style.className)} />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1 min-w-0">
          <ProcessId id={instance.instance_id} />
          {leads && <Crown className="w-3 h-3 text-success flex-none" aria-label="leader" />}
        </span>
        {fact.warning
          ? <span className="block text-[11px] text-warning truncate">{fact.warning}</span>
          : <Parts parts={fact.parts} className="text-[11px] text-fg-subtle tabular-nums" />}
      </span>
    </button>
  )
}

// Characters of an id kept whole when its box is too narrow for all of it:
// what tells two processes apart is its end (a pod's suffix, a replica number).
const ID_TAIL = 8

/** A process id elided in the middle by its box's width rather than by a count, so its tail always shows. */
function ProcessId({ id }: { id: string }) {
  const cut = Math.max(0, id.length - ID_TAIL)
  return (
    <span className="flex min-w-0 font-mono text-[11.5px]">
      <span className="truncate">{id.slice(0, cut)}</span>
      <span className="flex-none">{id.slice(cut)}</span>
    </span>
  )
}

/**
 * Facts that wrap whole onto the next line rather than being cut, with a dot
 * between two on one line: each part leads with its dot, and the row is
 * shifted left by a dot's width inside a clipped box, so the dot of the first
 * part on every line falls outside it.
 */
function Parts({ parts, className }: { parts: string[]; className?: string }) {
  return (
    <span className={cn('block overflow-hidden', className)}>
      <span className="flex flex-wrap -ml-3">
        {parts.map((part, i) => (
          <span key={i} className="whitespace-nowrap before:content-['·'] before:inline-block before:w-3 before:text-center">{part}</span>
        ))}
      </span>
    </span>
  )
}

/** What a process's line says: a few facts, or the one thing wrong with it. */
interface TopologyFact {
  parts: string[]
  warning?: string
}

/** The line under a process in one box: what it is doing as that runtime, or what is wrong with it. */
function topologyFact(
  instance: SystemInstance,
  runtime: Runtime,
  rate: ProcessRates | undefined,
  feed: SystemGatewayFeed | undefined,
): TopologyFact {
  const snapshot = instance.snapshot
  const warn = (warning: string): TopologyFact => ({ parts: [], warning })
  const plain = (...parts: (string | null)[]): TopologyFact => ({ parts: parts.filter((part) => part != null) })
  if (!snapshot) return plain('no readable snapshot')
  if (!snapshot.ready) return warn(snapshot.reasons[0] ?? 'not ready')
  const lag = snapshot.runtime.loop_lag_max_ms
  if (lag != null && lag >= LOOP_LAG_WARN_MS) return warn(`loop ${formatLag(lag)} behind`)
  switch (runtime) {
    case 'api': {
      const api = snapshot.api
      return api ? plain(`pool ${api.pool_checked_out ?? '-'} of ${api.pool_size ?? '-'} in use`) : plain('starting')
    }
    case 'controller': {
      const controller = snapshot.controller
      if (!controller) return plain('starting')
      if (!controller.is_leader) return plain('standing by')
      const failing = controller.compile_error ? 'compile failing' : controller.publish_error ? 'publish failing' : null
      return failing ? warn(failing) : plain('leading', `${compactCount(controller.sections)} ${plural(controller.sections, 'section', 'sections')}`)
    }
    case 'gateway': {
      const gateway = snapshot.gateway
      if (!gateway) return plain('starting')
      if (!gateway.listening) return warn('not listening')
      const failed = gateway.tunnels.find((tunnel) => tunnel.state === 'failed')
      if (failed) return warn(`${TUNNEL_LABEL[failed.protocol] ?? failed.protocol} failed`)
      return plain(
        `${compactCount(gateway.active_connections)} conn`,
        rate ? `${formatRate(rate.requests)} req/s` : null,
        feed && !feed.in_sync ? 'feed behind' : null,
      )
    }
    case 'worker': {
      const worker = snapshot.worker
      if (!worker) return plain('starting')
      const inFlight = jobsInFlight(worker)
      return plain(`${compactCount(inFlight)} in flight`, rate ? `${formatRate(rate.runs)} runs/s` : null)
    }
  }
}

/** The gateways' proxy connections open now, and their request and byte rates; a rate is null until one has it. */
function gatewaySums(gateways: SystemInstance[], rates: Record<string, ProcessRates>) {
  return {
    conns: gateways.reduce((sum, instance) => sum + (instance.snapshot?.gateway?.active_connections ?? 0), 0),
    requests: sumRates(gateways, rates, 'requests'),
    bytes: sumRates(gateways, rates, 'bytes'),
  }
}

function gatewayTotals(gateways: SystemInstance[], rates: Record<string, ProcessRates>): ReactNode {
  if (gateways.length === 0) return null
  const { conns, requests, bytes } = gatewaySums(gateways, rates)
  const parts = [`${compactCount(conns)} conn`]
  if (requests != null) parts.push(`${formatRate(requests)} req/s`)
  if (bytes != null) parts.push(formatByteRate(bytes))
  return <Parts parts={parts} />
}

function workerTotals(workers: SystemInstance[], rates: Record<string, ProcessRates>): ReactNode {
  if (workers.length === 0) return null
  const inFlight = workers.reduce((sum, instance) => sum + (instance.snapshot?.worker ? jobsInFlight(instance.snapshot.worker) : 0), 0)
  const runs = sumRates(workers, rates, 'runs')
  const parts = [`${compactCount(inFlight)} in flight`]
  if (runs != null) parts.push(`${formatRate(runs)} runs/s`)
  return <Parts parts={parts} />
}

interface StreamRow {
  name: string
  stream: SystemWorkStream
  hint: StreamHint | null
}

/** Every work stream with what it says about its workers, the same hints as the work streams table. */
function streamRows(data: SystemTopology, since: Record<string, number>): StreamRow[] {
  const at = readingMs(data)
  const workers = liveWorkers(data)
  return Object.entries(data.streams.by_name).map(([name, stream]) => ({
    name,
    stream,
    hint: streamHint(stream, takersOf(workers, name), since[name] != null ? (at - since[name]) / 1000 : 0),
  }))
}

function StreamsBox({ rows, error, className }: { rows: StreamRow[]; error: string | null; className?: string }) {
  const busy = rows
    .filter((row) => row.stream.length > 0)
    .sort((a, b) => b.stream.queued - a.stream.queued || b.stream.pending - a.stream.pending)
  const shown = busy.slice(0, STREAM_ROWS)
  const queued = rows.reduce((sum, row) => sum + row.stream.queued, 0)
  const pending = rows.reduce((sum, row) => sum + row.stream.pending, 0)
  const noWorker = rows.filter((row) => row.hint?.tone === 'danger').length
  const tooFew = rows.filter((row) => row.hint?.tone === 'warning').length
  return (
    <TopologyBox
      id="streams"
      icon={ListChecks}
      title="Work streams"
      count={rows.length}
      status={noWorker > 0
        ? <Badge color="red" className="px-1.5 py-0 text-[10px] whitespace-nowrap" title={`${noWorker} ${plural(noWorker, 'stream', 'streams')} with no worker`}>no worker</Badge>
        : tooFew > 0 && <Badge color="yellow" className="px-1.5 py-0 text-[10px] whitespace-nowrap" title={`${tooFew} ${plural(tooFew, 'stream', 'streams')} short of workers`}>short</Badge>}
      footer={!error && <Parts parts={[`${compactCount(queued)} queued`, `${compactCount(pending)} pending`]} />}
      className={className}
    >
      {error ? (
        <p className="text-[11px] text-danger px-1.5 py-1 wrap-break-word">{error}</p>
      ) : shown.length === 0 ? (
        <p className="text-[11px] text-fg-subtle px-1.5 py-1">Nothing queued or running</p>
      ) : (
        <div className="px-1.5 py-0.5 text-[11.5px]">
          <div className="flex gap-2 text-[10px] text-fg-subtle">
            <span className="flex-1" />
            <span className="w-10 text-right">queued</span>
            <span className="w-10 text-right">pending</span>
          </div>
          {shown.map(({ name, stream, hint }) => {
            const info = streamInfo(name)
            const tone = hint ? (hint.tone === 'danger' ? 'text-danger' : 'text-warning') : null
            return (
              <div key={name} className="flex items-baseline gap-2 py-0.5 tabular-nums" title={hint ? `${info.label}: ${hint.text}` : info.description || name}>
                <span className={cn('flex-1 min-w-0 truncate', tone)}>{info.label}</span>
                <span className={cn('w-10 text-right', tone ?? 'text-fg-muted')} title={stream.queued.toLocaleString()}>{compactCount(stream.queued)}</span>
                <span className="w-10 text-right text-fg-muted" title={stream.pending.toLocaleString()}>{compactCount(stream.pending)}</span>
              </div>
            )
          })}
          {busy.length > shown.length && <div className="text-[11px] text-fg-subtle pt-0.5">+{busy.length - shown.length} more in the list view</div>}
        </div>
      )}
    </TopologyBox>
  )
}

const LABEL_TONE: Record<FlowTone, string> = {
  quiet: 'text-fg-muted',
  ok: 'text-success',
  warning: 'text-warning',
  danger: 'text-danger',
  active: 'text-primary',
}

/** What an arrow's label and colour say: its tone, whether something moves on it, and the words. */
type EdgeState = Pick<FlowEdge, 'tone' | 'flowing' | 'label' | 'title'>

function edgeState(name: string, value: string, tone: FlowTone, title?: string, flowing = false): EdgeState {
  return {
    tone,
    flowing,
    title,
    label: (
      <>
        <span className="block text-[9.5px] uppercase tracking-wider text-fg-subtle">{name}</span>
        <span className={cn('block font-medium', LABEL_TONE[tone])}>{value}</span>
      </>
    ),
  }
}

/** The process holding the controller lease, while it is live. */
function leaderProcess(data: SystemTopology): SystemInstance | undefined {
  const { leader } = data.controller
  return leader ? allProcesses(data).find((instance) => instance.instance_id === leader) : undefined
}

/** The change stream from the api to the leading controller: whether its compile has caught up with the newest change. */
function changesEdge(data: SystemTopology): EdgeState {
  const { controller } = data
  if (controller.error) return edgeState('changes', 'unreadable', 'danger', controller.error)
  if (!controller.leader) return edgeState('changes', 'no leader', 'danger', 'No controller holds the lease, so nothing compiles the changes.')
  const leader = leaderProcess(data)
  const failing = leader?.snapshot?.controller?.compile_error
  if (failing) return edgeState('changes', 'compile failing', 'danger', failing)
  const compiled = leader ? standing(controller.changes, controller.changes_head, snapshotMs(data, leader)) : null
  if (!compiled) return edgeState('changes', 'not compiled', 'quiet', 'The leader has not reported a compile yet.')
  const { title } = standingText(compiled, 'Compiled')
  if (compiled.late) return edgeState('changes', 'behind', 'warning', title)
  // Behind for the moment after each save: the change is on its way.
  if (compiled.behind) return edgeState('changes', 'compiling', 'active', title, true)
  return edgeState('changes', 'in sync', 'ok', title)
}

/** The routing feed from the controller to the gateways: how many route on the head. */
function feedEdge(data: SystemTopology): EdgeState {
  const { feed } = data
  if (feed.error) return edgeState('feed', 'unreadable', 'danger', feed.error)
  const failing = leaderProcess(data)?.snapshot?.controller?.publish_error
  if (failing) return edgeState('feed', 'publish failing', 'danger', failing)
  if (!feed.digest) return edgeState('feed', 'no feed', 'danger', 'No controller has published a feed yet, or Redis lost it and the leader has not written it again yet.')
  const total = feed.gateways.length
  if (total === 0) return edgeState('feed', 'no gateways', 'quiet')
  const inSync = feed.gateways.filter((g) => g.in_sync).length
  const value = `${inSync}/${total} in sync`
  if (inSync === total) return edgeState('feed', value, 'ok', 'Every gateway routes on the head.')
  // A gateway reads behind after each publish until its next heartbeat reports
  // the apply; behind a head this much older than the reading, it is stuck.
  const compiledAt = parseApiDate(feed.compiled_at)?.getTime()
  const stuck = compiledAt != null && readingMs(data) - compiledAt > BEHIND_WARN_SECONDS * 1000
  return stuck
    ? edgeState('feed', value, 'warning', 'A gateway has stayed behind the head; its snapshot says why.')
    : edgeState('feed', value, 'active', 'A new feed was published: the gateways apply it within a heartbeat.', true)
}

/** The work streams to the workers: the worst hint, else whether anything is running. */
function workEdge(rows: StreamRow[], error: string | null): EdgeState {
  if (error) return edgeState('work', 'unreadable', 'danger', error)
  const queued = rows.reduce((sum, row) => sum + row.stream.queued, 0)
  const pending = rows.reduce((sum, row) => sum + row.stream.pending, 0)
  const value = queued > 0 ? `${compactCount(queued)} queued` : pending > 0 ? `${compactCount(pending)} pending` : 'idle'
  const worst = rows.find((row) => row.hint?.tone === 'danger') ?? rows.find((row) => row.hint?.tone === 'warning')
  if (worst?.hint) return edgeState('work', value, worst.hint.tone, `${streamInfo(worst.name).label}: ${worst.hint.text}`, pending > 0)
  return edgeState('work', value, pending > 0 ? 'active' : 'quiet', 'Entries queued for the workers, and delivered to one and not acknowledged yet', pending > 0)
}

function topologyEdges(
  data: SystemTopology,
  layout: TopologyLayout,
  boxes: Record<Runtime, SystemInstance[]>,
  rates: Record<string, ProcessRates>,
  streams: StreamRow[],
): FlowEdge[] {
  const edge = (id: EdgeId, state: EdgeState): FlowEdge => ({ id, ...ANCHORS[layout][id], ...state })
  const quiet = (title: string): EdgeState => ({ tone: 'quiet', title })
  const { conns, requests, bytes } = gatewaySums(boxes.gateway, rates)
  const serving = (requests ?? 0) > 0 || conns > 0
  const carrying = (bytes ?? 0) > 0
  return [
    edge('admins', quiet('The admin API and the web UI')),
    edge('changes', changesEdge(data)),
    edge('feed', feedEdge(data)),
    edge('clients', edgeState(
      'requests',
      requests != null ? `${formatRate(requests)}/s` : `${compactCount(conns)} conn`,
      serving ? 'active' : 'quiet',
      `${conns.toLocaleString()} proxy connections open now`,
      serving,
    )),
    edge('upstream', edgeState(
      'traffic',
      bytes != null ? formatByteRate(bytes) : '-',
      carrying ? 'active' : 'quiet',
      'Bytes sent and received through the upstreams, all gateways',
      carrying,
    )),
    edge('run-now', quiet('Runs asked for after a save: syncs, scale checks, health checks of new proxies')),
    edge('periodic', {
      tone: data.controller.leader ? 'quiet' : 'danger',
      title: 'The periodic jobs, which the leading controller publishes on its timers',
    }),
    edge('ingest', quiet('Preflight sightings, IP observations, and traffic decisions from metering')),
    edge('work', workEdge(streams, data.streams.error)),
    edge('api-postgres', quiet('The api writes definitions and reads everything it shows')),
    edge('worker-postgres', quiet('Workers write proxy status, metrics and history')),
    edge('worker-upstream', quiet('Health checks, vendor and cloud APIs, IP database downloads')),
  ]
}

// --- inventory -----------------------------------------------------------------------

function Fact({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="min-w-0" title={hint}>
      <div className="text-[11px] text-fg-muted truncate">{label}</div>
      <div className="text-[13px] font-medium tabular-nums truncate">{value}</div>
    </div>
  )
}

function InventoryTiles({ inventory }: { inventory: SystemInventory }) {
  const providersSub = inventory.providers_custom > 0
    ? `${inventory.providers_builtin} built-in · ${inventory.providers_custom} custom`
    : `${inventory.providers_builtin} built-in`
  return (
    <Card className="grid grid-cols-2 @lg:grid-cols-3 @5xl:grid-cols-6 gap-px bg-line overflow-hidden *:bg-surface">
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
            {formatCount(total)} {plural(total, 'proxy', 'proxies')}
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
                className={cn('rounded-xs first:rounded-l-md last:rounded-r-md', STATUS_COLORS[status] ?? 'bg-fg-subtle/40')}
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
        Every proxy of every project, by the status the health jobs last wrote to Postgres. Rate-limit quarantines
        live in Redis and show on each project's Proxies page.
      </p>
    </Card>
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
            <Fact
              label="Server connections"
              value={database.backends ?? '-'}
              hint="Connections to this database from every process and client. Each api process's own pool is on its snapshot."
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
                (table.row_estimate != null ? ` · ~${table.row_estimate.toLocaleString()} rows` : ' · row count not analysed yet') +
                (table.dead_rows > 0 ? ` · ${table.dead_rows.toLocaleString()} dead rows awaiting vacuum` : '') +
                (table.last_vacuum_at ? ` · last vacuum ${relativeTime(table.last_vacuum_at)}` : '')}
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

// --- projects ------------------------------------------------------------------------

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
 * Recorded by the snapshot job, which the controller publishes and a worker
 * runs, rather than by this page, so the series is the same whichever api
 * process answers and keeps going while nobody has the page open.
 */
function TrendsSection() {
  const [range, setRange] = useTimeRange(SYSTEM_HISTORY_RANGES)
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
              ? 'Only one snapshot so far - a trend needs at least two.'
              : `No snapshots in this range yet. The first arrives within ${formatDuration(data?.interval_seconds ?? 300)}.`}
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          <DeltaStrip points={points} range={range} />
          {/* Separate charts, so Redis memory - far smaller than the database - keeps its own scale. */}
          <div className="grid grid-cols-1 @3xl:grid-cols-2 gap-4">
            <TrendChart
              title="Database"
              points={points}
              height={200}
              series={[{ key: 'database_size_bytes', name: 'Database', color: SERIES.db }]}
              formatValue={formatBytes}
              formatTick={bytesShort}
              tickWidth={56}
            />
            <TrendChart
              title="Redis memory"
              points={points}
              height={200}
              series={[{ key: 'redis_memory_bytes', name: 'Redis memory', color: SERIES.redis }]}
              formatValue={formatBytes}
              formatTick={bytesShort}
              tickWidth={56}
            />
          </div>
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
    <div className="grid grid-cols-2 @lg:grid-cols-4 gap-px bg-line rounded-md overflow-hidden *:bg-surface">
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
 * recorded (no controller or no worker running) breaks the line instead
 * of being drawn through as if it were real.
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
        <AreaChart data={points} margin={{ top: 6, right: 6, left: -12, bottom: 0 }} aria-label={`${title} over time`}>
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
            labelFormatter={(v) => new Date(Number(v)).toLocaleString()}
            formatter={(v, name) => [formatValue ? formatValue(Number(v)) : Number(v).toLocaleString(), name]}
            contentStyle={tooltipStyle}
            itemSorter={(item) => series.findIndex((s) => s.key === item.dataKey)}
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
          <span className="inline-block w-2.5 h-0.5 rounded-sm" style={{ background: color }} />
          {label}
        </span>
      ))}
    </div>
  )
}

/**
 * Which tables actually grew over the window. A difference between the two
 * window edges, so it needs no bucketing - and it is the question the database
 * chart above raises as soon as its line slopes up.
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
          <div className="flex-1 min-w-[24px] h-2 rounded-xs bg-surface-raised">
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
      <div className="flex-1 min-w-[24px] h-2 rounded-xs bg-surface-raised">
        <div className="h-full bg-primary rounded-r-[4px]" style={{ width: `${pct}%` }} />
      </div>
      <span className="w-[72px] @lg:w-[80px] flex-none text-right font-medium tabular-nums">{value}</span>
    </div>
  )
}

/*
 * Counts here are unbounded: a loop on a long-lived process reaches millions
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

// A topology box is one column wide, so its counts band ten times sooner.
const COMPACT_FROM = 10_000

function compactCount(value: number): string {
  return Math.abs(value) < COMPACT_FROM ? value.toLocaleString() : bandFormatter.format(value)
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
