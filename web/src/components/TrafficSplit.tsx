// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import {
  ConnectorTrafficShare, HISTORY_RANGES, TrafficSplitResponse, fetchProjectTrafficSplit,
  type ExitReputationMode, type ReputationPlace,
} from '../api/client'
import { useTimeRange } from '../contexts/TimeRangeContext'
import { useProject } from '../contexts/ProjectContext'
import { compact, formatBytesDecimal, formatMoney, plural } from '../utils/format'
import { cn } from '../utils/cn'
import { ProviderLogo } from './ProviderLogo'
import { ReputationFactor, lowered, placeLabel, reputationReason, reputationTitle, weightText, wouldBe } from './ConnectorReputation'
import { RichSelect } from './RichSelect'
import { Card, CardHeader, InfoTip, LogoLoader, Reloading, Segmented } from './ui'

// One hue per connector position, cycling, so the bar and the rows agree: the chart palette the latency chart
// uses (blue, orange, aqua, yellow, magenta, green, violet, red), stepped for each theme. Neighbours stay apart
// for colour-blind readers too, and none is a theme token (primary, warning) that would repeat a status colour.
const SWATCHES = [
  'bg-[#2a78d6] dark:bg-[#3987e5]', 'bg-[#eb6834] dark:bg-[#d95926]', 'bg-[#1baf7a] dark:bg-[#199e70]', 'bg-[#eda100] dark:bg-[#c98500]',
  'bg-[#e87ba4] dark:bg-[#d55181]', 'bg-[#008300]', 'bg-[#4a3aa7] dark:bg-[#9085e9]', 'bg-[#e34948] dark:bg-[#e66767]',
]
/** The colour classes of the connector at position ``i``, for its dot, its bar segment and its row. */
export const swatch = (i: number) => SWATCHES[i % SWATCHES.length]

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
      return 'Round robin interleaves connectors in a fixed pattern by weight (weights 3 and 1 give A A B A, three A for every B), then cycles the proxies inside the chosen connector. Each gateway keeps its own sequence.'
    case 'random':
      return 'Random draws a connector with probability proportional to its weight, then a random proxy inside it.'
    case 'least_used':
      return 'Least used treats weight as capacity: the connector with the fewest transfers open on the gateway per unit of weight is picked, then its proxy with the fewest open transfers. Each gateway counts its own.'
    case 'sticky':
      return 'Sticky places each new session on a connector by hashing its id, weighted, so the same session always lands on the same connector. Sessions already bound keep their proxy; weight changes only steer new sessions.'
    case 'health_based':
      return 'Health based draws a connector by weight, then prefers the proxies inside it with the best success rate and latency on this gateway, still trying the ones it has seen fewer than 10 outcomes from. Weight is not adjusted for health.'
    default:
      return 'The connector is chosen by weight first, then a proxy inside it by the strategy.'
  }
}

/** Plain-language account of all traffic over the split's range: its requests, bytes and spend, and why it can differ. */
export function allTrafficExplanation(split: TrafficSplitResponse): string {
  const spend = totalSpend(split.connectors)
  return `All traffic over ${split.range}: ${split.observed_requests.toLocaleString()} requests, ${formatBytesDecimal(split.observed_bytes)}${spend ? `, ${formatMoney(spend.amount, spend.currency)} at the current rates` : ''}. It counts every request through each connector, whatever its site or location, so it can differ from the expected split.`
}

/** One sentence per connector describing its slice of untargeted traffic under the current setup. */
export function describeShare(c: ConnectorTrafficShare, split: TrafficSplitResponse): string {
  if (c.excluded_reason === 'disabled') return `${c.name} is disabled and takes no traffic.`
  if (c.excluded_reason === 'traffic_limit') return `${c.name} is over its traffic limit and takes no traffic until its period resets or the limit is raised; the others share its weight.`
  if (c.excluded_reason === 'reserved_only') return `${c.name} serves only requests naming a country it reserves, so it takes none of the requests that name no country; the others share them.`
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
  onOpenConnector?: (connectorId: string) => void
  className?: string
}

export function TrafficSplitPanel({ projectId, onOpenConnector, className }: TrafficSplitPanelProps) {
  const [range, setRange] = useTimeRange(HISTORY_RANGES)
  // The site, and the place its requests name (a LocationTarget key), the effective weights are shown for: none
  // shows the configured split alone.
  const [site, setSite] = useState('')
  const [place, setPlace] = useState('')
  // A picked site's place is settled once its answer lists the site's places: the place asked for stays if listed,
  // else the busiest. Until then the place asked for is the last site's, so a place both sites list is one fetch.
  const [settling, setSettling] = useState(false)
  const { selectedProject } = useProject()
  const mode = selectedProject?.id === projectId ? selectedProject.exit_reputation : undefined
  const { data: split, isPlaceholderData, isError, error } = useQuery({
    // Without a site the key is the one the Connectors page reads its weights from, so the two share one answer.
    queryKey: site ? ['traffic-split', projectId, range, site, place] : ['traffic-split', projectId, range],
    queryFn: () => fetchProjectTrafficSplit(projectId, range, site || undefined, place || undefined),
    enabled: !!projectId,
    refetchInterval: 15000,
    // Another range or site keeps the last split up, dimmed, until its own arrives, so the card keeps its height;
    // another project's split is not shown under this one.
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[1] === projectId ? previous : undefined),
  })

  useEffect(() => {
    if (!settling || !split || split.site !== site || (split.place ?? '') !== place) return
    const places = split.reputation_places ?? []
    if (!place || !places.some((p) => p.place === place)) setPlace(places[0]?.place ?? '')
    setSettling(false)
  }, [settling, split, site, place])

  const pickSite = (value: string) => {
    setSite(value)
    if (!value) setPlace('')
    setSettling(!!value)
  }
  // "No location" picked here stays: only a site switch settles the place again.
  const pickPlace = (value: string) => {
    setPlace(value)
    setSettling(false)
  }
  const picker = split && <SitePicker projectId={projectId} split={split} site={site} place={place} onSite={pickSite} onPlace={pickPlace} />
  const body = split
    ? <Reloading active={isPlaceholderData}><TrafficSplitBody split={split} mode={mode} onOpenConnector={onOpenConnector} /></Reloading>
    : isError
      ? <p className="text-xs text-danger py-3">Could not load the traffic split{error instanceof Error && error.message ? `: ${error.message}` : ''}.</p>
      : <LogoLoader size="sm" className="py-3" />
  const rangePicker = <Segmented options={HISTORY_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} size="sm" />

  return (
    <Card className={`px-4 py-3 ${className ?? ''}`}>
      <CardHeader
        title={
          <span className="inline-flex items-center gap-1.5">
            Traffic split
            <InfoTip label="About traffic split">
              Each connector has a weight on its Routing tab. Among the connectors that can serve a request, one is picked in proportion to weight, then the strategy picks a proxy inside it. Weights are relative: 1 and 3 split 25/75. A connector with no healthy proxy, or one that filters out the target domain or country, is skipped and the rest share its traffic. All traffic counts every request through each connector over the picked range, whatever its site or location, so the site and location pickers do not narrow it.
            </InfoTip>
          </span>
        }
        action={rangePicker}
        className="mb-2"
      />
      {picker}
      {body}
    </Card>
  )
}

/** A place's level by its key: "US" a country, "US/NY" a state, "US/NY/new_york" and "US//austin" a city. */
const placeLevel = (place: string) => {
  const [, state = '', city = ''] = place.split('/')
  return city ? 'City' : state ? 'State' : 'Country'
}

/**
 * Which requests the split is for: a site with exit reputation data (busiest first, or none), and once one is picked
 * the place its requests name, every one searchable. Shown only while the project has such sites, so the card is as
 * it always was without.
 */
function SitePicker({ projectId, split, site, place, onSite, onPlace }: {
  projectId: string
  split: TrafficSplitResponse
  site: string
  place: string
  onSite: (site: string) => void
  onPlace: (place: string) => void
}) {
  const sites = split.reputation_sites ?? []
  if (sites.length === 0 && !site) return null
  // A picked site or place that dropped out of the lists stays picked, and listed.
  const siteOptions = [
    { value: '', label: 'No particular site' },
    ...(!site || sites.some((s) => s.site === site) ? [] : [{ value: site, label: site }]),
    ...sites.map((s) => ({ value: s.site, label: s.site, badge: `${compact(s.reports)} ${plural(s.reports, 'report', 'reports')}` })),
  ]
  // The places are the picked site's once its answer is in; meanwhile the last site's are not offered.
  const places: ReputationPlace[] = split.site === site ? split.reputation_places ?? [] : []
  const placeOptions = [
    { value: '', label: 'No location' },
    ...(!place || places.some((p) => p.place === place) ? [] : [{ value: place, label: placeLabel(place), badge: placeLevel(place) }]),
    ...places.map((p) => ({ value: p.place, label: p.label, badge: `${placeLevel(p.place)} · ${compact(p.reports)}` })),
  ]
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 mb-3 text-xs text-fg-muted">
      {/* A picker that does not fit beside its words goes under them rather than cutting its text. */}
      <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1 min-w-0 max-w-full">
        <span className="flex-none">Requests to</span>
        <RichSelect size="sm" searchable options={siteOptions} value={site} onChange={onSite} aria-label="Requests to site" className="min-w-0 max-w-[14rem]" />
      </span>
      {site && (
        <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1 min-w-0 max-w-full">
          <span className="flex-none">from</span>
          <RichSelect size="sm" searchable options={placeOptions} value={place} onChange={onPlace} aria-label="Requests from location" className="min-w-0 max-w-[14rem]" />
        </span>
      )}
      {site && (
        <Link
          to={`/projects/${projectId}/locations?tab=reputation&site=${encodeURIComponent(site)}`}
          className="ml-auto inline-flex items-center font-medium text-primary hover:brightness-110 whitespace-nowrap pointer-coarse:min-h-11"
        >
          Every place for this site →
        </Link>
      )}
    </div>
  )
}

/** Spend across connectors when they all price in one currency; null when unpriced or mixed. */
export function totalSpend(connectors: { cost: number | null; currency: string | null }[]): { amount: number; currency: string } | null {
  const priced = connectors.filter((c) => c.cost != null && c.currency)
  if (priced.length === 0) return null
  const currency = priced[0].currency as string
  if (priced.some((c) => c.currency !== currency)) return null
  return { amount: priced.reduce((a, c) => a + (c.cost ?? 0), 0), currency }
}

/** One stacked bar of the connectors' shares, each in the colour of its position, so the bar and the rows agree. */
export function ShareBar<T extends { connector_id: string; name: string }>({ connectors, share, className }: {
  connectors: T[]
  share: (c: T) => number
  className?: string
}) {
  return (
    <div className={cn('flex h-2 rounded-full overflow-hidden bg-surface-raised', className)}>
      {connectors.map((c, i) => share(c) > 0 && (
        <span key={c.connector_id} className={`${swatch(i)} h-full`} style={{ flex: share(c) }} title={`${c.name}: ${formatShare(share(c))}`} />
      ))}
    </div>
  )
}

/**
 * A connector's dot colour, logo, name and the tags that explain its row. ``off``: the row takes none of the requests
 * shown, so "no healthy proxy", which says so of requests naming no country, is not set beside a share.
 */
function ConnectorName({ c, off }: { c: ConnectorTrafficShare; off: boolean }) {
  return (
    <span className={`flex items-center gap-2 min-w-0 ${off ? 'text-fg-subtle' : ''}`}>
      <ProviderLogo type={c.credential_type} className="w-4 h-4 text-[16px] flex-none" />
      <span className="truncate">{c.name}</span>
      {c.dynamic && <span className="text-fg-subtle text-[11px] flex-none">dynamic</span>}
      {c.excluded_reason === 'disabled' && <span className="text-fg-subtle text-[11px] flex-none">disabled</span>}
      {c.excluded_reason === 'traffic_limit' && <span className="text-orange-500 text-[11px] flex-none">over traffic limit</span>}
      {c.excluded_reason === 'reserved_only' && <span className="text-primary text-[11px] flex-none">reserved countries only</span>}
      {c.excluded_reason === 'no_eligible_proxies' && off && <span className="text-warning text-[11px] flex-none">no healthy proxy</span>}
    </span>
  )
}

function TrafficSplitBody({ split, mode, onOpenConnector }: { split: TrafficSplitResponse; mode?: ExitReputationMode; onOpenConnector?: (id: string) => void }) {
  if (split.connectors.length === 0) return <p className="text-xs text-fg-muted py-3">No connectors yet.</p>
  if (split.site) return <SiteSplitBody split={split} site={split.site} mode={mode} onOpenConnector={onOpenConnector} />
  const active = split.connectors.filter((c) => !c.excluded_reason)
  const priced = split.connectors.some((c) => c.cost != null)
  // Wide containers lay each connector out as one row of six columns, the band, the header and every row on one
  // grid's columns (subgrid), so each column lines up down the card. Below @md (448px, a phone or a narrow column)
  // the numbers would leave the name too little room, so they stack under it on a second line instead.
  const wideCols = priced ? '@md:grid-cols-[auto_minmax(0,1fr)_auto_auto_auto_auto]' : '@md:grid-cols-[auto_minmax(0,1fr)_auto_auto_auto]'

  return (
    <div className="space-y-3">
      {/* Expected split as one stacked bar */}
      <div>
        <div className="flex items-center justify-between flex-wrap gap-x-3 text-[11px] text-fg-subtle mb-1">
          <span>Expected, untargeted requests</span>
          <span>{active.length} of {split.connectors.length} connectors eligible</span>
        </div>
        <ShareBar connectors={split.connectors} share={(c) => c.expected_share} />
      </div>

      {/* Per connector rows: weight, expected, observed, spend */}
      <div className="@container -mx-1.5">
        <div className={`@md:grid ${wideCols} gap-x-3`}>
          {/* A band over the columns says which requests each group counts: Weight and Expected those naming no
              country, Observed and Spend all traffic, set apart by a divider down the header and every row. Its words
              take no width of their own (contain-inline-size), so neither label widens a column: one a little wider
              than its columns overhangs its rule, centred, rather than being cut short. */}
          <div className="hidden @md:grid @md:grid-cols-subgrid @md:col-span-full px-1.5 text-[11px] text-fg-subtle">
            <span className="col-start-3 col-span-2 contain-inline-size flex justify-center whitespace-nowrap border-b border-line-strong pb-0.5">Untargeted requests</span>
            <span className={cn('contain-inline-size border-l border-line-strong pl-3', priced && 'col-span-2')}>
              <span className="flex justify-center whitespace-nowrap border-b border-line-strong pb-0.5">All traffic · {split.range}</span>
            </span>
          </div>
          <div className="hidden @md:grid @md:grid-cols-subgrid @md:col-span-full px-1.5 text-[11px] text-fg-subtle">
            <span className="w-2" />
            <span>Connector</span>
            <span className="text-right">Weight</span>
            <span className="text-right">Expected</span>
            <span className="text-right border-l border-line-strong pl-3">Observed</span>
            {priced && <span className="text-right">Spend</span>}
          </div>
          {split.connectors.map((c, i) => {
            const off = !!c.excluded_reason
            const Row = onOpenConnector ? 'button' : 'div'
            return (
              <Row
                key={c.connector_id}
                {...(onOpenConnector ? { type: 'button' as const, onClick: () => onOpenConnector(c.connector_id) } : {})}
                className={`w-full grid grid-cols-[auto_minmax(0,1fr)_auto] @md:grid-cols-subgrid @md:col-span-full items-center gap-x-3 gap-y-0.5 py-1.5 @md:py-0 @md:h-[32px] px-1.5 rounded-md text-[12.5px] text-left ${onOpenConnector ? 'hover:bg-surface-raised transition-colors' : ''}`}
                title={describeShare(c, split)}
              >
                <span className={`w-2 h-2 rounded-full ${off ? 'bg-fg-subtle' : swatch(i)}`} />
                <ConnectorName c={c} off={off} />
                {/* Stacked: one line under the name. Wide: `contents` hands the three cells to the row grid. */}
                <span className="col-start-2 col-span-2 row-start-2 flex items-center flex-wrap gap-x-3 gap-y-0.5 min-w-0 @md:contents">
                  <span className="tabular-nums text-fg-muted whitespace-nowrap @md:text-right">
                    <span className="text-fg-subtle @md:hidden">Weight </span>{c.weight}
                  </span>
                  <span className={`tabular-nums font-medium whitespace-nowrap @md:text-right ${off ? 'text-fg-subtle' : ''}`}>
                    <span className="text-fg-subtle font-normal @md:hidden">Expected </span>{formatShare(c.expected_share)}
                  </span>
                  <span className="tabular-nums text-fg-muted inline-flex items-center gap-2 whitespace-nowrap @md:self-stretch @md:justify-end @md:min-w-[88px] @md:border-l @md:border-line-strong @md:pl-3" title={`${formatBytesDecimal(c.observed_bytes)} over ${split.range}, every site and location`}>
                    <span className="text-fg-subtle @md:hidden">All traffic · {split.range}</span>
                    <span className="w-8 h-1 rounded-full bg-primary-soft overflow-hidden inline-block flex-none">
                      <span className={`block h-full rounded-full ${swatch(i)}`} style={{ width: `${Math.min(100, c.observed_share ?? 0)}%` }} />
                    </span>
                    {formatShare(c.observed_share)}
                  </span>
                </span>
                {priced && (
                  <span className="col-start-3 row-start-1 @md:col-auto @md:row-auto tabular-nums text-right text-fg-muted whitespace-nowrap @md:min-w-[64px]">
                    {c.cost != null && c.currency ? formatMoney(c.cost, c.currency) : '-'}
                  </span>
                )}
              </Row>
            )
          })}
        </div>
      </div>

      <p className="text-xs text-fg-muted">
        {strategyExplanation(split.strategy)}
        {split.observed_requests === 0 && ' No requests in this window yet.'}
        {split.observed_requests > 0 && ` ${allTrafficExplanation(split)}`}
      </p>
    </div>
  )
}

const MODE_NAMES: Record<ExitReputationMode, string> = { off: 'Off', record: 'Record', avoid: 'Avoid' }

/**
 * The split for requests to one site (from one place): each connector's configured weight, its reputation there, the
 * weight that makes and the share of the site's requests it gets, the configured split faint above that one. While the
 * project does not avoid, the numbers are what Avoid would do, and say so.
 */
function SiteSplitBody({ split, site, mode, onOpenConnector }: {
  split: TrafficSplitResponse
  site: string
  mode?: ExitReputationMode
  onOpenConnector?: (id: string) => void
}) {
  const place = split.place ?? ''
  const places = split.reputation_places ?? []
  const from = place ? ` from ${placeLabel(place, places)}` : ''
  // The band over the columns the pickers decide, naming the requests they are for.
  const picked = place ? `${site}${from}` : `${site}, no location`
  const applied = split.reputation_applied
  // Whether a connector serves the site's requests from the place, by the answer for that request: a connector with IPs
  // has rows eligible for it (every one avoided keeps its ×0 row), a dynamic one an effective weight; one its domain
  // filter, a reservation or the place keeps off has neither. ``excluded_reason`` is the reason for requests naming no
  // site and no place, so a connector reserving the place's country takes them although it has one.
  const takes = (c: ConnectorTrafficShare) =>
    c.reputation_kind === 'usable_ips' ? (c.eligible_ips ?? 0) > 0 : c.effective_weight > 0
  // Where the project does not avoid the API answers the configured weights, so what Avoid would make is worked out
  // here, by the same rule: weight times reputation among the connectors that take the requests.
  const would = wouldBe(split.connectors, takes)
  const effective = new Map(split.connectors.map((c, i) => [c.connector_id, applied ? { weight: c.effective_weight, share: c.effective_share } : would[i]]))
  const active = split.connectors.filter(takes)
  const effectiveTotal = active.reduce((sum, c) => sum + (effective.get(c.connector_id)?.weight ?? 0), 0)
  // The reason line names the connector reputation lowers most among those that take requests.
  const lowest = active.filter((c) => lowered(c.reputation)).sort((a, b) => a.reputation - b.reputation)[0]
  // Round robin draws a request whose weights reputation lowered at random by them, out of its fixed order; a connector
  // with every row avoided only drops out, which lowers no weight.
  const drawn = split.strategy === 'round_robin' && applied && active.some((c) => c.reputation > 0 && c.reputation < 1)
  // Six columns need more room than the configured split's: below @lg (512px) the numbers stack under the name. Wide,
  // the band, the header and every row share one grid's columns (subgrid), as in the configured split.
  const wideCols = '@lg:grid-cols-[auto_minmax(0,1fr)_auto_auto_auto_auto_auto]'

  return (
    <div className="space-y-3">
      {!applied && (
        <p className="text-xs rounded-md border border-warning/30 bg-warning-soft text-warning px-2.5 py-1.5">
          Not applied: {mode && mode !== 'avoid' ? `the project's exit reputation mode is ${MODE_NAMES[mode]}, not Avoid` : 'the project\'s exit reputation does not avoid'}, so routing keeps the configured weights. The numbers below are what Avoid would do.
        </p>
      )}

      {/* The configured split, faint, above the split for the site. */}
      <div className="space-y-1">
        <div className="flex items-center justify-between flex-wrap gap-x-3 text-[11px] text-fg-subtle">
          <span>Configured</span>
          <span>{applied ? 'For' : 'If avoided, for'} {site}{from}</span>
        </div>
        <ShareBar connectors={split.connectors} share={(c) => c.expected_share} className="h-1 opacity-50" />
        <ShareBar connectors={split.connectors} share={(c) => effective.get(c.connector_id)?.share ?? 0} className={applied ? undefined : 'opacity-60'} />
      </div>

      <div className="@container -mx-1.5">
        <div className={`@lg:grid ${wideCols} gap-x-3`}>
          {/* The band: Weight to Expected follow the pickers, Observed counts all traffic. The pickers' words take
              no width of their own (contain-inline-size), so a long site or place is cut short, not a wider column;
              nor does the range's, which overhangs its rule a little, centred, when wider than Observed. */}
          <div className="hidden @lg:grid @lg:grid-cols-subgrid @lg:col-span-full px-1.5 text-[11px] text-fg-subtle">
            <span className="col-start-3 col-span-4 contain-inline-size truncate text-center border-b border-line-strong pb-0.5" title={picked}>{picked}</span>
            <span className="contain-inline-size border-l border-line-strong pl-3">
              <span className="flex justify-center whitespace-nowrap border-b border-line-strong pb-0.5">All traffic · {split.range}</span>
            </span>
          </div>
          <div className="hidden @lg:grid @lg:grid-cols-subgrid @lg:col-span-full px-1.5 text-[11px] text-fg-subtle">
            <span className="w-2" />
            <span>Connector</span>
            <span className="text-right">Weight</span>
            <span className="text-right">Reputation</span>
            <span className="text-right">Effective</span>
            <span className="text-right">Expected</span>
            <span className="text-right border-l border-line-strong pl-3">Observed</span>
          </div>
          {split.connectors.map((c, i) => {
            const off = !takes(c)
            const { weight: effectiveWeight, share: effectiveShare } = effective.get(c.connector_id) ?? { weight: 0, share: 0 }
            const Row = onOpenConnector ? 'button' : 'div'
            // A connector off every request says why; one kept off this site's requests from the place, what can.
            const offTitle = c.excluded_reason === 'disabled' || c.excluded_reason === 'traffic_limit'
              ? describeShare(c, split)
              : `${c.name} takes none of the requests to ${site}${from}: no healthy row of it serves them, or its domain filter or a country another connector reserves keeps it off.`
            return (
              <Row
                key={c.connector_id}
                {...(onOpenConnector ? { type: 'button' as const, onClick: () => onOpenConnector(c.connector_id) } : {})}
                className={`w-full grid grid-cols-[auto_minmax(0,1fr)] @lg:grid-cols-subgrid @lg:col-span-full items-center gap-x-3 gap-y-0.5 py-1.5 @lg:py-0 @lg:h-[32px] px-1.5 rounded-md text-[12.5px] text-left ${onOpenConnector ? 'hover:bg-surface-raised transition-colors' : ''}`}
                title={off ? offTitle : reputationTitle(c.name, c, site, place, places)}
              >
                <span className={`w-2 h-2 rounded-full ${off ? 'bg-fg-subtle' : swatch(i)}`} />
                <ConnectorName c={c} off={off} />
                {/* Stacked: under the name, the weight as a sum ("Weight 3 ×0.65 = 1.95"), then the shares. Wide:
                    `contents` hands the five cells to the row grid. A connector that takes none of the requests has
                    no sum: a dash and 0 on a wide card, its weight alone stacked. */}
                <span className="col-start-2 row-start-2 flex items-center flex-wrap gap-x-3 gap-y-0.5 min-w-0 @lg:contents">
                  <span className="inline-flex items-center gap-x-1.5 whitespace-nowrap @lg:contents">
                    <span className="tabular-nums text-fg-muted whitespace-nowrap @lg:text-right">
                      <span className="text-fg-subtle @lg:hidden">Weight </span>{c.weight}
                    </span>
                    <span className={cn('items-center whitespace-nowrap @lg:inline-flex @lg:justify-end', off ? 'hidden' : 'inline-flex')}>
                      {off ? <span className="text-fg-subtle">-</span> : <ReputationFactor facts={c} places={places} />}
                    </span>
                    <span className={cn('tabular-nums whitespace-nowrap @lg:block @lg:text-right', off && 'hidden', lowered(c.reputation) && !off ? 'font-medium' : 'text-fg-muted')}>
                      <span className="text-fg-subtle font-normal @lg:hidden">= </span>{weightText(effectiveWeight)}
                    </span>
                  </span>
                  <span className={cn('tabular-nums whitespace-nowrap @lg:text-right', off ? 'text-fg-subtle' : applied ? 'font-medium' : 'text-fg-muted')}>
                    <span className="text-fg-subtle font-normal @lg:hidden">Expected </span>{formatShare(effectiveShare)}
                  </span>
                  <span className="tabular-nums text-fg-muted inline-flex items-center gap-2 whitespace-nowrap @lg:self-stretch @lg:justify-end @lg:min-w-[88px] @lg:border-l @lg:border-line-strong @lg:pl-3" title={`${formatBytesDecimal(c.observed_bytes)} over ${split.range}, every site and location`}>
                    <span className="text-fg-subtle @lg:hidden">All traffic · {split.range}</span>
                    <span className="w-8 h-1 rounded-full bg-primary-soft overflow-hidden inline-block flex-none">
                      <span className={`block h-full rounded-full ${swatch(i)}`} style={{ width: `${Math.min(100, c.observed_share ?? 0)}%` }} />
                    </span>
                    {formatShare(c.observed_share)}
                  </span>
                </span>
              </Row>
            )
          })}
        </div>
      </div>

      <p className="text-xs text-fg-muted">
        {lowest
          ? `${lowest.name}: ${reputationReason(lowest, site, place, places)}. Effective weights total ${weightText(effectiveTotal)} of ${split.total_weight}.`
          : `Nothing lowers a connector's weight for ${site}${from}: every connector keeps its weight.`}
      </p>
      <p className="text-xs text-fg-muted">
        {strategyExplanation(split.strategy)}
        {drawn && ' Requests to this site are drawn at random by the effective weights, since reputation lowers a weight here; the fixed order holds for the other requests.'}
        {split.observed_requests === 0 && ' No requests in this window yet.'}
        {split.observed_requests > 0 && ` ${allTrafficExplanation(split)}`}
      </p>
    </div>
  )
}
