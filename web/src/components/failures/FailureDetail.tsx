// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { FailureRecord } from '../../api/client'
import { formatDateTime, formatTime, parseApiDate } from '../../utils/format'
import { Badge, Inspector, InspectorSection, KeyValue } from '../ui'
import { CopyButton, StatusCodeBadge } from '../mitm/RequestDetail'

/** Where a kind of failure comes from, which decides whether Overview counts it. */
export type FailureGroup = 'upstream' | 'refused' | 'intercepted' | 'other'

export const GROUP_INFO: Record<FailureGroup, { label: string; heading: string; color: 'red' | 'yellow' | 'purple' | 'gray'; note: string }> = {
  upstream: {
    label: 'Upstream failure',
    heading: 'Upstream failures',
    color: 'red',
    note: 'A proxy was chosen and the request through it did not get going. These are the failures Overview counts.',
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

interface KindInfo {
  label: string
  group: FailureGroup
  /** One fixed hue per kind, so the chart, the summary and the table agree. */
  color: string
  /** What it means and what to look at. */
  hint: ReactNode
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
    hint: 'No healthy proxy matched the request: the pool is empty or unhealthy, the connectors\' domain filters do not allow this host, or nothing serves the requested location. Check the Proxies page and the connectors\' domain and location settings.',
  },
  all_proxies_quarantined: {
    label: 'All proxies quarantined',
    group: 'refused',
    color: '#0d9488',
    hint: 'Every proxy that could serve the request is resting after hitting its rate limit. The client was told to retry later (429). If this is frequent, the pool is too small for the request rate or the limits are too tight.',
  },
  traffic_limit_exceeded: {
    label: 'Traffic limit reached',
    group: 'refused',
    color: '#2563eb',
    hint: 'The connectors that could serve the request have used their traffic allowance for the period. When this cuts a request that already had a proxy, Overview counts it as a failure. See the traffic limit on the connector.',
  },
  preflight_mismatch: {
    label: 'Wrong exit location',
    group: 'refused',
    color: '#4f46e5',
    hint: 'Exit verification found the proxy exiting somewhere other than the requested location, and the request was refused rather than sent from the wrong place. A message starting "Exit location not checked" means the echo through the proxy failed, so where it exits is unknown. See Exit locations.',
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

/** Time of day for today's entries, date and time for older ones. */
export function formatWhen(value: string): string {
  const d = parseApiDate(value)
  if (!d) return '-'
  const today = new Date()
  return d.toDateString() === today.toDateString() ? formatTime(value) : formatDateTime(value)
}

function ms(value: number): string {
  if (value < 1) return '<1 ms'
  return value >= 10000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`
}

export function FailurePanel({ record, onClose }: { record: FailureRecord; onClose: () => void }) {
  const info = kindInfo(record.kind)
  const tunnel = !!record.peer_id

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
          {record.message && <CopyButton text={record.message} className="" />}
        </div>
        {record.error_type && <p className="text-[11px] text-fg-subtle mt-1">{record.error_type}</p>}
        {record.upstream_message && (
          <div className="mt-2 pt-2 border-t border-line">
            <p className="text-[11px] text-fg-subtle mb-1">The vendor said</p>
            <div className="flex items-start gap-2">
              <p className="font-mono text-xs wrap-break-word leading-relaxed text-fg flex-1 min-w-0">{record.upstream_message}</p>
              <CopyButton text={record.upstream_message} className="" />
            </div>
          </div>
        )}
      </div>

      {info.hint && <p className="text-[12.5px] text-fg-muted leading-relaxed">{info.hint}</p>}
      {GROUP_INFO[info.group].note && <p className="text-[12px] text-fg-subtle leading-relaxed">{GROUP_INFO[info.group].note}</p>}

      <InspectorSection title="Request">
        <div>
          <KeyValue label={tunnel ? 'Device' : 'Method'} value={tunnel ? (record.peer_name || record.peer_id) : (record.method || '-')} />
          <KeyValue label="Target" value={record.target || '-'} mono />
          <KeyValue label="Answered" value={tunnel ? `connection closed (a proxy client gets ${record.status})` : record.status} />
          {record.session_id && <KeyValue label={record.count > 1 ? 'Session (first)' : 'Session'} value={record.session_id} mono />}
          {record.location && <KeyValue label="Location" value={record.location} mono />}
        </div>
      </InspectorSection>

      <InspectorSection title="Upstream">
        <div>
          <KeyValue label="Connector" value={record.connector_name || (record.connector_id ? 'deleted connector' : 'none chosen')} />
          {record.proxy && <KeyValue label="Proxy" value={record.proxy} mono />}
          {record.upstream_status != null && <KeyValue label="Vendor status" value={record.upstream_status} />}
          {record.latency_ms != null && <KeyValue label={record.count > 1 ? 'Time to fail (average)' : 'Time to fail'} value={ms(record.latency_ms)} />}
        </div>
      </InspectorSection>

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
