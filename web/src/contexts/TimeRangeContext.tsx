// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * One time range for the whole app. Every chart and table that shows a window
 * of history reads it from here, so picking 1h on the Overview also moves the
 * traffic split, the Hosts page, the connector and device charts, provider
 * accuracy and the system trends. It is remembered in the browser.
 *
 * Views differ in what they can show (claim statistics are daily, system
 * trends have no 6h bucket, project history stops at 30d), so each view
 * narrows the shared range to its own options with `useTimeRange(options)`.
 *
 * A view that shows the range also keeps it in the address (`?range=`), so a
 * shared link carries it. A link's range holds in the browser tab it opened
 * until a range is picked; it never changes the range saved in the browser.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState, ReactNode } from 'react'
import { useLocation } from 'react-router'
import { useSetUrlParams } from '../hooks/useUrlState'

export const TIME_RANGES = ['1h', '6h', '24h', '7d', '30d', '90d'] as const
export type TimeRange = (typeof TIME_RANGES)[number]

const HOUR = 3_600_000
export const TIME_RANGE_MS: Record<TimeRange, number> = {
  '1h': HOUR, '6h': 6 * HOUR, '24h': 24 * HOUR, '7d': 7 * 24 * HOUR, '30d': 30 * 24 * HOUR, '90d': 90 * 24 * HOUR,
}

/** A history chart's time axis: the range ending when its data arrived (a query's `dataUpdatedAt`), or now before any has. */
export function rangeDomain(range: TimeRange, dataUpdatedAt: number): [number, number] {
  const end = dataUpdatedAt || Date.now()
  return [end - TIME_RANGE_MS[range], end]
}

/** How far apart a range's axis ticks are: minutes of the local day inside a day, local midnights beyond it. */
const TICK_EVERY: Record<TimeRange, { minutes: number } | { days: number }> = {
  '1h': { minutes: 10 }, '6h': { minutes: 60 }, '24h': { minutes: 180 }, '7d': { days: 1 }, '30d': { days: 5 }, '90d': { days: 15 },
}

/**
 * Axis ticks for a history chart on round local times inside `[start, end]`: every 10 minutes for 1h, on the hour
 * for 6h, every 3 hours from midnight for 24h, then midnights (daily for 7d, every 5 days for 30d, 15 for 90d).
 */
export function rangeTicks(range: TimeRange, [start, end]: [number, number]): number[] {
  const every = TICK_EVERY[range]
  const t = new Date(start)
  if ('minutes' in every) {
    // The first multiple of the step since local midnight at or after the start.
    const into = t.getHours() * 60 + t.getMinutes() + (t.getSeconds() || t.getMilliseconds() ? 1 : 0)
    t.setHours(0, Math.ceil(into / every.minutes) * every.minutes, 0, 0)
  } else {
    t.setHours(0, 0, 0, 0)
    if (t.getTime() < start) t.setDate(t.getDate() + 1)
  }
  const ticks: number[] = []
  while (t.getTime() <= end) {
    ticks.push(t.getTime())
    if ('minutes' in every) t.setMinutes(t.getMinutes() + every.minutes)
    else t.setDate(t.getDate() + every.days)
  }
  return ticks
}

/** Axis tick for a history chart: the time of day inside a day, the date beyond it. */
export function formatRangeTick(epoch: number, range: TimeRange): string {
  const d = new Date(epoch)
  return TIME_RANGE_MS[range] <= TIME_RANGE_MS['24h']
    ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const STORAGE_KEY = 'octoprox_time_range'
const DEFAULT_RANGE: TimeRange = '24h'

export function isTimeRange(value: unknown): value is TimeRange {
  return typeof value === 'string' && (TIME_RANGES as readonly string[]).includes(value)
}

function readStored(): TimeRange {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return isTimeRange(stored) ? stored : DEFAULT_RANGE
  } catch {
    return DEFAULT_RANGE
  }
}

/**
 * The option a view shows for the shared range: the range itself when the
 * view offers it, otherwise the smallest option that still covers it, and
 * the widest option when none does. So 1h on a daily view reads 24h and 90d
 * on a 30-day view reads 30d.
 */
export function resolveTimeRange<T extends TimeRange>(range: TimeRange, options: readonly T[]): T {
  if ((options as readonly TimeRange[]).includes(range)) return range as T
  const sorted = [...options].sort((a, b) => TIME_RANGE_MS[a] - TIME_RANGE_MS[b])
  return sorted.find((o) => TIME_RANGE_MS[o] > TIME_RANGE_MS[range]) ?? sorted[sorted.length - 1]
}

interface TimeRangeContextType {
  range: TimeRange
  setRange: (range: TimeRange) => void
}

const TimeRangeContext = createContext<TimeRangeContextType | undefined>(undefined)

/** The range a link opened this browser tab with: read once, so Back to an older address never brings its range back. */
function readLinked(): TimeRange | null {
  const linked = new URLSearchParams(window.location.search).get('range')
  return isTimeRange(linked) ? linked : null
}

export function TimeRangeProvider({ children }: { children: ReactNode }) {
  const [stored, setStored] = useState<TimeRange>(readStored)
  const [linked, setLinked] = useState<TimeRange | null>(readLinked)
  const range = linked ?? stored

  // Picking a range ends a link's and saves the pick, as before.
  const setRange = useCallback((next: TimeRange) => {
    setLinked(null)
    setStored(next)
    try { localStorage.setItem(STORAGE_KEY, next) } catch { /* ignore */ }
  }, [])

  // Another tab picking a range picks it here too.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== STORAGE_KEY || !isTimeRange(e.newValue)) return
      setLinked(null)
      setStored(e.newValue)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const value = useMemo(() => ({ range, setRange }), [range, setRange])
  return <TimeRangeContext value={value}>{children}</TimeRangeContext>
}

/**
 * The shared range narrowed to the options a view supports, and a setter
 * that moves the shared range for every view. Shaped like `useState` so a
 * view swaps its local state for this without other changes.
 */
export function useTimeRange<T extends TimeRange>(options: readonly T[]): [T, (range: T) => void] {
  const context = useContext(TimeRangeContext)
  if (context === undefined) throw new Error('useTimeRange must be used within a TimeRangeProvider')
  const resolved = useMemo(() => resolveTimeRange(context.range, options), [context.range, options])
  // The address carries the shared range, not this view's narrowed one, so a link opens every view as it was. It is
  // written again when a navigation on the same page (its sidebar entry) leaves the address without it.
  const setUrl = useSetUrlParams()
  const inAddress = new URLSearchParams(useLocation().search).get('range')
  useEffect(() => { setUrl({ range: context.range }) }, [context.range, inAddress, setUrl])
  return [resolved, context.setRange]
}
