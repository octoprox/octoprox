// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Each connector's factor at a place over time, in the place inspector: a dynamic one's own factor, or the value routing
 * weighed it by under the Strength and Lowest factor in force at each moment; a connector with IPs, the share of them
 * usable for the site, the same in both.
 */

import { useMemo } from 'react'
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis, usePlotArea, useXAxisScale } from 'recharts'
import type { PlaceHistory, PlaceHistoryPoint, PlaceHistoryRange, PlaceHistoryRule, PlaceHistorySeries } from '../../api/client'
import { formatRangeTick, rangeTicks } from '../../contexts/TimeRangeContext'
import { useTheme } from '../../contexts/ThemeContext'
import { useWidth } from '../../hooks/useWidth'
import { formatClock, parseApiDate } from '../../utils/format'
import { factorText, levelName } from '../ConnectorReputation'
import { LogoLoader } from '../ui'

/** Which value a line draws: the connector's own factor, or the one routing weighs it by. */
export type HistoryView = 'own' | 'routed'

/**
 * Recharts reads a dataKey as a lodash path, so a connector id could be read as nested fields. Series are keyed by
 * position instead; the connector travels as the series name.
 */
const seriesKey = (index: number) => `s${index}`

/** A point of a series with its time as a number. */
type Timed = PlaceHistoryPoint & { time: number }

/**
 * One moment of the chart: each series' value from then (null where it has none, so its line breaks), and what the
 * tooltip reads. A hold row repeats the moment before it just ahead of the next, so a step ends where a gap starts.
 */
type Row = { time: number; from: number; floor: number | null; rule: PlaceHistoryRule | null; points: (Timed | null)[] } & Record<string, number | null | unknown>

const time = (at: string) => parseApiDate(at)?.getTime() ?? 0
const valueOf = (point: Timed | null, view: HistoryView) => (point == null ? null : view === 'own' ? point.own ?? null : point.routed)

/** Walks a list sorted by time forward: the last item at or before each ``t`` asked, the ``t``s asked in order. */
function cursor<T extends { time: number }>(items: T[]): (t: number) => T | null {
  let next = 0
  return (t) => {
    while (next < items.length && items[next].time <= t) next++
    return next > 0 ? items[next - 1] : null
  }
}

/**
 * The rows from ``since`` to the window's end: a row at every moment a series has a point, each series' value carried,
 * with the Lowest factor when ``ruled``. Before ``start`` (the history's) a blank row at each end, so the pointer there
 * finds no moment.
 */
function buildRows(history: PlaceHistory, view: HistoryView, ruled: boolean, since: number, start: number, steps: boolean): Row[] {
  const until = time(history.until)
  const series: Timed[][] = history.series.map((s) => s.points.map((p) => ({ ...p, time: time(p.at) })).sort((a, b) => a.time - b.time))
  const rules = history.rules.map((r) => ({ ...r, time: time(r.at) })).sort((a, b) => a.time - b.time)
  const moments = new Set([start, until])
  for (const points of series) for (const p of points) if (p.time > start && p.time < until) moments.add(p.time)
  // A rule change moves the Lowest factor line, so it is a moment too.
  if (ruled) for (const r of rules) if (r.time > start && r.time < until) moments.add(r.time)
  const pointAt = series.map((points) => cursor(points))
  const ruleAt = cursor(rules)
  const rows: Row[] = []
  if (start > since) {
    for (const t of [since, start - 1]) {
      const row: Row = { time: t, from: t, floor: null, rule: null, points: series.map(() => null) }
      series.forEach((_, i) => { row[seriesKey(i)] = null })
      rows.push(row)
    }
  }
  for (const t of [...moments].sort((a, b) => a - b)) {
    // The rule in force at t: the last one from at or before it, else the first.
    const rule = ruleAt(t) ?? rules[0] ?? null
    const row: Row = { time: t, from: t, floor: ruled && rule ? rule.floor : null, rule, points: pointAt.map((at) => at(t)) }
    row.points.forEach((p, i) => { row[seriesKey(i)] = valueOf(p, view) })
    const previous = rows[rows.length - 1]
    // A step that runs into a gap: without the hold row the line would stop at the step's start.
    if (steps && previous && row.points.some((p, i) => valueOf(p, view) == null && valueOf(previous.points[i], view) != null)) {
      rows.push({ ...previous, time: t - 1 })
    }
    rows.push(row)
  }
  return rows
}

/** The axis's lower end: the lowest value drawn rounded down to 0.05, at most ×0.95 so a flat ×1 has room. */
function lowerBound(rows: Row[], count: number): number {
  let lowest = 1
  for (const row of rows) {
    for (let i = 0; i < count; i++) {
      const v = row[seriesKey(i)]
      if (typeof v === 'number') lowest = Math.min(lowest, v)
    }
    if (row.floor != null) lowest = Math.min(lowest, row.floor)
  }
  return Math.max(0, Math.min(0.95, Math.floor(lowest * 20 + 1e-9) / 20))
}

/** Round ticks from ``lower`` to 1, at most six. */
function factorTicks(lower: number): number[] {
  const span = 1 - lower
  const step = [0.05, 0.1, 0.2, 0.25, 0.5].find((s) => span / s <= 5) ?? 0.5
  const ticks: number[] = []
  for (let v = 1; v >= lower - 1e-9; v -= step) ticks.push(Math.round(v * 100) / 100)
  return ticks.reverse()
}

/** What changed from one rule to the next: "Strength 10", "Lowest factor ×0.2", "Avoid on". */
function ruleChange(previous: PlaceHistoryRule, rule: PlaceHistoryRule): string {
  const parts: string[] = []
  if (rule.applied !== previous.applied) parts.push(rule.applied ? 'Avoid on' : 'Avoid off')
  if (rule.strength !== previous.strength) parts.push(`Strength ${rule.strength}`)
  if (rule.floor !== previous.floor) parts.push(`Lowest factor ${factorText(rule.floor)}`)
  return parts.join(', ')
}

/** Whether any connector's own value moved in the window; a routed value that moved alone is a rule change. */
function anyMoved(history: PlaceHistory): boolean {
  return history.series.some((s) => s.points.some((p, k) => {
    const before = s.points[k - 1]
    return k > 0 && ((p.own ?? null) !== (before.own ?? null) || (p.level ?? null) !== (before.level ?? null))
  }))
}

type Marker = { time: number; label: string }
/** A marker's label in place: the row above the plot it sits in (0 the nearest), and which way it reads from the line. */
type PlacedMarker = Marker & { row: number; anchor: 'start' | 'end' }

// The marker labels' font size, and the height of a row of them above the plot.
const MARKER_FONT = 10
const MARKER_ROW = 12
// Where the plot starts and ends across the chart: the left margin plus the y axis's width, and the right margin.
const PLOT_LEFT = -18 + 52
const PLOT_RIGHT = 6

let measureContext: CanvasRenderingContext2D | null | undefined
/** How wide ``text`` renders in a marker label; an estimate where the browser cannot measure. */
function labelWidth(text: string): number {
  if (measureContext === undefined) {
    measureContext = document.createElement('canvas').getContext('2d')
    if (measureContext) measureContext.font = `${MARKER_FONT}px ${getComputedStyle(document.body).fontFamily}`
  }
  return measureContext ? measureContext.measureText(text).width : text.length * 5.6
}

/**
 * The markers' labels in rows above the plot, so none covers another and no line runs through one: each reads away
 * from the nearer edge, in the nearest row where it clears the labels already there.
 */
function placeMarkers(markers: Marker[], since: number, until: number, width: number): PlacedMarker[] {
  const plot = Math.max(1, width - PLOT_LEFT - PLOT_RIGHT)
  const ends: number[] = []
  return markers.map((m) => {
    const x = PLOT_LEFT + ((m.time - since) / Math.max(1, until - since)) * plot
    const anchor = m.time > (since + until) / 2 ? 'end' as const : 'start' as const
    const w = labelWidth(m.label)
    const [from, to] = anchor === 'start' ? [x, x + w] : [x - w, x]
    let row = ends.findIndex((end) => from > end + 6)
    if (row < 0) row = ends.length
    ends[row] = to
    return { ...m, row, anchor }
  })
}

/** The markers' labels, drawn above the plot from the chart's own scale. */
function MarkerLabels({ markers, color }: { markers: PlacedMarker[]; color: string }) {
  const xScale = useXAxisScale()
  const plot = usePlotArea()
  if (!xScale || !plot) return null
  return (
    <g pointerEvents="none">
      {markers.map((m) => {
        const x = xScale(m.time)
        if (x == null) return null
        return (
          <text key={m.time} x={x + (m.anchor === 'start' ? 3 : -3)} y={plot.y - 3 - m.row * MARKER_ROW} textAnchor={m.anchor} fontSize={MARKER_FONT} style={{ fill: color }}>
            {m.label}
          </text>
        )
      })}
    </g>
  )
}

type ChartProps = {
  history: PlaceHistory | undefined
  view: HistoryView
  range: PlaceHistoryRange
  /** A connector's colour, matching its swatch in the panel. */
  colorOf: (connectorId: string) => string
  height?: number
  loading?: boolean
}

export function PlaceHistoryChart(props: ChartProps) {
  // Measured so the marker labels can be placed before the chart draws them.
  const [ref, width] = useWidth()
  return <div ref={ref}><HistoryPlot {...props} width={width} /></div>
}

function HistoryPlot({ history, view, range, colorOf, height = 150, loading, width }: ChartProps & { width: number }) {
  const { isDark } = useTheme()
  const since = history ? time(history.since) : 0
  const until = history ? time(history.until) : 0
  const recorded = history?.recorded_since ? time(history.recorded_since) : null
  // Nothing is drawn before the first move was recorded.
  const start = recorded != null && recorded > since ? Math.min(recorded, until) : since
  // Means per bucket (an hour over 7 days, 3 hours over 30) are drawn as lines; every move up to 24 h, as steps.
  const bucketed = history?.bucket_seconds != null
  const dynamic = history?.series.some((s) => s.kind === 'block_rate') ?? false
  const withIps = history?.series.some((s) => s.kind === 'usable_ips') ?? false
  // Strength and Lowest factor weigh dynamic connectors alone: no floor or rule markers without one.
  const ruled = view === 'routed' && dynamic
  const rows = useMemo(() => (history ? buildRows(history, view, ruled, since, start, !bucketed) : []), [history, view, ruled, since, start, bucketed])
  const count = history?.series.length ?? 0
  const lower = useMemo(() => lowerBound(rows, count), [rows, count])
  const domain: [number, number] = [since, until]
  const ticks = useMemo(() => (history ? rangeTicks(range, [since, until]) : []), [history, range, since, until])
  const markers = useMemo(() => {
    if (!history || !ruled) return []
    const rules = [...history.rules].sort((a, b) => time(a.at) - time(b.at))
    const changes = rules.slice(1).map((rule, i) => ({ time: time(rule.at), label: ruleChange(rules[i], rule) }))
      .filter((m) => m.label && m.time > start && m.time <= until)
    return placeMarkers(changes, since, until, width)
  }, [history, ruled, since, start, until, width])
  const labelRows = markers.reduce((most, m) => Math.max(most, m.row + 1), 0)

  if (loading || !history) {
    return <div className="flex items-center justify-center" style={{ height }}><LogoLoader size="sm" /></div>
  }
  if (history.series.length === 0) {
    return <p className="text-fg-subtle text-xs text-center flex items-center justify-center" style={{ height }}>No connector takes these requests.</p>
  }
  const gridColor = isDark ? '#374151' : '#e5e7eb'
  const tick = { fontSize: 10, fill: '#9ca3af' }
  const floorColor = isDark ? '#9ca3af' : '#6b7280'
  const current = history.rules.length > 0 ? [...history.rules].sort((a, b) => time(a.at) - time(b.at)).pop()! : null
  return (
    <div className="space-y-1.5">
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={rows} margin={{ top: Math.max(14, labelRows * MARKER_ROW + 4), right: PLOT_RIGHT, left: -18, bottom: 0 }} aria-label={view === 'own' ? 'Own factor over time, by connector' : 'Routed value over time, by connector'}>
          <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
          <XAxis dataKey="time" type="number" scale="time" domain={domain} ticks={ticks} tickFormatter={(v: number) => formatRangeTick(v, range)} tick={tick} axisLine={false} tickLine={false} minTickGap={40} />
          <YAxis type="number" domain={[lower, 1]} ticks={factorTicks(lower)} tickFormatter={(v: number) => factorText(v)} tick={tick} axisLine={false} tickLine={false} width={52} />
          {/* Every row reaches the tooltip, one where no connector is judged too. */}
          <Tooltip filterNull={false} content={({ active, payload }) => (
            <HistoryTooltip active={active} row={payload?.[0]?.payload as Row | undefined} history={history} view={view} bucket={bucketWords(history.bucket_seconds)} colorOf={colorOf} />
          )} />
          {markers.map((m) => <ReferenceLine key={m.time} x={m.time} stroke={floorColor} strokeDasharray="2 2" />)}
          <MarkerLabels markers={markers} color={floorColor} />
          {ruled && (
            <Line dataKey="floor" name="Lowest factor" type="stepAfter" stroke={floorColor} strokeWidth={1} strokeDasharray="4 3" dot={false} activeDot={false} isAnimationActive={false} />
          )}
          {history.series.map((s, i) => (
            <Line
              key={s.connector_id} dataKey={seriesKey(i)} name={s.name} type={bucketed ? 'linear' : 'stepAfter'} connectNulls={false}
              stroke={colorOf(s.connector_id)} strokeWidth={1.75} dot={false} activeDot={{ r: 3 }} isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-fg-muted">
        {history.series.map((s) => (
          <span key={s.connector_id} className="inline-flex items-center gap-1.5 min-w-0">
            <span className="inline-block w-2 h-2 rounded-full flex-none" style={{ background: colorOf(s.connector_id) }} aria-hidden />
            <span className="truncate">{s.name}</span>
          </span>
        ))}
        {ruled && current && (
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-block w-3 border-t border-dashed flex-none" style={{ borderColor: floorColor }} aria-hidden />
            Lowest factor {factorText(current.floor)}
          </span>
        )}
      </div>
      {start > since && <p className="text-xs text-fg-subtle">History starts {formatClock(history.recorded_since)}.</p>}
      {/* Before any move is kept the lines are the values now, not a steady range. */}
      {!history.recorded_since
        ? <p className="text-xs text-fg-subtle">History starts with the next {dynamic && withIps ? 'factor move or avoid' : dynamic ? 'factor move' : 'avoid'}.</p>
        : !anyMoved(history) && <p className="text-xs text-fg-subtle">{dynamic && withIps ? 'Nothing moved' : dynamic ? 'No factor moved' : 'No avoid began or ended'} in this range.</p>}
      {view === 'routed' && current && !current.applied && (
        <p className="text-xs text-fg-muted">The project does not avoid now: routing uses the configured weights, and these are what Avoid would weigh by.</p>
      )}
    </div>
  )
}

/** A connector with IPs at a point: "17 of 20 IPs usable", a bucket's mean to a tenth. */
function ipsText(point: PlaceHistoryPoint): string {
  const usable = (point.usable ?? 0).toLocaleString(undefined, { maximumFractionDigits: 1 })
  return `${usable} of ${(point.eligible ?? 0).toLocaleString()} IPs usable`
}

/** A bucket of ``seconds`` in words: "the hour", "those 3 hours"; null for every move. */
function bucketWords(seconds: number | null | undefined): string | null {
  if (seconds == null) return null
  const hours = seconds / 3600
  return hours === 1 ? 'the hour' : `those ${hours.toLocaleString()} hours`
}

/** What the tooltip says of a series at a point: the value drawn, and the line under it. */
function tooltipLines(s: PlaceHistorySeries, p: Timed, row: Row, history: PlaceHistory, view: HistoryView, routedWord: string, bucket: string | null) {
  const routedValue = routedWord === 'Would be' ? `Would be ${factorText(p.routed)}` : factorText(p.routed)
  if (s.kind === 'usable_ips') {
    // Strength and Lowest factor do not touch it: Own and Routed are the same share.
    return { badge: null, value: view === 'own' ? factorText(p.own ?? p.routed) : routedValue, detail: ipsText(p) }
  }
  const own = p.own != null ? factorText(p.own) : 'not judged'
  const inherited = p.level != null && p.level !== history.place && p.own != null
  // Counts belong to the point that holds them: shown at its own moment.
  const counted = p.requests != null && p.time === row.from
    ? `${p.requests.toLocaleString()} requests, ${(p.blocked ?? 0).toLocaleString()} blocked ${bucket ? `in ${bucket}` : 'since the previous move'}`
    : null
  const detail = view === 'own' ? `${routedWord} ${factorText(p.routed)}` : `Own ${own}${inherited ? ` from ${levelName(p.level!)}` : ''}`
  return {
    badge: view === 'own' && inherited ? levelName(p.level!) : null,
    value: view === 'own' ? own : routedValue,
    detail: counted ? `${detail} · ${counted}` : detail,
  }
}

/** The moment under the finger or pointer: each connector's value with what was counted, or its usable IPs. */
function HistoryTooltip({ active, row, history, view, bucket, colorOf }: {
  active?: boolean
  row?: Row
  history: PlaceHistory
  view: HistoryView
  /** The bucket a mean covers in words, null for every move. */
  bucket: string | null
  colorOf: (connectorId: string) => string
}) {
  // A blank row before the history starts.
  if (!active || !row || row.points.every((p) => p == null)) return null
  const when = new Date(row.from).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
  // The mode did not avoid then: what Avoid would have weighed by, as the panel words it.
  const routedWord = row.rule && !row.rule.applied ? 'Would be' : 'Routed'
  return (
    <div className="rounded-lg border border-line bg-surface px-2.5 py-2 text-xs shadow-md min-w-[180px] max-w-[16rem] space-y-1">
      <div className="text-fg-muted">{when}{bucket ? `, mean of ${bucket}` : ''}</div>
      {history.series.map((s, i) => {
        const p = row.points[i]
        if (!p) return null
        const { badge, value, detail } = tooltipLines(s, p, row, history, view, routedWord, bucket)
        return (
          <div key={s.connector_id} className="tabular-nums">
            <div className="flex items-center gap-2">
              <span className="inline-block w-2 h-2 rounded-full flex-none" style={{ background: colorOf(s.connector_id) }} />
              <span className="flex-1 min-w-0 truncate text-fg-muted">{s.name}</span>
              {badge && <span className="text-[10px] leading-none px-1 py-0.5 rounded-sm bg-surface-raised text-fg-subtle">{badge}</span>}
              <span className="text-fg font-medium">{value}</span>
            </div>
            <div className="pl-4 text-[11px] text-fg-subtle">{detail}</div>
          </div>
        )
      })}
    </div>
  )
}
