// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ConnectorTrafficShare, TrafficSplitRange, TrafficSplitResponse, fetchProjectTrafficSplit } from '../api/client'
import { ProviderLogo } from './ProviderLogo'
import { Card, CardHeader, InfoTip, Segmented } from './ui'

const RANGES: TrafficSplitRange[] = ['1h', '6h', '24h', '7d', '30d']

// One hue per connector position, cycling. Fixed palette so the bar and the rows agree.
const SWATCHES = ['bg-primary', 'bg-success', 'bg-warning', 'bg-purple-500', 'bg-orange-500', 'bg-sky-500', 'bg-pink-500', 'bg-teal-500']
const swatch = (i: number) => SWATCHES[i % SWATCHES.length]

export const formatShare = (v: number | null | undefined): string => {
  if (v == null) return '-'
  if (v === 0) return '0%'
  if (v < 1) return '<1%'
  return `${Math.round(v)}%`
}

/** Plain-language account of how the strategy divides traffic between connectors. */
export function strategyExplanation(strategy: string): string {
  switch (strategy) {
    case 'round_robin':
      return 'Round robin interleaves connectors in a fixed pattern by weight (weights 3 and 1 give A A B A, three A for every B), then cycles the proxies inside the chosen connector.'
    case 'random':
      return 'Random draws a connector with probability proportional to its weight, then a random proxy inside it.'
    case 'least_used':
      return 'Least used treats weight as capacity: the connector with the fewest requests per unit of weight is picked, then its least-used proxy.'
    case 'sticky':
      return 'Sticky places each new session on a connector by hashing its id, weighted, so the same session always lands on the same connector. Sessions already bound keep their proxy; weight changes only steer new sessions.'
    case 'health_based':
      return 'Health based draws a connector by weight, then prefers the healthiest proxies inside it. Weight is not adjusted for health.'
    default:
      return 'The connector is chosen by weight first, then a proxy inside it by the strategy.'
  }
}

/** One sentence per connector describing its slice of untargeted traffic under the current setup. */
export function describeShare(c: ConnectorTrafficShare, split: TrafficSplitResponse): string {
  if (c.excluded_reason === 'disabled') return `${c.name} is disabled and takes no traffic.`
  if (c.excluded_reason === 'no_eligible_proxies') return `${c.name} has no healthy proxy right now, so its weight does not count and the others share its traffic.`
  const active = split.connectors.filter((x) => !x.excluded_reason)
  if (active.length === 1) return `${c.name} is the only connector with healthy proxies and takes every request.`
  const share = formatShare(c.expected_share)
  const weights = `weight ${c.weight} of ${split.total_weight}`
  if (c.dynamic) return `${c.name} takes ${share} of requests (${weights}); each request opens a fresh vendor session or reuses the client's.`
  const per = c.eligible_proxies > 0 ? c.expected_share / c.eligible_proxies : 0
  return `${c.name} takes ${share} of requests (${weights}), spread across ${c.eligible_proxies} healthy ${c.eligible_proxies === 1 ? 'proxy' : 'proxies'}, about ${formatShare(per)} each.`
}

interface TrafficSplitPanelProps {
  projectId: string
  /** 'card' wraps the panel in a Card with header; 'inline' renders just the body. */
  variant?: 'card' | 'inline'
  onOpenConnector?: (connectorId: string) => void
  className?: string
}

export function TrafficSplitPanel({ projectId, variant = 'card', onOpenConnector, className }: TrafficSplitPanelProps) {
  const [range, setRange] = useState<TrafficSplitRange>('1h')
  const { data: split, isError, error } = useQuery({
    queryKey: ['traffic-split', projectId, range],
    queryFn: () => fetchProjectTrafficSplit(projectId, range),
    enabled: !!projectId,
    refetchInterval: 15000,
  })

  const body = split
    ? <TrafficSplitBody split={split} onOpenConnector={onOpenConnector} />
    : isError
      ? <p className="text-xs text-danger py-3">Could not load the traffic split{error instanceof Error && error.message ? `: ${error.message}` : ''}.</p>
      : <p className="text-xs text-fg-muted py-3">Loading…</p>
  const rangePicker = <Segmented options={RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} size="sm" />

  if (variant === 'inline') {
    return (
      <div className={className}>
        <div className="flex items-center justify-between gap-3 mb-2">
          <span className="text-xs text-fg-muted">Observed over</span>
          {rangePicker}
        </div>
        {body}
      </div>
    )
  }
  return (
    <Card className={`px-4 py-3 ${className ?? ''}`}>
      <CardHeader
        title={
          <span className="inline-flex items-center gap-1.5">
            Traffic split
            <InfoTip label="About traffic split">
              Each connector has a weight on its Routing tab. Among the connectors that can serve a request, one is picked in proportion to weight, then the strategy picks a proxy inside it. Weights are relative: 1 and 3 split 25/75. A connector with no healthy proxy, or one that filters out the target domain or country, is skipped and the rest share its traffic.
            </InfoTip>
          </span>
        }
        action={rangePicker}
        className="mb-2"
      />
      {body}
    </Card>
  )
}

function TrafficSplitBody({ split, onOpenConnector }: { split: TrafficSplitResponse; onOpenConnector?: (id: string) => void }) {
  const active = split.connectors.filter((c) => !c.excluded_reason)
  if (split.connectors.length === 0) return <p className="text-xs text-fg-muted py-3">No connectors yet.</p>

  return (
    <div className="space-y-3">
      {/* Expected split as one stacked bar */}
      <div>
        <div className="flex items-center justify-between text-[11px] text-fg-subtle mb-1">
          <span>Expected, untargeted requests</span>
          <span>{active.length} of {split.connectors.length} connectors eligible</span>
        </div>
        <div className="flex h-2 rounded-full overflow-hidden bg-surface-raised">
          {split.connectors.map((c, i) => c.expected_share > 0 && (
            <span key={c.connector_id} className={`${swatch(i)} h-full`} style={{ flex: c.expected_share }} title={`${c.name}: ${formatShare(c.expected_share)}`} />
          ))}
        </div>
      </div>

      {/* Per connector rows: weight, expected, observed */}
      <div className="-mx-1.5">
        <div className="grid grid-cols-[auto_1fr_auto_auto_auto] gap-x-3 px-1.5 text-[11px] text-fg-subtle">
          <span />
          <span>Connector</span>
          <span className="text-right">Weight</span>
          <span className="text-right">Expected</span>
          <span className="text-right">Observed</span>
        </div>
        {split.connectors.map((c, i) => {
          const off = !!c.excluded_reason
          const Row = onOpenConnector ? 'button' : 'div'
          return (
            <Row
              key={c.connector_id}
              {...(onOpenConnector ? { type: 'button' as const, onClick: () => onOpenConnector(c.connector_id) } : {})}
              className={`w-full grid grid-cols-[auto_1fr_auto_auto_auto] items-center gap-x-3 h-[32px] px-1.5 rounded-md text-[12.5px] text-left ${onOpenConnector ? 'hover:bg-surface-raised transition-colors' : ''}`}
              title={describeShare(c, split)}
            >
              <span className={`w-2 h-2 rounded-full ${off ? 'bg-fg-subtle' : swatch(i)}`} />
              <span className={`inline-flex items-center gap-2 min-w-0 ${off ? 'text-fg-subtle' : ''}`}>
                <ProviderLogo type={c.credential_type} className="w-4 h-4 text-[16px] flex-none" />
                <span className="truncate">{c.name}</span>
                {c.dynamic && <span className="text-fg-subtle text-[11px] flex-none">dynamic</span>}
                {c.excluded_reason === 'disabled' && <span className="text-fg-subtle text-[11px] flex-none">disabled</span>}
                {c.excluded_reason === 'no_eligible_proxies' && <span className="text-warning text-[11px] flex-none">no healthy proxy</span>}
              </span>
              <span className="tabular-nums text-fg-muted text-right">{c.weight}</span>
              <span className={`tabular-nums text-right font-medium ${off ? 'text-fg-subtle' : ''}`}>{formatShare(c.expected_share)}</span>
              <span className="tabular-nums text-right text-fg-muted inline-flex items-center justify-end gap-2 w-[76px]">
                <span className="w-8 h-1 rounded-full bg-primary-soft overflow-hidden inline-block">
                  <span className={`block h-full rounded-full ${swatch(i)}`} style={{ width: `${Math.min(100, c.observed_share ?? 0)}%` }} />
                </span>
                {formatShare(c.observed_share)}
              </span>
            </Row>
          )
        })}
      </div>

      <p className="text-xs text-fg-muted">
        {strategyExplanation(split.strategy)}
        {split.observed_requests === 0 && ' No requests in this window yet.'}
        {split.observed_requests > 0 && ` Observed over ${split.range}: ${split.observed_requests.toLocaleString()} requests. Requests with a country or a filtered domain see a narrower set of connectors, so the observed split can differ from the expected one.`}
      </p>
    </div>
  )
}
