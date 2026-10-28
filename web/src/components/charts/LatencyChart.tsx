// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Latency over time, stacked by the step it was spent in: routing and preflight
 * are Octoprox's part, connect, handshake and response the vendor's. Shared by
 * the Overview and the connector inspector.
 */

import { useMemo } from 'react'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { HISTORY_GAP_MS, HistoryRange, LatencyStep, MetricsSnapshot } from '../../api/client'
import { formatRangeTick } from '../../contexts/TimeRangeContext'
import { useTheme } from '../../contexts/ThemeContext'
import { formatMs, parseApiDate } from '../../utils/format'

type Band = LatencyStep | 'unsplit'

type BandStyle = { key: Band; label: string; hint: string; light: string; dark: string }

/** The steps in stacking order, bottom first, in the chart palette's first five slots (light, dark). */
const STEPS: (BandStyle & { key: LatencyStep })[] = [
  { key: 'route', label: 'Routing', hint: 'choosing the proxy', light: '#2a78d6', dark: '#3987e5' },
  { key: 'preflight', label: 'Preflight', hint: 'checking where it exits', light: '#eb6834', dark: '#d95926' },
  { key: 'connect', label: 'Connect', hint: 'reaching the vendor', light: '#1baf7a', dark: '#199e70' },
  { key: 'handshake', label: 'Handshake', hint: 'the vendor reaching the target', light: '#eda100', dark: '#c98500' },
  { key: 'response', label: 'Response', hint: 'plain HTTP until its status line', light: '#e87ba4', dark: '#d55181' },
]
/** Each step in order with its name, for pages that list steps without the chart. */
export const LATENCY_STEPS: { key: LatencyStep; label: string; hint: string }[] = STEPS.map(({ key, label, hint }) => ({ key, label, hint }))

// History from before steps were recorded: its latency, in one band.
const UNSPLIT: BandStyle = { key: 'unsplit', label: 'Not split', hint: 'recorded before steps were', light: '#a3a3a3', dark: '#6b7280' }

/** One chart point: each band as its [bottom, top] in ms, null where the point has none, so its area breaks there. */
type Point = { time: number; total: number | null; steps: Partial<Record<LatencyStep, number>> | null } & Partial<Record<Band, [number, number] | null>>

const EMPTY_BANDS: Record<Band, null> = { route: null, preflight: null, connect: null, handshake: null, response: null, unsplit: null }

function toPoints(snapshots: MetricsSnapshot[], range: HistoryRange): Point[] {
  const points: Point[] = []
  let previous: number | null = null
  for (const s of snapshots) {
    // Naive UTC from the API; a plain Date() would read it as local time.
    const time = parseApiDate(s.timestamp)?.getTime() ?? 0
    if (previous != null && time - previous > HISTORY_GAP_MS[range]) {
      points.push({ time: previous + 1, total: null, steps: null, ...EMPTY_BANDS })
    }
    previous = time
    const point: Point = { time, total: s.avg_latency_ms, steps: s.step_latency_ms, ...EMPTY_BANDS }
    if (s.avg_latency_ms != null && s.step_latency_ms) {
      let bottom = 0
      for (const step of STEPS) {
        const top = bottom + (s.step_latency_ms[step.key] ?? 0)
        point[step.key] = [bottom, top]
        bottom = top
      }
    } else if (s.avg_latency_ms != null) {
      point.unsplit = [0, s.avg_latency_ms]
    }
    points.push(point)
  }
  return points
}

/** Each step's average over the range's successful requests, and whether any of the range was recorded unsplit. */
function rangeAverages(snapshots: MetricsSnapshot[]) {
  const sums: Record<LatencyStep, number> = { route: 0, preflight: 0, connect: 0, handshake: 0, response: 0 }
  let successes = 0
  let unsplit = false
  for (const s of snapshots) {
    if (s.avg_latency_ms == null) continue
    if (!s.step_latency_ms) { unsplit = true; continue }
    successes += s.success_count
    for (const step of STEPS) sums[step.key] += (s.step_latency_ms[step.key] ?? 0) * s.success_count
  }
  const averages = successes > 0 ? STEPS.map((step) => ({ ...step, ms: sums[step.key] / successes })) : []
  return { averages, unsplit }
}

export function LatencyChart({ snapshots, range, height, domain, compact }: {
  snapshots: MetricsSnapshot[]
  range: HistoryRange
  height: number
  /** The time axis; the data's own extent when not given. */
  domain?: [number, number]
  /** Smaller ticks, for a narrow card or an inspector. */
  compact?: boolean
}) {
  const { isDark } = useTheme()
  const points = useMemo(() => toPoints(snapshots, range), [snapshots, range])
  const { averages, unsplit } = useMemo(() => rangeAverages(snapshots), [snapshots])
  const color = (band: BandStyle) => (isDark ? band.dark : band.light)
  const gridColor = isDark ? '#374151' : '#e5e7eb'
  const tick = { fontSize: compact ? 10 : 11, fill: '#9ca3af' }
  // Only the steps the range has, so a project without preflight lists none.
  const legend = averages.filter((step) => step.ms > 0)

  if (points.length === 0) {
    return <p className="text-fg-subtle text-xs text-center flex items-center justify-center" style={{ height }}>No data</p>
  }
  return (
    <div>
      <ResponsiveContainer width="100%" height={height}>
        <AreaChart data={points} margin={{ top: 6, right: 4, left: -20, bottom: 0 }} aria-label="Latency over time, by step">
          <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
          <XAxis dataKey="time" type="number" scale="time" domain={domain ?? ['dataMin', 'dataMax']} tickFormatter={(v: number) => formatRangeTick(v, range)} tick={tick} axisLine={false} tickLine={false} minTickGap={48} />
          <YAxis tick={tick} axisLine={false} tickLine={false} tickFormatter={(v: number) => `${Math.round(v)}`} />
          <Tooltip content={({ active, payload }) => <LatencyTooltip active={active} point={payload?.[0]?.payload as Point | undefined} color={color} />} />
          {/* A surface-coloured edge on each band keeps a gap between neighbours. */}
          {[UNSPLIT, ...STEPS].map((band) => (
            <Area
              key={band.key} dataKey={band.key} name={band.label} connectNulls={false} type="monotone" isAnimationActive={false}
              stroke="var(--color-surface)" strokeWidth={1} fill={color(band)} fillOpacity={0.85} activeDot={false}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
      {(legend.length > 0 || unsplit) && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1.5 text-[11px] text-fg-muted tabular-nums">
          {legend.map((step) => (
            <span key={step.key} className="inline-flex items-center gap-1.5" title={`${step.label}: ${step.hint}, averaged over the successful requests of the range`}>
              <span className="inline-block w-2 h-2 rounded-xs" style={{ background: color(step) }} />
              {step.label} <span className="text-fg">{formatMs(step.ms)}</span>
            </span>
          ))}
          {unsplit && (
            <span className="inline-flex items-center gap-1.5" title={`Latency ${UNSPLIT.hint}`}>
              <span className="inline-block w-2 h-2 rounded-xs" style={{ background: color(UNSPLIT) }} />
              {UNSPLIT.label}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function LatencyTooltip({ active, point, color }: { active?: boolean; point?: Point; color: (band: BandStyle) => string }) {
  if (!active || !point || point.total == null) return null
  const steps = point.steps
  return (
    <div className="rounded-lg border border-line bg-surface px-2.5 py-2 text-xs shadow-md min-w-[170px]">
      <div className="text-fg-muted mb-1">{new Date(point.time).toLocaleString()}</div>
      {steps ? (
        // Top band first, as the stack reads.
        [...STEPS].reverse().filter((step) => (steps[step.key] ?? 0) > 0).map((step) => (
          <div key={step.key} className="flex items-center gap-2 tabular-nums">
            <span className="inline-block w-2 h-2 rounded-xs flex-none" style={{ background: color(step) }} />
            <span className="flex-1 text-fg-muted">{step.label}</span>
            <span className="text-fg">{formatMs(steps[step.key] ?? 0)}</span>
          </div>
        ))
      ) : (
        <div className="text-fg-subtle">Not split into steps</div>
      )}
      <div className="flex items-center gap-2 tabular-nums border-t border-line mt-1 pt-1">
        <span className="flex-1 text-fg-muted">Latency</span>
        <span className="text-fg font-medium">{formatMs(point.total)}</span>
      </div>
    </div>
  )
}
