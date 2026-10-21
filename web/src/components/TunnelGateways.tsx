// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { TunnelEndpointPhase, TunnelEndpointState, TunnelStatus } from '../api/client'
import { elideMiddle, plural } from '../utils/format'
import { Alert, Badge, Card, CardHeader } from './ui'

/*
 * A tunnel protocol across the install, for its settings page: every gateway
 * terminating it and the protocol's devices. Tunnels run on gateways, so the
 * page lists them from the heartbeats they publish rather than describing the
 * api process that answered; a gateway that stops leaves the list within
 * about ten seconds.
 */

const PHASE_COLORS: Record<TunnelEndpointPhase, 'green' | 'yellow' | 'red' | 'gray'> = {
  running: 'green', starting: 'yellow', failed: 'red', stopped: 'gray',
}

/** One badge for the protocol: the worst state a gateway is in, and on how many. */
export function TunnelStateBadge({ status }: { status: TunnelStatus }) {
  const total = status.gateways.length
  if (total === 0) return <Badge color="gray">No gateway</Badge>
  const count = (phase: TunnelEndpointPhase) => status.gateways.filter((g) => g.state === phase).length
  const failed = count('failed')
  if (failed > 0) return <Badge color="red">failed on {failed} of {total}</Badge>
  const starting = count('starting')
  if (starting > 0) return <Badge color="yellow">starting on {starting} of {total}</Badge>
  const running = count('running')
  if (running === total) return <Badge color="green">running on {total} {plural(total, 'gateway', 'gateways')}</Badge>
  return <Badge color="gray">running on {running} of {total}</Badge>
}

/**
 * The gateways terminating a protocol, each with its own state and error,
 * then the protocol's devices across the install.
 */
export function TunnelGatewaysCard({ status, notRunning, details, children }: {
  status: TunnelStatus
  /** What to say when no gateway terminates the protocol: how to enable it. */
  notRunning: ReactNode
  /** The protocol's facts about one gateway, shown under its id; nulls are left out. */
  details: (gateway: TunnelEndpointState) => (string | null)[]
  /** Notes under the facts. */
  children?: ReactNode
}) {
  return (
    <Card className="p-5 h-fit">
      <CardHeader title="Gateways" action={<TunnelStateBadge status={status} />} />
      {status.gateways.length === 0 ? (
        <p className="text-sm text-fg-muted mt-2">{notRunning}</p>
      ) : (
        <>
          <p className="text-xs text-fg-muted mt-1">
            Every gateway terminating the tunnel, as its heartbeat reported it a few seconds ago. A device's traffic
            enters the proxy path on the gateway that carries it.
          </p>
          <div className="mt-2">
            {status.gateways.map((gateway) => (
              <div key={gateway.instance_id} className="py-2 border-b border-line last:border-b-0">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-mono text-xs truncate" title={gateway.instance_id}>{elideMiddle(gateway.instance_id, 34)}</span>
                  <Badge color={PHASE_COLORS[gateway.state] ?? 'gray'} className="flex-none">{gateway.state}</Badge>
                </div>
                <div className="text-xs text-fg-muted mt-0.5 tabular-nums">
                  {details(gateway).filter((d): d is string => !!d).join(' · ')}
                </div>
                {gateway.error && (
                  <Alert variant={gateway.state === 'failed' ? 'error' : 'warning'} className="text-xs mt-1.5 px-3 py-2 wrap-break-word whitespace-pre-wrap">
                    {gateway.error}
                  </Alert>
                )}
              </div>
            ))}
          </div>
        </>
      )}
      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <Row label="Devices" value={`${status.peers_enabled} enabled of ${status.peers_total}`} />
        <Row label="Online now" value={status.peers_online} title="Devices a gateway is carrying now, merged across the gateways." />
        <Row label="Routed by address" value={status.connections_by_address} title="Connections of every device, all time, relayed by address because no destination name could be recovered. Each device's own count is on its row." />
        <Row label="Encrypted DNS blocked" value={status.encrypted_dns_blocked} title="Encrypted-DNS connections closed so devices keep the tunnel resolver: every device, all time." />
      </dl>
      {children}
    </Card>
  )
}

function Row({ label, value, title }: { label: string; value: string | number; title?: string }) {
  return (
    <>
      <dt className="text-fg-muted" title={title}>{label}</dt>
      <dd className="text-right tabular-nums" title={title ?? String(value)}>{value}</dd>
    </>
  )
}

/** Connections a gateway's shared tunnel listener relays now, every tunnel protocol together, as a detail line puts it. */
export function relayedLabel(gateway: TunnelEndpointState): string {
  return `${gateway.active_connections} ${plural(gateway.active_connections, 'connection', 'connections')} relayed, all tunnel protocols`
}
