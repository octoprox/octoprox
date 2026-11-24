// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * One site across every place its records name, on the Reputation tab: each connector's reputation and share of the
 * requests from each place, a tile per connector, and the place inspector a row opens.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { queryOptions, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'
import {
  fetchSitePlaces,
  type PlaceCell, type SiteConnector, type SitePlace, type SitePlaceLevel, type SitePlacesView,
} from '../../api/client'
import { ProviderLogo } from '../ProviderLogo'
import { RichSelect } from '../RichSelect'
import { ShareBar, formatShare, swatch } from '../TrafficSplit'
import { HATCHED, STATE_LOOKS, factorText, factorTint, factorTone, levelName, placeLevels, strengthNote, type StrengthRule } from '../ConnectorReputation'
import { Card, CardHeader, InfoTip, Inspector, InspectorSection, LogoLoader, Reloading } from '../ui'
import { LoadError, loadFailed } from '../ui/LoadError'
import { compact, plural, relativeTime } from '../../utils/format'
import { cn } from '../../utils/cn'

/** One site's places ('' for the default site), shared by the card and the place inspector so both read one answer. */
export const placesQuery = (projectId: string, site: string) => queryOptions({
  queryKey: ['exit-reputation-places', projectId, site],
  queryFn: () => fetchSitePlaces(projectId, site || undefined),
  // Reports arrive and factors are decided while the page is open.
  refetchInterval: 60_000,
})

/** Which places the table shows: every one, or only the lowered or the not judged ones. */
export const PLACE_PRESETS = ['all', 'lowered', 'not_judged'] as const
export type Preset = (typeof PLACE_PRESETS)[number]

const PRESETS: { value: Preset; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'lowered', label: 'Lowered' },
  { value: 'not_judged', label: 'Not judged' },
]

/** Children an open place shows before "Show N more". */
const FIRST_CHILDREN = 5
/** The Jump picker's value for no place named, whose key ("") would read as nothing picked. */
const NO_PLACE_OPTION = '-'

const LEVEL_WORDS: Record<SitePlaceLevel, string> = { none: 'No place', country: 'Country', state: 'State', city: 'City' }

// Table cells; the place column stays in view while the connectors scroll sideways under it, with a background of its
// own so they do not show through.
const CELL = 'px-1.5 py-1 border-b border-line whitespace-nowrap'
const FIRST = 'sticky left-0 z-[1] pl-4 pr-2 text-left border-r border-r-line min-w-[10rem] max-w-[12rem] @2xl:min-w-[13rem] @2xl:max-w-[20rem]'
// Columns a phone drops: the split bar goes under the place's name instead.
const WIDE = 'hidden @2xl:table-cell'
// A place's indent by its depth: a state or a city under its country, a city under its state. Every row keeps the
// chevron's room (a spacer where it has none), so each step is the indent alone, on touch as with a mouse.
const INDENT = ['', 'pl-3.5 pointer-coarse:pl-5', 'pl-7 pointer-coarse:pl-10']

const matches = (row: SitePlace, preset: Preset) =>
  preset === 'all' || (preset === 'lowered' ? row.lowered : row.not_judged)

/** Whether a connector takes any of the place's requests. */
const takes = (cell: PlaceCell | undefined): cell is PlaceCell => !!cell && cell.state !== 'takes_none'

/** "+5 pts", "-26 pts", "0 pts": how far a share moved. */
const points = (from: number, to: number) => {
  const delta = Math.round(to - from)
  return `${delta > 0 ? '+' : ''}${delta} pts`
}

/**
 * The places as a tree, siblings in a steady order: the first answer's for the site, places that appear later after
 * those already shown, so the 60 s poll does not move rows under a finger.
 */
class PlaceTree {
  readonly roots: SitePlace[]
  readonly children = new Map<string, SitePlace[]>()
  readonly byPlace: Map<string, SitePlace>

  constructor(places: SitePlace[], rank: Map<string, number>) {
    let next = rank.size
    for (const row of places) if (!rank.has(row.place)) rank.set(row.place, next++)
    this.byPlace = new Map(places.map((row) => [row.place, row]))
    const ordered = [...places].sort((a, b) => rank.get(a.place)! - rank.get(b.place)!)
    this.roots = ordered.filter((row) => row.parent == null || !this.byPlace.has(row.parent))
    for (const row of ordered) {
      if (row.parent == null || !this.byPlace.has(row.parent)) continue
      const siblings = this.children.get(row.parent)
      if (siblings) siblings.push(row)
      else this.children.set(row.parent, [row])
    }
  }

  /** The place's parents, nearest first. */
  ancestors(place: string): string[] {
    const found: string[] = []
    for (let parent = this.byPlace.get(place)?.parent; parent != null && this.byPlace.has(parent); parent = this.byPlace.get(parent)?.parent) {
      found.push(parent)
    }
    return found
  }

  /** Whether the place or a place under it matches the preset. */
  shows(row: SitePlace, preset: Preset): boolean {
    return matches(row, preset) || (this.children.get(row.place) ?? []).some((child) => this.shows(child, preset))
  }

  /** The place's children the preset leaves. */
  kids(place: string, preset: Preset): SitePlace[] {
    return (this.children.get(place) ?? []).filter((child) => this.shows(child, preset))
  }
}

/** A line of the table: a place, or the button that shows the rest of a place's children. */
type Line =
  | { kind: 'place'; row: SitePlace; depth: number; kids: number; open: boolean; dim: boolean }
  | { kind: 'more'; parent: string; depth: number; hidden: SitePlace[] }

/** The project's Strength and Lowest factor the view was worked out with, naming connectors as the view does. */
const ruleOf = (data: SitePlacesView): StrengthRule => ({
  strength: data.reputation_strength,
  floor: data.reputation_floor,
  nameOf: (id) => data.connectors.find((c) => c.connector_id === id)?.name,
})

/** The counts of a connector at ``row``'s place, when its factor came from the place itself: they are its own there. */
const ownCounts = (row: SitePlace) => (id: string) => {
  const cell = row.cells[id]
  return cell && cell.reputation_place === row.place ? cell : undefined
}

/** A place as a sentence names it: its label, or "no place" for requests that name none. */
const placeWords = (row: SitePlace) => (row.place ? row.label : 'requests naming no place')

/**
 * A dynamic connector's counts as routing last decided its factor: "5 of 20, counted 3 h ago", or with ``words`` "5 of
 * 20 requests reported blocked, counted 3 h ago".
 */
function countsText(cell: PlaceCell, words = false): string | null {
  if (cell.blocked == null || cell.requests == null) return null
  const counted = cell.counted_at ? `, counted ${relativeTime(cell.counted_at)}` : ''
  return `${cell.blocked.toLocaleString()} of ${cell.requests.toLocaleString()}${words ? ' requests reported blocked' : ''}${counted}`
}

/**
 * Why a connector stands where it does at a place, as a sentence: the cell's tooltip, and the place inspector's reason
 * so a touch screen reads it too. ``labelOf`` names another level of the place.
 */
function cellReason(connector: SiteConnector, cell: PlaceCell, row: SitePlace, site: string, labelOf: (place: string) => string): string {
  const here = placeWords(row)
  if (cell.state === 'takes_none') {
    if (connector.excluded_reason === 'disabled') return 'Takes none of these requests: it is disabled'
    if (connector.excluded_reason === 'traffic_limit') return 'Takes none of these requests: it is over its traffic limit'
    return `Takes none of these requests: it filters out ${site}, a reservation or its country list keeps it off the country, it cannot serve this place, or it has no healthy proxy here`
  }
  if (!connector.dynamic) {
    const eligible = cell.eligible_ips ?? 0
    const usable = cell.usable_ips ?? 0
    if (cell.state === 'no_reports') return `No reports about its IPs for ${site}: all ${eligible.toLocaleString()} here usable`
    const avoided = eligible - usable
    return `${usable.toLocaleString()} of ${eligible.toLocaleString()} of its IPs here usable for ${site}${avoided > 0 ? `; ${avoided.toLocaleString()} ${plural(avoided, 'is', 'are')} avoided for it` : ''}`
  }
  const counts = countsText(cell)
  const told = countsText(cell, true)
  const levels = placeLevels(row.place)
  switch (cell.state) {
    case 'own': {
      if (!counts || !cell.requests) return `Its own factor from ${here} is ${factorText(cell.reputation)}`
      const rate = Math.min(100, Math.round(((cell.blocked ?? 0) / cell.requests) * 100))
      const reported = `${rate}% of its requests to ${site} from ${here} were reported blocked (${counts})`
      return cell.reputation >= 1 ? `${reported}, too few to lower its weight` : reported
    }
    case 'inherited': {
      const from = cell.reputation_place != null ? labelOf(cell.reputation_place) : 'a coarser level'
      const uses = `uses the factor from ${from}, ${factorText(cell.reputation)}`
      return counts
        ? `Too little evidence from ${here} yet to judge it there (${told}), so it ${uses}`
        : `No report about it has named ${here} yet, so its requests here are not counted on their own. It ${uses}`
    }
    case 'not_judged':
      return `${levels.length > 1 ? `Reported, but no level of ${here} has enough evidence yet` : `Reported, but too little evidence from ${here} yet`}, so its weight is unchanged${told ? ` (${told})` : ''}`
    default:
      return `No reports about it for ${site} from ${here}${levels.length > 1 ? ' or a coarser level' : ''}, so its weight is unchanged`
  }
}

/**
 * A dynamic connector's factor at each level of the place, finest first: "New York, NY ×0.3, then NY, US ×0.4, then
 * United States ×0.6". Routing uses the first that has one. Empty for a place of one level.
 */
function levelLine(connectorId: string, place: string, tree: PlaceTree, labelOf: (place: string) => string): string {
  const levels = placeLevels(place)
  if (levels.length < 2) return ''
  return levels.map((level) => {
    const factor = tree.byPlace.get(level)?.cells[connectorId]?.own_factor
    return `${labelOf(level)} ${factor != null ? factorText(factor) : 'no factor'}`
  }).join(', then ')
}

/** A cell's look by its state: tinted by its own factor, dashed with the level it came from, hatched, plain, or a dash. */
function CellValue({ cell, labelOf, className }: { cell: PlaceCell | undefined; labelOf: (place: string) => string; className?: string }) {
  const state = cell?.state ?? 'takes_none'
  const look = STATE_LOOKS[state]
  const box = 'inline-flex items-center justify-center rounded-md px-1.5 min-w-11 h-8 pointer-coarse:min-h-11 tabular-nums text-[12.5px]'
  if (!cell || state === 'takes_none') return <span className={cn(box, look.text, className)}>-</span>
  if (state === 'no_reports') {
    return <span className={cn(box, 'gap-1', look.text, className)}>×1<span aria-hidden>·</span></span>
  }
  if (state === 'inherited') {
    return (
      // The level tag goes over the factor on a phone, where the column is narrow.
      <span className={cn(box, 'flex-col @2xl:flex-row gap-0 @2xl:gap-1 leading-tight', look.box, factorTone(cell.reputation), className)}>
        {cell.reputation_place != null && (
          <span className="text-[10px] leading-none px-1 py-0.5 rounded-sm bg-surface-raised text-fg-subtle" title={`Uses the factor from ${labelOf(cell.reputation_place)}`}>
            {levelName(cell.reputation_place)}
          </span>
        )}
        {factorText(cell.reputation)}
      </span>
    )
  }
  if (state === 'not_judged') return <span className={cn(box, look.box, look.text, className)}>×1</span>
  return <span className={cn(box, factorTint(cell.reputation), look.text, cell.reputation < 1 && 'font-medium', className)}>{factorText(cell.reputation)}</span>
}

/** All, Lowered and Not judged, applied at once: three parts across a phone, by the length of their words, their counts under them. */
function Presets({ value, counts, onChange }: { value: Preset; counts: Record<Preset, number>; onChange: (preset: Preset) => void }) {
  return (
    <div className="grid grid-cols-[0.8fr_1fr_1.2fr] w-full @2xl:w-auto @2xl:inline-grid @2xl:grid-cols-[auto_auto_auto] bg-surface-raised rounded-lg p-0.5" role="radiogroup" aria-label="Which places">
      {PRESETS.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            'rounded-md font-medium transition-colors px-1 @2xl:px-3 py-1 text-[13px] leading-tight whitespace-nowrap pointer-coarse:min-h-11',
            'flex flex-col @2xl:flex-row items-center justify-center @2xl:gap-1.5',
            value === o.value ? 'bg-surface text-fg shadow-xs' : 'text-fg-muted hover:text-fg',
          )}
        >
          {o.label}
          <span className="text-[11px] @2xl:text-[13px] font-normal tabular-nums text-fg-subtle">{counts[o.value].toLocaleString()}</span>
        </button>
      ))}
    </div>
  )
}

/** The places under a tile's bar, by where the connector stands, with counts so a touch screen reads it too. */
const BUCKETS: { key: keyof SiteConnector['tile']['buckets']; label: string; look: string }[] = [
  { key: 'heavy', label: 'under ×0.5', look: 'bg-danger/55' },
  { key: 'lowered', label: 'under ×1', look: 'bg-danger/20' },
  { key: 'inherited', label: 'inherited', look: 'border border-dashed border-fg-subtle' },
  { key: 'full', label: 'full weight', look: 'bg-surface-raised' },
  { key: 'not_judged', label: 'not judged', look: HATCHED },
]

/** One connector across the site's places: where it is lowered per level, and its places by where it stands. */
function ConnectorTile({ connector, index, applied }: { connector: SiteConnector; index: number; applied: boolean }) {
  const { tile } = connector
  const levels = ([
    [tile.lowered_countries, tile.countries, 'country', 'countries'],
    [tile.lowered_states, tile.states, 'state', 'states'],
    [tile.lowered_cities, tile.cities, 'city', 'cities'],
  ] as const).filter(([, total]) => total > 0)
  // A connector with IPs is read by its usable rows: its value at a city often stands on one or two of them.
  const lowered = connector.dynamic && levels.length > 0
    ? `${applied ? 'Lowered' : 'Would be lowered'} in ${levels.map(([k, n, one, many]) => `${k.toLocaleString()} of ${n.toLocaleString()} ${plural(n, one, many)}`).join(', ')}`
    : null
  const buckets = BUCKETS.filter((b) => tile.buckets[b.key] > 0)
  const total = buckets.reduce((sum, b) => sum + tile.buckets[b.key], 0)
  const eligible = tile.eligible_ips ?? 0
  return (
    <div className="flex-none w-[13.75rem] snap-start @2xl:w-auto min-w-0 rounded-lg border border-line px-3 py-2 space-y-1.5">
      <div className="flex items-center gap-1.5 min-w-0 text-[13px] font-medium text-fg">
        <span className={cn('w-2 h-2 rounded-full flex-none', swatch(index))} aria-hidden />
        <ProviderLogo type={connector.credential_type} className="w-4 h-4 text-[16px] flex-none" />
        <span className="truncate" title={connector.name}>{connector.name}</span>
        <span className="text-[11px] font-normal text-fg-subtle flex-none">
          {connector.excluded_reason === 'disabled' ? 'disabled' : connector.excluded_reason === 'traffic_limit' ? 'over traffic limit' : connector.dynamic ? 'dynamic' : null}
        </span>
      </div>
      {!connector.dynamic && (
        <p className="text-xs text-fg-muted">
          {eligible > 0
            ? <>{applied ? '' : 'Would be '}{factorText(tile.reputation ?? 1)} with no place named: {(tile.usable_ips ?? 0).toLocaleString()} of {eligible.toLocaleString()} rows usable</>
            : 'No eligible rows for requests naming no place'}
        </p>
      )}
      {lowered ? <p className="text-xs text-fg-muted">{lowered}</p> : connector.dynamic && <p className="text-xs text-fg-subtle">No factor of its own at a listed place yet</p>}
      {total > 0 && (
        <>
          <div className="flex h-1.5 gap-px rounded-full overflow-hidden" aria-hidden>
            {buckets.map((b) => <span key={b.key} className={cn('h-full box-border', b.look)} style={{ flex: tile.buckets[b.key] }} />)}
          </div>
          <p className="text-[11px] text-fg-subtle leading-snug">{buckets.map((b) => `${tile.buckets[b.key].toLocaleString()} ${b.label}`).join(' · ')}</p>
        </>
      )}
    </div>
  )
}

/** The one-line account above the tiles: how many connectors are lowered somewhere, and the hardest hit. */
function Summary({ data, site }: { data: SitePlacesView; site: string }) {
  const { lowered_connectors: lowered, serving_connectors: serving, hardest } = data.summary
  const applied = data.reputation_applied
  if (serving === 0) return <p className="text-[13px] text-fg-muted wrap-anywhere">On <b className="font-medium text-fg">{site}</b>, no connector takes these requests.</p>
  if (lowered === 0) return <p className="text-[13px] text-fg-muted wrap-anywhere">On <b className="font-medium text-fg">{site}</b>, no connector {applied ? 'is' : 'would be'} lowered.</p>
  const connector = hardest ? data.connectors.find((c) => c.connector_id === hardest.connector_id) : undefined
  const place = hardest ? data.places.find((p) => p.place === hardest.place) : undefined
  return (
    <p className="text-[13px] text-fg-muted wrap-anywhere">
      On <b className="font-medium text-fg">{site}</b>, {lowered.toLocaleString()} of {serving.toLocaleString()} {plural(serving, 'connector', 'connectors')}{' '}
      {applied ? plural(lowered, 'is', 'are') : 'would be'} lowered somewhere.
      {hardest && connector && place && (
        <>
          {' '}Hardest hit: <b className="font-medium text-fg">{connector.name} {place.place ? `in ${place.label}` : 'for requests naming no place'} ({factorText(hardest.value)})</b>.
        </>
      )}
    </p>
  )
}

/** The card's body for one site: controls, summary, tiles and the places table. Its open places are the site's own. */
function SiteBody({ data, site, sitePicker, preset, onPreset, inspecting, onInspect, onExitIps }: {
  data: SitePlacesView
  site: string
  sitePicker: ReactNode
  preset: Preset
  onPreset: (preset: Preset) => void
  inspecting: string | null
  onInspect: (place: string) => void
  onExitIps: () => void
}) {
  // The order rows were first shown in, kept for the site (the body is keyed by it).
  const rank = useRef(new Map<string, number>())
  const tree = useMemo(() => new PlaceTree(data.places, rank.current), [data.places])
  // Places opened or closed by hand (else open while a preset narrows the rows), and those showing all their children.
  const [open, setOpen] = useState(new Map<string, boolean>())
  const [more, setMore] = useState(new Set<string>())
  const [highlight, setHighlight] = useState<string | null>(null)
  const rows = useRef(new Map<string, HTMLTableRowElement>())

  const isOpen = (place: string) => open.get(place) ?? preset !== 'all'
  const lines: Line[] = []
  const walk = (nodes: SitePlace[], depth: number) => {
    for (const row of nodes) {
      const kids = tree.kids(row.place, preset)
      const opened = kids.length > 0 && isOpen(row.place)
      lines.push({ kind: 'place', row, depth, kids: kids.length, open: opened, dim: !matches(row, preset) })
      if (!opened) continue
      const shown = more.has(row.place) ? kids : kids.slice(0, FIRST_CHILDREN)
      walk(shown, depth + 1)
      if (shown.length < kids.length) lines.push({ kind: 'more', parent: row.place, depth: depth + 1, hidden: kids.slice(shown.length) })
    }
  }
  walk(tree.roots.filter((row) => tree.shows(row, preset)), 0)

  const parents = [...tree.children.keys()]
  const allOpen = parents.length > 0 && parents.every((place) => isOpen(place))
  const counts: Record<Preset, number> = {
    all: data.places.length,
    lowered: data.places.filter((p) => p.lowered).length,
    not_judged: data.places.filter((p) => p.not_judged).length,
  }
  const labelOf = (place: string) => tree.byPlace.get(place)?.label ?? place.split('/').filter(Boolean).join(', ')

  // Opens every level above the place and the "Show more" hiding it, dropping a preset that leaves it out; then the
  // row is scrolled to and marked.
  const jump = (place: string) => {
    const row = tree.byPlace.get(place)
    if (!row) return
    const within = tree.shows(row, preset) ? preset : 'all'
    if (within !== preset) onPreset(within)
    const opened = new Map(within === preset ? open : [])
    const shown = new Set(more)
    let child = place
    for (const parent of tree.ancestors(place)) {
      opened.set(parent, true)
      if (tree.kids(parent, within).findIndex((kid) => kid.place === child) >= FIRST_CHILDREN) shown.add(parent)
      child = parent
    }
    setOpen(opened)
    setMore(shown)
    setHighlight(place)
  }
  // Again once a dropped preset lands: it comes through the address, after the row was marked.
  useEffect(() => {
    if (!highlight) return
    rows.current.get(highlight)?.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' })
    const timer = setTimeout(() => setHighlight(null), 2000)
    return () => clearTimeout(timer)
  }, [highlight, preset])

  const pickPreset = (next: Preset) => {
    onPreset(next)
    setOpen(new Map())
  }
  const toggle = (place: string) => setOpen(new Map(open).set(place, !isOpen(place)))
  const toggleAll = () => setOpen(new Map(parents.map((place) => [place, !allOpen])))

  const applied = data.reputation_applied
  const jumpOptions = data.places.map((p) => ({
    value: p.place || NO_PLACE_OPTION,
    label: p.label,
    badge: `${LEVEL_WORDS[p.level]} · ${compact(p.reports)}`,
  }))
  const connectors = data.connectors
  const rule = ruleOf(data)
  return (
    <div className="space-y-3">
      {/* A phone: the site, then the presets across, then Jump and Expand all; one wrapping line once there is room. */}
      <div className="flex flex-col gap-2 @2xl:flex-row @2xl:flex-wrap @2xl:items-center @2xl:gap-x-3">
        {sitePicker}
        {data.places.length > 0 && (
          <>
            <Presets value={preset} counts={counts} onChange={pickPreset} />
            <div className="flex items-center gap-2 @2xl:ml-auto">
              <RichSelect
                size="sm"
                searchable
                options={jumpOptions}
                value=""
                placeholder="Jump to place"
                onChange={(value) => jump(value === NO_PLACE_OPTION ? '' : value)}
                aria-label="Jump to place"
                className="flex-1 min-w-0 @2xl:flex-none @2xl:w-44"
              />
              {parents.length > 0 && (
                <button type="button" onClick={toggleAll} className="flex-none text-xs font-medium text-primary hover:brightness-110 px-1 pointer-coarse:min-h-11">
                  {allOpen ? 'Collapse all' : 'Expand all'}
                </button>
              )}
            </div>
          </>
        )}
      </div>
      {data.places.length === 0 ? (
        <p className="text-xs text-fg-subtle wrap-anywhere">
          No place data for {site}: no dynamic connector has a record for it, and no connector with IPs has an IP avoided for it. Its connectors with IPs are weighed by their exit IPs,{' '}
          <button type="button" onClick={onExitIps} className="text-primary hover:brightness-110 pointer-coarse:py-2">in the Exit IPs table below</button>.
        </p>
      ) : (
        <>
          <Summary data={data} site={site} />
          {/* One row that scrolls sideways on a phone, a grid once there is room. */}
          <div className="flex gap-2 overflow-x-auto snap-x snap-mandatory -mx-4 px-4 pb-1 @2xl:mx-0 @2xl:px-0 @2xl:pb-0 @2xl:grid @2xl:grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] @2xl:overflow-visible">
            {connectors.map((c, i) => <ConnectorTile key={c.connector_id} connector={c} index={i} applied={applied} />)}
          </div>
          <div className="overflow-x-auto -mx-4" role="region" aria-label={`Places for ${site}, scrolls sideways`} tabIndex={0}>
            <table className="min-w-full border-separate border-spacing-0 text-[12.5px]">
              <thead>
                <tr className="text-[11px] text-fg-subtle">
                  <th scope="col" className={cn(CELL, FIRST, 'bg-surface font-medium')}>
                    Place
                    {/* A phone has the split under each name. */}
                    <span className="@2xl:hidden font-normal"> · {applied ? 'split' : 'split (would)'}</span>
                  </th>
                  <th scope="col" className={cn(CELL, WIDE, 'font-medium text-right')} title="Reports through dynamic connectors at this exact place since the first; a country's include its cities'">Reports</th>
                  <th scope="col" className={cn(CELL, WIDE, 'font-medium text-left')} title={applied ? 'The expected split of the place\'s requests, by weight times reputation' : 'The split Avoid would make of the place\'s requests, by weight times reputation'}>
                    {applied ? 'Split here' : 'Split here (would)'}
                  </th>
                  {connectors.map((c, i) => (
                    <th key={c.connector_id} scope="col" className={cn(CELL, 'font-medium text-center')} title={c.name}>
                      <span className="inline-flex items-center gap-1 max-w-[3.75rem] @2xl:max-w-[7rem]">
                        <span className={cn('w-2 h-2 rounded-full flex-none', swatch(i))} aria-hidden />
                        <span className="truncate">{c.name}</span>
                      </span>
                    </th>
                  ))}
                  <th scope="col" className={cn(CELL, WIDE, 'font-medium text-center')} title="The lowest value in force among the connectors that take the place's requests">Lowest</th>
                  <th aria-hidden className="w-2 border-b border-line" />
                </tr>
              </thead>
              <tbody>
                {lines.map((line) => line.kind === 'more' ? (
                  <tr key={`more:${line.parent}`}>
                    <td className={cn(CELL, 'sticky left-0 z-[1] bg-surface pl-4')}>
                      <div className={INDENT[Math.min(line.depth, 2)]}>
                      <button
                        type="button"
                        onClick={() => setMore(new Set(more).add(line.parent))}
                        className="text-xs font-medium text-primary hover:brightness-110 pointer-coarse:min-h-11"
                      >
                        Show {line.hidden.length.toLocaleString()} more {line.hidden.every((p) => p.level === 'city') ? plural(line.hidden.length, 'city', 'cities') : plural(line.hidden.length, 'place', 'places')}
                      </button>
                      </div>
                    </td>
                    <td colSpan={connectors.length + 4} className="border-b border-line" />
                  </tr>
                ) : (
                  <PlaceRow
                    key={line.row.place || NO_PLACE_OPTION}
                    line={line}
                    nested={parents.length > 0}
                    connectors={connectors}
                    site={site}
                    labelOf={labelOf}
                    rule={rule}
                    active={inspecting === line.row.place}
                    highlighted={highlight === line.row.place}
                    onToggle={() => toggle(line.row.place)}
                    onOpen={() => onInspect(line.row.place)}
                    rowRef={(el) => { if (el) rows.current.set(line.row.place, el); else rows.current.delete(line.row.place) }}
                  />
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-fg-muted">
            {data.places.length.toLocaleString()} {plural(data.places.length, 'place', 'places')} with records for this site. A tinted cell is the connector's own factor there; a dashed one uses a coarser level's, the place having too little evidence of its own; a hatched one is reported on but not judged yet; ×1 with a dot has no reports; "-" takes none of these requests.
            {applied ? '' : ' The project does not avoid, so the split and the lowered counts are what Avoid would do.'} Open a place for the reasons.
          </p>
        </>
      )}
    </div>
  )
}

/** One place: its name (a button that opens it), reports, the split, a cell per connector and the lowest value. */
function PlaceRow({ line, nested, connectors, site, labelOf, rule, active, highlighted, onToggle, onOpen, rowRef }: {
  line: Extract<Line, { kind: 'place' }>
  /** Some place has places under it: a row without them keeps the chevron's room, so names line up. */
  nested: boolean
  connectors: SiteConnector[]
  site: string
  labelOf: (place: string) => string
  rule: StrengthRule
  active: boolean
  highlighted: boolean
  onToggle: () => void
  onOpen: () => void
  rowRef: (el: HTMLTableRowElement | null) => void
}) {
  const { row, depth, kids, open, dim } = line
  // Every cell carries the row's background, the sticky one too, so a marked row reads across.
  const bg = active || highlighted ? 'bg-primary-soft' : 'bg-surface group-hover:bg-surface-raised'
  const share = (c: SiteConnector) => row.cells[c.connector_id]?.effective_share ?? 0
  const citiesTotal = row.cities_total ?? 0
  const citiesLowered = row.cities_lowered ?? 0
  // A state or a city with no state sits under its country's row, so it drops the country's name: "New York", "Nairobi".
  const country = row.parent != null && !row.parent.includes('/') ? labelOf(row.parent) : null
  const name = country && row.label.endsWith(`, ${country}`) ? row.label.slice(0, -(country.length + 2)) : row.label
  return (
    <tr
      ref={rowRef}
      onClick={(e) => { if (!(e.target as HTMLElement).closest('button, a')) onOpen() }}
      className={cn('group cursor-pointer', dim && 'text-fg-subtle')}
    >
      <th scope="row" className={cn(CELL, FIRST, bg, 'font-normal transition-colors', active && 'shadow-[inset_2px_0_0_var(--color-primary)]')}>
        <div className={cn('flex items-center gap-1 min-w-0', INDENT[Math.min(depth, 2)])}>
          {kids > 0 ? (
            <button
              type="button"
              onClick={onToggle}
              aria-expanded={open}
              aria-label={`${open ? 'Hide' : 'Show'} the places in ${row.label}`}
              className="flex-none w-5 h-5 -ml-1 pointer-coarse:w-11 pointer-coarse:h-11 pointer-coarse:-my-1.5 inline-flex items-center justify-center rounded-md text-fg-subtle hover:text-fg hover:bg-surface-raised"
            >
              <ChevronRight className={cn('w-3.5 h-3.5 transition-transform', open && 'rotate-90')} />
            </button>
          ) : nested && <span className="flex-none w-4 pointer-coarse:w-10" aria-hidden />}
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 min-w-0">
              <button
                type="button"
                onClick={onOpen}
                title={row.label}
                className={cn('min-w-0 max-w-full truncate text-left hover:underline pointer-coarse:min-h-11', depth === 0 ? 'font-medium' : '', dim ? 'text-fg-subtle' : 'text-fg')}
              >
                {name}
              </button>
              {citiesTotal > 0 && (
                <span
                  className={cn('flex-none text-[10.5px] leading-none px-1 py-0.5 rounded-sm', citiesLowered > 0 ? 'bg-danger/10 text-danger' : 'bg-surface-raised text-fg-subtle')}
                  title={`${citiesLowered} of ${citiesTotal} listed cities with a factor under 1`}
                >
                  <span className="@2xl:hidden">{citiesLowered}/{citiesTotal} lowered</span>
                  <span className="hidden @2xl:inline">{citiesLowered} of {citiesTotal} {plural(citiesTotal, 'city', 'cities')} lowered</span>
                </span>
              )}
            </div>
            <ShareBar connectors={connectors} share={share} className="@2xl:hidden h-1 mt-1 max-w-[7rem]" />
          </div>
        </div>
      </th>
      <td className={cn(CELL, WIDE, bg, 'text-right tabular-nums text-fg-muted')} title={`${row.reports.toLocaleString()} reports since the first`}>{compact(row.reports)}</td>
      <td className={cn(CELL, WIDE, bg)}><ShareBar connectors={connectors} share={share} className="w-32" /></td>
      {connectors.map((c) => {
        const cell = row.cells[c.connector_id]
        const compared = cell && takes(cell) ? strengthNote(cell, c.connector_id, rule, ownCounts(row)) : ''
        return (
          <td key={c.connector_id} className={cn(CELL, bg, 'text-center')} title={cell ? `${c.name}: ${cellReason(c, cell, row, site, labelOf)}.${compared ? ` ${compared}` : ''}` : undefined}>
            <CellValue cell={cell} labelOf={labelOf} />
          </td>
        )
      })}
      <td className={cn(CELL, WIDE, bg, 'text-center')}>
        {row.lowest
          ? (
            <span
              className={cn('inline-flex items-center justify-center rounded-md px-1.5 h-6 tabular-nums', factorTone(row.lowest.value), row.lowest.inherited && STATE_LOOKS.inherited.box)}
              title={row.lowest.inherited ? 'A coarser level\'s factor' : undefined}
            >
              {factorText(row.lowest.value)}
            </span>
          )
          : <span className="text-fg-subtle">-</span>}
      </td>
      <td aria-hidden className={cn('w-2 border-b border-line', bg)} />
    </tr>
  )
}

/**
 * One site across every place its records name: pick a site (the busiest with places by default), narrow the places
 * to the lowered or not judged ones, or jump to one. A place opens in the place inspector.
 */
export function SitePlacesCard({ query, site, onSite, preset, onPreset, inspecting, onInspect, onExitIps }: {
  query: UseQueryResult<SitePlacesView>
  /** The site asked for; '' is the default one. */
  site: string
  onSite: (site: string) => void
  preset: Preset
  onPreset: (preset: Preset) => void
  /** The place open in the inspector, for the site shown. */
  inspecting: string | null
  onInspect: (place: string) => void
  /** Brings the Exit IPs table into view, for a site with no place data. */
  onExitIps: () => void
}) {
  const data = query.data
  const shown = data?.site ?? ''
  const sites = data?.sites ?? []
  // A site asked for that the last answer does not list yet stays picked, and listed.
  const siteOptions = [
    ...(site && !sites.some((s) => s.site === site) ? [{ value: site, label: site }] : []),
    ...sites.map((s) => ({ value: s.site, label: s.site, badge: `${compact(s.reports)} ${plural(s.reports, 'report', 'reports')}` })),
  ]
  const sitePicker = (
    <div className="flex items-center gap-1.5 min-w-0 text-xs text-fg-muted">
      <span className="flex-none" aria-hidden>Site</span>
      <RichSelect
        size="sm"
        searchable
        options={siteOptions}
        value={site || shown}
        onChange={(value) => { if (value !== (site || shown)) onSite(value) }}
        aria-label="Site"
        className="flex-1 min-w-0 @2xl:flex-none @2xl:w-56"
      />
    </div>
  )
  return (
    <Card className="px-4 py-3">
      <CardHeader
        title={
          <span className="inline-flex items-center gap-1.5">
            Every place for a site
            <InfoTip label="About every place for a site">
              Each row is a place requests to the site named, and each cell what one connector's weight is multiplied by for them. A dynamic connector's factor is one less its block rate from the place, once the place has enough requests of its own (20), else the nearest coarser level's; a connector with IPs has the share of its IPs there not avoided for the site.{data && data.reputation_strength > 1 && ` At strength ${data.reputation_strength}, a dynamic connector's factor is then compared with the best one's at the place, never under the lowest factor, ${factorText(data.reputation_floor)}.`} Split here is the expected split of the place's requests by weight times that factor.
            </InfoTip>
          </span>
        }
        className="mb-2"
      />
      {loadFailed(query) ? (
        <LoadError what="the places" query={query} />
      ) : !data ? (
        <LogoLoader size="sm" className="py-4" />
      ) : (
        <Reloading active={query.isPlaceholderData} align="top">
          <SiteBody
            key={shown}
            data={data}
            site={shown}
            sitePicker={sitePicker}
            preset={preset}
            onPreset={onPreset}
            inspecting={inspecting}
            // A row of the last site, up while the picked one loads, opens nothing.
            onInspect={(place) => { if (!query.isPlaceholderData) onInspect(place) }}
            onExitIps={onExitIps}
          />
        </Reloading>
      )}
    </Card>
  )
}

/** One connector in the place inspector: its value, how its share moves, why, and its factor at every level. */
function PlaceConnector({ connector, index, cell, row, site, tree, labelOf, rule }: {
  connector: SiteConnector
  index: number
  cell: PlaceCell
  row: SitePlace
  site: string
  tree: PlaceTree
  labelOf: (place: string) => string
  rule: StrengthRule
}) {
  const serving = takes(cell)
  const compared = serving ? strengthNote(cell, connector.connector_id, rule, ownCounts(row)) : ''
  const levels = connector.dynamic && serving ? levelLine(connector.connector_id, row.place, tree, labelOf) : ''
  const delta = cell.effective_share - cell.configured_share
  return (
    <div className="py-2 border-b border-line last:border-b-0 space-y-0.5 text-[13px]">
      <div className="flex items-center gap-2 min-w-0">
        <span className={cn('w-2 h-2 rounded-full flex-none', swatch(index))} aria-hidden />
        <ProviderLogo type={connector.credential_type} className="w-4 h-4 text-[16px] flex-none" />
        <span className="font-medium text-fg truncate">{connector.name}</span>
        {connector.dynamic && <span className="text-[11px] text-fg-subtle flex-none">dynamic</span>}
        <CellValue cell={cell} labelOf={labelOf} className="ml-auto flex-none h-6 pointer-coarse:min-h-0 flex-row gap-1" />
      </div>
      {serving && (
        <div className="flex justify-between gap-3 tabular-nums text-fg-muted">
          <span>{formatShare(cell.configured_share)} → {formatShare(cell.effective_share)}</span>
          <span className={cn('font-medium', Math.round(delta) < 0 ? 'text-danger' : Math.round(delta) > 0 ? 'text-success' : 'text-fg-subtle')}>
            {points(cell.configured_share, cell.effective_share)}
          </span>
        </div>
      )}
      <p className="text-xs text-fg-muted wrap-anywhere">{cellReason(connector, cell, row, site, labelOf)}.{compared && ` ${compared}`}</p>
      {levels && <p className="text-xs text-fg-subtle">By level: {levels}.</p>}
    </div>
  )
}

/**
 * A place of a site in the side panel: the configured and effective split of its requests, and every connector by
 * where it stands, with the reason in words. It reads the card's answer for the site as asked ('' the default one),
 * so it follows the poll; the page closes it when the place drops out of that answer.
 */
export function PlaceInspector({ projectId, site, place, onClose }: { projectId: string; site: string; place: string; onClose: () => void }) {
  const query = useQuery(placesQuery(projectId, site))
  const data = query.data
  const row = data?.places.find((p) => p.place === place)
  const tree = useMemo(() => new PlaceTree(data?.places ?? [], new Map()), [data?.places])
  if (!data || !row) {
    return (
      <Inspector title="Place" subtitle={data?.site ?? site} onClose={onClose}>
        {loadFailed(query) ? <LoadError what="the place" query={query} /> : <LogoLoader className="py-10" />}
      </Inspector>
    )
  }
  const labelOf = (place: string) => tree.byPlace.get(place)?.label ?? place.split('/').filter(Boolean).join(', ')
  const applied = data.reputation_applied
  const rule = ruleOf(data)
  const entries = data.connectors.map((connector, index) => ({ connector, index, cell: row.cells[connector.connector_id] })).filter((e) => !!e.cell)
  // A value under 1 is lowered here when it is the place's own, and lowered via a coarser level when inherited from
  // it: a lane per level, the nearest first.
  const inheritedLow = entries.filter((e) => e.cell.state === 'inherited' && e.cell.reputation < 1)
  const coarser = placeLevels(row.place).slice(1)
  const via = [...coarser, null].map((level) => ({
    title: `${applied ? 'Lowered' : 'Would be lowered'} via ${level == null ? 'a coarser place' : labelOf(level)}`,
    lowered: true,
    items: inheritedLow.filter((e) => (level == null ? !coarser.includes(e.cell.reputation_place ?? '') : e.cell.reputation_place === level)),
  }))
  const lanes = [
    { title: applied ? 'Lowered here' : 'Would be lowered here', lowered: true, items: entries.filter((e) => e.cell.state === 'own' && e.cell.reputation < 1) },
    ...via,
    { title: 'Full weight', lowered: false, items: entries.filter((e) => takes(e.cell) && e.cell.reputation >= 1) },
    { title: 'Takes none here', lowered: false, items: entries.filter((e) => !takes(e.cell)) },
  ].filter((lane) => lane.items.length > 0)
  const moved = entries.reduce((sum, e) => sum + Math.max(0, e.cell.effective_share - e.cell.configured_share), 0)
  const serving = entries.some((e) => takes(e.cell))
  // The note on relative weights goes under the last lowered lane, when a lowered connector in any of them gains.
  const lastLowered = lanes.filter((lane) => lane.lowered).pop()
  const gains = lanes.some((lane) => lane.lowered && lane.items.some((e) => Math.round(e.cell.effective_share - e.cell.configured_share) > 0))
  const configured = (c: SiteConnector) => row.cells[c.connector_id]?.configured_share ?? 0
  const effective = (c: SiteConnector) => row.cells[c.connector_id]?.effective_share ?? 0
  return (
    <Inspector
      title={row.label}
      subtitle={`Requests to ${data.site} · ${row.reports.toLocaleString()} ${plural(row.reports, 'report', 'reports')}`}
      onClose={onClose}
    >
      <InspectorSection title={applied ? 'Split here' : 'Split here, as Avoid would make it'}>
        {serving ? (
          <div className="space-y-2">
            <div className="space-y-1">
              <div className="flex justify-between text-xs text-fg-muted"><span>Configured</span></div>
              <ShareBar connectors={data.connectors} share={configured} className="h-1.5 opacity-60" />
            </div>
            <div className="space-y-1">
              <div className="flex justify-between gap-3 text-xs text-fg-muted">
                <span>{applied ? 'Effective' : 'Would be'}</span>
                <span className="tabular-nums">{formatShare(moved)} of these requests {applied ? 'move' : 'would move'}</span>
              </div>
              <ShareBar connectors={data.connectors} share={effective} />
            </div>
            {!applied && <p className="text-xs text-fg-muted">The project does not avoid now: routing uses the configured split, and these are what Avoid would do.</p>}
          </div>
        ) : (
          <p className="text-xs text-fg-subtle">No connector takes these requests.</p>
        )}
      </InspectorSection>
      {lanes.map((lane) => (
        <InspectorSection key={lane.title} title={`${lane.title} · ${lane.items.length}`}>
          <div>
            {lane.items.map((e) => (
              <PlaceConnector key={e.connector.connector_id} connector={e.connector} index={e.index} cell={e.cell} row={row} site={data.site ?? ''} tree={tree} labelOf={labelOf} rule={rule} />
            ))}
          </div>
          {lane === lastLowered && gains && (
            <p className="text-xs text-fg-muted">Weights are relative: a lowered connector can still gain share when the others lose more.</p>
          )}
        </InspectorSection>
      ))}
      <p className="text-xs text-fg-subtle">
        Counts are as routing last decided the factor, not faded since: a quiet place keeps its factor until its next requests decide it again.
      </p>
    </Inspector>
  )
}
