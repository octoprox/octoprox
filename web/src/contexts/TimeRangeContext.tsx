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
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState, ReactNode } from 'react'

export const TIME_RANGES = ['1h', '6h', '24h', '7d', '30d', '90d'] as const
export type TimeRange = (typeof TIME_RANGES)[number]

const HOUR = 3_600_000
export const TIME_RANGE_MS: Record<TimeRange, number> = {
  '1h': HOUR, '6h': 6 * HOUR, '24h': 24 * HOUR, '7d': 7 * 24 * HOUR, '30d': 30 * 24 * HOUR, '90d': 90 * 24 * HOUR,
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

export function TimeRangeProvider({ children }: { children: ReactNode }) {
  const [range, setRangeState] = useState<TimeRange>(readStored)

  const setRange = useCallback((next: TimeRange) => {
    setRangeState(next)
    try { localStorage.setItem(STORAGE_KEY, next) } catch { /* ignore */ }
  }, [])

  // Another tab changing the range changes it here too.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && isTimeRange(e.newValue)) setRangeState(e.newValue)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const value = useMemo(() => ({ range, setRange }), [range, setRange])
  return <TimeRangeContext.Provider value={value}>{children}</TimeRangeContext.Provider>
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
  return [resolved, context.setRange]
}
