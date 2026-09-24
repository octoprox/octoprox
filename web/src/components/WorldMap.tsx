// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import worldMap from '../assets/world-map.json'
import { cn } from '../utils/cn'
import { plural } from '../utils/format'

export interface CountryStat {
  total: number
  healthy: number
  /** Distinct connectors with proxies in the country (project-wide view). */
  connectors?: number
}

/**
 * Countries reachable through dynamic-sessions connectors, which hold no exit of
 * their own to plot: the vendor picks one per request from wherever the connector allows.
 */
export interface DynamicCoverage {
  /** Country code to the number of dynamic connectors whose allow-list names it. */
  countries: Record<string, number>
  /** Dynamic connectors with no allow-list, which can exit from any country. */
  worldwide: number
}

interface Location {
  id: string
  name: string
  d: string
}

const LOCATIONS = worldMap.locations as Location[]
const NAMES: Record<string, string> = Object.fromEntries(LOCATIONS.map((l) => [l.id, l.name]))
// 1010x666; the dataset has no Antarctica, so nothing needs cropping.
const VIEW_BOX = worldMap.viewBox
// Countries reachable only through dynamic sessions are tinted, not counted: there is no
// pool there to size, so they sit below the lightest choropleth step (0.35).
const DYNAMIC_OPACITY = 0.15
// Dashed, unfilled: nothing is provisioned there, unlike the solid pills of countries with proxies.
const DYNAMIC_PILL = 'inline-flex flex-none items-center gap-1.5 rounded-full border border-dashed border-line pl-2 pr-2.5 py-0.5'

/** Country name for an ISO code, falling back to the code itself. */
export const countryName = (code: string) => NAMES[code.toUpperCase()] ?? code

/**
 * Choropleth of proxies per country. Countries with proxies are filled in the
 * primary colour, darker the more proxies they hold; hovering shows the count.
 * Countries that dynamic-sessions connectors can exit from (their allow-list, or the
 * whole world when they have none) get a faint tint instead, since nothing is
 * provisioned there until a request asks. A single scrollable row of pills below
 * the map lists every country, busiest first. Used on the Overview page.
 */
export function WorldMap({ stats, dynamic, unknown = 0, className }: {
  stats: Record<string, CountryStat>
  dynamic?: DynamicCoverage
  unknown?: number
  className?: string
}) {
  const [hover, setHover] = useState<{ id: string; x: number; y: number; w: number; h: number } | null>(null)
  const max = useMemo(() => Math.max(1, ...Object.values(stats).map((s) => s.total)), [stats])
  const ranked = useMemo(
    () => Object.entries(stats).sort((a, b) => b[1].total - a[1].total || a[0].localeCompare(b[0])),
    [stats],
  )
  const worldwide = dynamic?.worldwide ?? 0
  // Allow-listed countries with no fixed proxies of their own; the rest already have a pill.
  const dynamicOnly = useMemo(
    () => Object.keys(dynamic?.countries ?? {}).filter((code) => !stats[code]).sort(),
    [dynamic, stats],
  )
  /** Dynamic connectors that can exit from the country: those naming it plus those allowing any. */
  const viaDynamic = (code: string) => (dynamic?.countries[code] ?? 0) + worldwide

  const hovered = hover ? stats[hover.id] : undefined
  const hoveredDynamic = hover ? viaDynamic(hover.id) : 0
  const hasPills = ranked.length > 0 || dynamicOnly.length > 0 || worldwide > 0 || unknown > 0

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <div
        // The SVG is absolutely positioned so its aspect ratio never adds intrinsic height: the card
        // takes its height from the charts beside it and the map letterboxes into whatever is left.
        className="relative flex-1 min-h-[160px] rounded-lg border border-line bg-surface-sunken/40 overflow-hidden"
        onMouseLeave={() => setHover(null)}
      >
        <svg viewBox={VIEW_BOX} className="absolute inset-0 w-full h-full" role="img" aria-label="Proxy locations">
          {LOCATIONS.map((loc) => {
            const stat = stats[loc.id]
            const reachable = stat != null || viaDynamic(loc.id) > 0
            const opacity = stat ? 0.35 + 0.65 * (stat.total / max) : reachable ? DYNAMIC_OPACITY : undefined
            return (
              <path
                key={loc.id}
                d={loc.d}
                fill={reachable ? 'rgb(var(--color-primary))' : 'rgb(var(--color-surface-raised))'}
                fillOpacity={opacity}
                stroke="rgb(var(--color-surface))"
                strokeWidth={0.6}
                className={reachable ? 'cursor-pointer' : undefined}
                onMouseMove={reachable ? (e) => {
                  const box = e.currentTarget.ownerSVGElement?.getBoundingClientRect()
                  if (box) setHover({ id: loc.id, x: e.clientX - box.left, y: e.clientY - box.top, w: box.width, h: box.height })
                } : undefined}
                onMouseLeave={reachable ? () => setHover(null) : undefined}
              />
            )
          })}
        </svg>
        {hover && (hovered || hoveredDynamic > 0) && (
          <div
            className="pointer-events-none absolute z-10 whitespace-nowrap rounded-md border border-line bg-surface px-2 py-1 text-xs shadow-md"
            style={{
              // Anchor to the side of the cursor with more room, so edge countries (New Zealand, Alaska) stay readable.
              ...(hover.x > hover.w / 2 ? { right: hover.w - hover.x + 12 } : { left: hover.x + 12 }),
              ...(hover.y > hover.h / 2 ? { bottom: hover.h - hover.y + 12 } : { top: hover.y + 12 }),
            }}
          >
            <div className="font-medium text-fg">{countryName(hover.id)}</div>
            {hovered ? (
              <div className="text-fg-muted tabular-nums">
                {hovered.total} {plural(hovered.total, 'proxy', 'proxies')}
                {hovered.healthy !== hovered.total && <> · {hovered.healthy} healthy</>}
                {hovered.connectors != null && hovered.connectors > 1 && <> · {hovered.connectors} connectors</>}
                {hoveredDynamic > 0 && <> · also via {hoveredDynamic} dynamic</>}
              </div>
            ) : (
              <div className="text-fg-muted tabular-nums">
                On request · {hoveredDynamic} dynamic {plural(hoveredDynamic, 'connector', 'connectors')}
              </div>
            )}
          </div>
        )}
      </div>
      {hasPills && (
        // One row regardless of how many countries: scroll sideways for the long tail.
        <ul className="flex flex-nowrap gap-1.5 overflow-x-auto pb-1 text-xs [scrollbar-width:thin]">
          {ranked.map(([code, stat]) => (
            <li key={code} className="inline-flex flex-none items-center gap-1.5 rounded-full border border-line bg-surface-raised pl-2 pr-2.5 py-0.5" title={countryName(code)}>
              <span className="font-mono font-medium">{code}</span>
              <span className={cn('tabular-nums font-medium', stat.healthy < stat.total && 'text-warning')}>{stat.healthy}/{stat.total}</span>
            </li>
          ))}
          {dynamicOnly.map((code) => (
            <li
              key={code}
              className={DYNAMIC_PILL}
              title={`${countryName(code)} · on request through ${viaDynamic(code)} dynamic ${plural(viaDynamic(code), 'connector', 'connectors')}`}
            >
              <span className="font-mono font-medium">{code}</span>
              <span className="text-fg-muted">dynamic</span>
            </li>
          ))}
          {worldwide > 0 && (
            <li
              className={DYNAMIC_PILL}
              title={`${worldwide} dynamic ${plural(worldwide, 'connector', 'connectors')} with no country allow-list: any country on request`}
            >
              <span className="font-medium">Any country</span>
              <span className="text-fg-muted">{worldwide} dynamic</span>
            </li>
          )}
          {unknown > 0 && <li className="inline-flex flex-none items-center px-2 py-0.5 text-fg-subtle">{unknown} unknown</li>}
        </ul>
      )}
    </div>
  )
}

export default WorldMap
