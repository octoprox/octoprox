// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { FailureRecord } from '../../api/client'
import { formatDateTime, formatTime, parseApiDate } from '../../utils/format'
import { Badge, Inspector, InspectorSection, KeyValue } from '../ui'
import { CopyButton, StatusCodeBadge } from '../mitm/RequestDetail'

/** Where a kind of failure comes from, which decides whether Overview counts it. */
export type FailureGroup = 'upstream' | 'refused' | 'intercepted' | 'other'

export const GROUP_INFO: Record<FailureGroup, { label: string; color: 'red' | 'yellow' | 'purple' | 'gray'; dot: string; note: string }> = {
  upstream: {
    label: 'Upstream failure',
    color: 'red',
    dot: '#dc2626',
    note: 'A proxy was chosen and the request through it did not get going. These are the failures Overview counts.',
  },
  refused: {
    label: 'Refused by Octoprox',
    color: 'yellow',
    dot: '#ca8a04',
    note: 'Octoprox answered the client itself, before using any proxy. Overview does not count these as requests.',
  },
  intercepted: {
    label: 'Intercepted request',
    color: 'purple',
    dot: '#9333ea',
    note: 'One request inside a TLS-intercepted connection. Overview counts the connection, which succeeded.',
  },
  other: { label: 'Other', color: 'gray', dot: '#94a3b8', note: '' },
}

interface KindInfo {
  label: string
  group: FailureGroup
  /** What it means and what to look at. */
  hint: ReactNode
}

/** Every kind the backend logs (api/core/failure_log.py FailureKind), plus the window tally. */
export const KIND_INFO: Record<string, KindInfo> = {
  upstream_timeout: {
    label: 'Upstream timeout',
    group: 'upstream',
    hint: 'The upstream proxy did not answer in time, either to the connection or to the request through it. A vendor under load, an exit that cannot reach the target, or a slow target. If the targets are known to be slow, raise proxy.connection.timeout_seconds.',
  },
  upstream_unreachable: {
    label: 'Proxy unreachable',
    group: 'upstream',
    hint: 'Octoprox could not open a connection to the upstream proxy: it was refused or the host could not be reached. The proxy may be down or its address wrong; a cloud instance may have been terminated. Check the proxy\'s health on the Proxies page.',
  },
  upstream_rejected: {
    label: 'Refused by the vendor',
    group: 'upstream',
    hint: 'The upstream proxy answered with an error instead of connecting. Its status says why: 407 means the vendor rejected the connector\'s credentials, 403 that the account may not reach this target or zone, 502 or 503 that the vendor had no exit for the request or could not reach the target.',
  },
  upstream_error: {
    label: 'Upstream error',
    group: 'upstream',
    hint: 'The connection through the upstream proxy failed some other way, for example it was reset or closed during the handshake. The message is the error as it was raised.',
  },
  client_closed: {
    label: 'Client went away',
    group: 'upstream',
    hint: 'The client closed the connection, or stopped sending, before it had sent the whole request body. Usually a timeout or a cancelled request on the client side, not a fault in the pool.',
  },
  interrupted: {
    label: 'Interrupted',
    group: 'upstream',
    hint: 'Octoprox stopped, for a restart or a deploy, while the request was being set up.',
  },
  no_proxy_available: {
    label: 'No proxy available',
    group: 'refused',
    hint: 'No healthy proxy matched the request: the pool is empty or unhealthy, the connectors\' domain filters do not allow this host, or nothing serves the requested location. Check the Proxies page and the connectors\' domain and location settings.',
  },
  all_proxies_quarantined: {
    label: 'All proxies quarantined',
    group: 'refused',
    hint: 'Every proxy that could serve the request is resting after hitting its rate limit. The client was told to retry later (429). If this is frequent, the pool is too small for the request rate or the limits are too tight.',
  },
  traffic_limit_exceeded: {
    label: 'Traffic limit reached',
    group: 'refused',
    hint: 'The connectors that could serve the request have used their traffic allowance for the period. When this cuts a request that already had a proxy, Overview counts it as a failure. See the traffic limit on the connector.',
  },
  preflight_mismatch: {
    label: 'Wrong exit location',
    group: 'refused',
    hint: 'Exit verification found the proxy exiting somewhere other than the requested location, and the request was refused rather than sent from the wrong place. See Exit locations.',
  },
  invalid_location: {
    label: 'Invalid location',
    group: 'refused',
    hint: 'The proxy username asked for a state or city without a country. Add -cc-<country> to the client\'s username.',
  },
  intercepted_request_failed: {
    label: 'Intercepted request failed',
    group: 'intercepted',
    hint: 'A request inside a TLS-intercepted connection could not be relayed upstream, and the client got a 502 for it. The message is the relay engine\'s error.',
  },
  suppressed: {
    label: 'Not itemised',
    group: 'other',
    hint: 'More distinct failures arrived in a few seconds than the log itemises per project, so the rest were only counted by kind. The breakdown below says which kinds.',
  },
}

export function kindInfo(kind: string): KindInfo {
  return KIND_INFO[kind] ?? { label: kind, group: 'other', hint: '' }
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
  const breakdown = record.breakdown ? Object.entries(record.breakdown).sort((a, b) => b[1] - a[1]) : []

  return (
    <Inspector
      title={info.label}
      subtitle={`${formatDateTime(record.last_at)}${record.count > 1 ? ` · ${record.count.toLocaleString()} times` : ''}`}
      onClose={onClose}
      width={520}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <Badge color={GROUP_INFO[info.group].color}>{GROUP_INFO[info.group].label}</Badge>
        {record.kind !== 'suppressed' && <StatusCodeBadge code={record.status} />}
        {record.upstream_status != null && <span className="text-xs text-fg-muted">vendor answered {record.upstream_status}</span>}
      </div>

      <div className="rounded-lg border border-line bg-surface-raised/50 px-3 py-2.5">
        <div className="flex items-start gap-2">
          <p className="font-mono text-xs break-words leading-relaxed text-fg flex-1 min-w-0">{record.message || '-'}</p>
          {record.message && <CopyButton text={record.message} className="" />}
        </div>
        {record.error_type && <p className="text-[11px] text-fg-subtle mt-1">{record.error_type}</p>}
      </div>

      {info.hint && <p className="text-[12.5px] text-fg-muted leading-relaxed">{info.hint}</p>}
      {GROUP_INFO[info.group].note && <p className="text-[12px] text-fg-subtle leading-relaxed">{GROUP_INFO[info.group].note}</p>}

      {breakdown.length > 0 && (
        <InspectorSection title="Not itemised, by kind">
          <div>
            {breakdown.map(([kind, n]) => (
              <KeyValue key={kind} label={<KindBadge kind={kind} />} value={n.toLocaleString()} />
            ))}
          </div>
        </InspectorSection>
      )}

      {record.kind !== 'suppressed' && (
        <>
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
        </>
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
