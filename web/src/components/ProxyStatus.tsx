// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Proxy } from '../api/client'
import { Badge } from './ui'

type BadgeColor = 'green' | 'blue' | 'yellow' | 'red' | 'gray' | 'orange' | 'purple' | 'slate'

const STATUS_COLORS: Record<string, BadgeColor> = {
  healthy: 'green',
  initializing: 'blue',
  degraded: 'yellow',
  unhealthy: 'red',
  unknown: 'gray',
  draining: 'orange',
  terminating: 'purple',
  quarantined: 'orange',
  disabled: 'slate',
}

// Dot colours are literal so they work on every theme (status colours are reserved).
const DOT_CLASSES: Record<BadgeColor, string> = {
  green: 'bg-success',
  blue: 'bg-primary',
  yellow: 'bg-warning',
  red: 'bg-danger',
  gray: 'bg-fg-subtle',
  orange: 'bg-orange-500',
  purple: 'bg-purple-500',
  slate: 'bg-slate-400',
}

export function formatQuarantineRemaining(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

/** Effective display status, folding quarantine and disabled connectors in. */
export function displayStatus(p: Pick<Proxy, 'status' | 'quarantined' | 'connector_enabled'>): string {
  if (p.quarantined) return 'quarantined'
  if (!p.connector_enabled) return 'disabled'
  return p.status
}

export function statusColor(status: string): BadgeColor {
  return STATUS_COLORS[status] ?? 'gray'
}

/** Dot + label, for dense table rows. */
export function ProxyStatusDot({ proxy }: { proxy: Pick<Proxy, 'status' | 'quarantined' | 'connector_enabled' | 'quarantine_remaining_seconds'> }) {
  const status = displayStatus(proxy)
  return (
    <span className="inline-flex items-center gap-1.5 text-[13px] whitespace-nowrap">
      <span className={`w-2 h-2 rounded-full flex-none ${DOT_CLASSES[statusColor(status)]}`} />
      {status}
      {proxy.quarantined && (
        <span className="text-xs text-fg-subtle tabular-nums">{formatQuarantineRemaining(proxy.quarantine_remaining_seconds)}</span>
      )}
    </span>
  )
}

/** Pill badge, for panel headers. */
export function ProxyStatusBadge({ proxy }: { proxy: Pick<Proxy, 'status' | 'quarantined' | 'connector_enabled'> }) {
  const status = displayStatus(proxy)
  return <Badge color={statusColor(status)}>{status}</Badge>
}
