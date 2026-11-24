// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { FailureRecord } from '../../api/client'
import { useProject } from '../../contexts/ProjectContext'
import { cn } from '../../utils/cn'
import { formatDateTime, formatMs, formatTime, parseApiDate } from '../../utils/format'
import { Badge, CopyButton, Inspector, InspectorSection, KeyValue } from '../ui'
import { StatusCodeBadge } from '../mitm/RequestDetail'
import { LATENCY_STEPS } from '../charts/LatencyChart'
import { GuardedLink } from '../RouteList'

/** Where a kind of failure comes from, which decides whether Overview counts it. */
export type FailureGroup = 'upstream' | 'refused' | 'intercepted' | 'other'

export const GROUP_INFO: Record<FailureGroup, { label: string; heading: string; color: 'red' | 'yellow' | 'purple' | 'gray'; note: string }> = {
  upstream: {
    label: 'Upstream failure',
    heading: 'Upstream failures',
    color: 'red',
    note: 'A proxy was chosen and the request through it did not get going, or its echo found the exit in the wrong place. These are the failures Overview counts.',
  },
  refused: {
    label: 'Refused by Octoprox',
    heading: 'Refused by Octoprox',
    color: 'yellow',
    note: 'Octoprox answered the client itself, before using any proxy. Overview does not count these as requests.',
  },
  intercepted: {
    label: 'Intercepted request',
    heading: 'Inside intercepted connections',
    color: 'purple',
    note: 'One request inside a TLS-intercepted connection. Overview counts the connection, which succeeded.',
  },
  other: { label: 'Other', heading: 'Other', color: 'gray', note: '' },
}

export const GROUP_ORDER: FailureGroup[] = ['upstream', 'refused', 'intercepted', 'other']

/** What a refusal that came after a proxy was chosen is to Overview: a traffic limit cutting a request under way.
 * Refusals before routing never name a proxy. */
const REFUSED_AFTER_A_PROXY_NOTE = 'Octoprox answered the client itself, after a proxy had been chosen for the request, so Overview counts it as a failed request.'

/** The refused group as a whole: mostly answered before any proxy, with the one kind of answer that came after one. */
export const REFUSED_GROUP_NOTE = 'Octoprox answered the client itself, mostly before using any proxy, which Overview does not count as requests. A traffic limit that cut a request under way came after a proxy was used, and Overview counts it as a failed request.'

interface KindInfo {
  label: string
  group: FailureGroup
  /** One fixed hue per kind, so the chart, the summary and the table agree. */
  color: string
  /** What it means and what to look at, which may name a page for this one example. */
  hint: ReactNode | ((record: FailureRecord) => ReactNode)
}

/** Every kind the backend logs (api/core/stats.py FailureKind). */
export const KIND_INFO: Record<string, KindInfo> = {
  upstream_timeout: {
    label: 'Upstream timeout',
    group: 'upstream',
    color: '#dc2626',
    hint: 'The upstream proxy did not answer in time, either to the connection or to the request through it. A vendor under load, an exit that cannot reach the target, or a slow target. If the targets are known to be slow, raise proxy.connection.timeout_seconds.',
  },
  upstream_unreachable: {
    label: 'Proxy unreachable',
    group: 'upstream',
    color: '#db2777',
    hint: 'Octoprox could not open a connection to the upstream proxy: it was refused or the host could not be reached. The proxy may be down or its address wrong; a cloud instance may have been terminated. Check the proxy\'s health on the Proxies page.',
  },
  upstream_rejected: {
    label: 'Refused by the vendor',
    group: 'upstream',
    color: '#ea580c',
    hint: 'The upstream proxy answered with an error instead of connecting. Its status, and the vendor\'s own message when it sends one, say why: 407 means the vendor rejected the connector\'s credentials, 403 that the account may not reach this target or zone, 502 or 503 that the vendor had no exit for the request or could not reach the target.',
  },
  upstream_error: {
    label: 'Upstream error',
    group: 'upstream',
    color: '#be123c',
    hint: 'The connection through the upstream proxy failed some other way, for example it was reset or closed during the handshake. The message is the error as it was raised.',
  },
  client_closed: {
    label: 'Client went away',
    group: 'upstream',
    color: '#78716c',
    hint: 'The client closed the connection, or stopped sending, before it had sent the whole request body. Usually a timeout or a cancelled request on the client side, not a fault in the pool.',
  },
  interrupted: {
    label: 'Interrupted',
    group: 'upstream',
    color: '#a8a29e',
    hint: 'Octoprox stopped, for a restart or a deploy, while the request was being set up.',
  },
  no_proxy_available: {
    label: 'No proxy available',
    group: 'refused',
    color: '#ca8a04',
    hint: 'No healthy proxy matched the request: the pool is empty or unhealthy, the connectors\' domain filters do not allow this host, or nothing serves the requested location. A request that named its proxy with -prxy- says so in the message: that proxy is not one of the project\'s, is not healthy, or does not serve this host or location. Check the Proxies page and the connectors\' domain and location settings.',
  },
  all_proxies_quarantined: {
    label: 'All proxies quarantined',
    group: 'refused',
    color: '#0d9488',
    hint: 'Every proxy that could serve the request is resting after hitting its rate limit, or the one a request named with -prxy- is. The client was told to retry later (429). If this is frequent, the pool is too small for the request rate or the limits are too tight.',
  },
  exits_avoided: {
    label: 'Exits avoided',
    group: 'refused',
    color: '#854d0e',
    hint: (record) => (
      <>
        Exit reputation keeps exit IPs away from a site after bad exit reports: every row that could serve the request has its IP avoided there, or a sticky session's row does while the project does not move refused sessions. Only connectors with IPs avoid IPs; a dynamic connector is weighted instead and never refuses. The message names the site. Avoids end after the project's Avoid for, longer for an IP avoided again: a larger pool leaves more IPs for the site, and a shorter Avoid for in the project's Exit reputation settings gives avoided IPs back sooner. The Exit locations page lists them on its{' '}
        <PageLink to={record.target ? `locations?tab=reputation&site=${encodeURIComponent(hostOf(record.target).toLowerCase())}` : 'locations?tab=reputation'}>Reputation tab</PageLink>.
      </>
    ),
  },
  traffic_limit_exceeded: {
    label: 'Traffic limit reached',
    group: 'refused',
    color: '#2563eb',
    hint: 'The connectors that could serve the request, or the connector of the proxy a request named with -prxy-, have used their traffic allowance for the period. When this cuts a request that already had a proxy, Overview counts it as a failure. See the traffic limit on the connector.',
  },
  preflight_mismatch: {
    label: 'Wrong exit location',
    group: 'upstream',
    color: '#4f46e5',
    hint: (record) => (
      <>
        Exit verification found the proxy exiting somewhere other than the requested location, and the request was refused rather than sent from the wrong place. A message starting "Exit location not checked" means the echo got through the proxy but no answer placed the exit; a vendor failing the echo itself is listed under its own kind, such as Refused by the vendor. See{' '}
        <PageLink to={record.proxy_id ? `locations?tab=observations&proxy_id=${encodeURIComponent(record.proxy_id)}` : 'locations'}>Exit locations</PageLink>.
      </>
    ),
  },
  invalid_location: {
    label: 'Invalid location',
    group: 'refused',
    color: '#65a30d',
    hint: 'The proxy username asked for a state or city without a country. Add -cc-<country> to the client\'s username.',
  },
  intercepted_request_failed: {
    label: 'Intercepted request failed',
    group: 'intercepted',
    color: '#9333ea',
    hint: 'A request inside a TLS-intercepted connection could not be relayed upstream, and the client got a 502 for it. The message is the relay engine\'s error.',
  },
}

export function kindInfo(kind: string): KindInfo {
  return KIND_INFO[kind] ?? { label: kind, group: 'other', color: '#94a3b8', hint: '' }
}

export function KindBadge({ kind }: { kind: string }) {
  const info = kindInfo(kind)
  return <Badge color={GROUP_INFO[info.group].color} className="whitespace-nowrap">{info.label}</Badge>
}

/** The host a failure's target names: host:port, [IPv6]:port, or the URL of an intercepted request. */
function hostOf(target: string): string {
  const authority = target.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').split('/', 1)[0]
  const v6 = /^\[([^\]]+)\]/.exec(authority)
  return v6 ? v6[1] : authority.replace(/:\d+$/, '')
}

/**
 * Another of the project's pages as a real link, so it can be copied or opened in a new tab. A plain click asks first
 * while an editor has unsaved work, as the sidebar does.
 */
function PageLink({ to, className, children }: { to: string; className?: string; children: ReactNode }) {
  const { selectedProjectId } = useProject()
  if (!selectedProjectId) return <>{children}</>
  return <GuardedLink to={`/projects/${selectedProjectId}/${to}`} className={cn('text-primary hover:underline', className)}>{children}</GuardedLink>
}

/** A panel value that opens its own page, 44 px tall on a touch screen. */
function ValueLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <PageLink to={to} className="inline-flex items-center max-w-full pointer-coarse:min-h-11">
      <span className="truncate min-w-0">{children}</span>
    </PageLink>
  )
}

/** Time of day for today's entries, date and time for older ones. */
export function formatWhen(value: string): string {
  const d = parseApiDate(value)
  if (!d) return '-'
  const today = new Date()
  return d.toDateString() === today.toDateString() ? formatTime(value) : formatDateTime(value)
}

export function FailurePanel({ record, onClose }: { record: FailureRecord; onClose: () => void }) {
  const info = kindInfo(record.kind)
  const tunnel = !!record.peer_id
  const note = info.group === 'refused' && record.proxy_id ? REFUSED_AFTER_A_PROXY_NOTE : GROUP_INFO[info.group].note
  const hint = typeof info.hint === 'function' ? info.hint(record) : info.hint
  const connector = record.connector_name || (record.connector_id ? 'deleted connector' : 'none chosen')

  return (
    <Inspector
      title={info.label}
      subtitle={`${formatDateTime(record.last_at)}${record.count > 1 ? ` · ${record.count.toLocaleString()} times` : ''}`}
      onClose={onClose}
      width={520}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <Badge color={GROUP_INFO[info.group].color}>{GROUP_INFO[info.group].label}</Badge>
        <StatusCodeBadge code={record.status} />
        {record.upstream_status != null && <span className="text-xs text-fg-muted">vendor answered {record.upstream_status}</span>}
      </div>

      <div className="rounded-lg border border-line bg-surface-raised/50 px-3 py-2.5">
        <div className="flex items-start gap-2">
          <p className="font-mono text-xs wrap-break-word leading-relaxed text-fg flex-1 min-w-0">{record.message || '-'}</p>
          {record.message && <CopyButton text={record.message} label="Copy message" />}
        </div>
        {record.error_type && <p className="text-[11px] text-fg-subtle mt-1">{record.error_type}</p>}
        {record.upstream_message && (
          <div className="mt-2 pt-2 border-t border-line">
            <p className="text-[11px] text-fg-subtle mb-1">The vendor said</p>
            <div className="flex items-start gap-2">
              <p className="font-mono text-xs wrap-break-word leading-relaxed text-fg flex-1 min-w-0">{record.upstream_message}</p>
              <CopyButton text={record.upstream_message} label="Copy what the vendor said" />
            </div>
          </div>
        )}
      </div>

      {hint && <p className="text-[12.5px] text-fg-muted leading-relaxed">{hint}</p>}
      {note && <p className="text-[12px] text-fg-subtle leading-relaxed">{note}</p>}

      <InspectorSection title="Request">
        <div>
          {tunnel ? (
            <KeyValue label="Device" value={<ValueLink to={`devices?open=${encodeURIComponent(record.peer_id!)}`}>{record.peer_name || record.peer_id}</ValueLink>} />
          ) : (
            <KeyValue label="Method" value={record.method || '-'} />
          )}
          <KeyValue
            label="Target"
            value={record.target ? <ValueLink to={`hosts?search=${encodeURIComponent(hostOf(record.target).toLowerCase())}`}>{record.target}</ValueLink> : '-'}
            mono
          />
          <KeyValue label="Answered" value={tunnel ? `connection closed (a proxy client gets ${record.status})` : record.status} />
          {record.session_id && <KeyValue label={record.count > 1 ? 'Session (first)' : 'Session'} value={record.session_id} mono />}
          {record.location && <KeyValue label="Location" value={record.location} mono />}
        </div>
      </InspectorSection>

      <InspectorSection title="Upstream">
        <div>
          <KeyValue
            label="Connector"
            value={record.connector_id && record.connector_name
              ? <ValueLink to={`connectors?open=${encodeURIComponent(record.connector_id)}`}>{connector}</ValueLink>
              : connector}
          />
          {record.proxy && (
            <KeyValue
              label="Proxy"
              value={record.proxy_id ? <ValueLink to={`proxies?open=${encodeURIComponent(record.proxy_id)}`}>{record.proxy}</ValueLink> : record.proxy}
              mono
            />
          )}
          {record.upstream_status != null && <KeyValue label="Vendor status" value={record.upstream_status} />}
          {record.latency_ms != null && <KeyValue label={record.count > 1 ? 'Time to fail (average)' : 'Time to fail'} value={formatMs(record.latency_ms)} />}
        </div>
      </InspectorSection>

      {record.steps && (
        <InspectorSection title={record.count > 1 ? 'Steps (first failure)' : 'Steps'}>
          <div>
            {LATENCY_STEPS.filter((step) => record.steps?.[step.key] != null).map((step) => (
              <KeyValue
                key={step.key}
                label={<span title={step.hint}>{step.label}{step.key === record.step && <Badge color="red" className="ml-2">failed here</Badge>}</span>}
                value={formatMs(record.steps?.[step.key] ?? 0)}
              />
            ))}
          </div>
        </InspectorSection>
      )}

      <InspectorSection title="When">
        <div>
          {record.count > 1 && <KeyValue label="First" value={formatDateTime(record.first_at)} />}
          <KeyValue label={record.count > 1 ? 'Last' : 'At'} value={formatDateTime(record.last_at)} />
          <KeyValue label="Occurrences" value={record.count.toLocaleString()} />
        </div>
      </InspectorSection>
    </Inspector>
  )
}
