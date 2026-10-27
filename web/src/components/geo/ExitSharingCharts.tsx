// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  CartesianGrid, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, useChartWidth, usePlotArea, useXAxisScale, useYAxisScale,
  type ScatterShapeProps, type TooltipContentProps,
} from 'recharts'
import { useTheme } from '../../contexts/ThemeContext'
import { cn } from '../../utils/cn'
import { elideMiddle } from '../../utils/format'

/** What the charts need of a connector: its name, exits seen in the window and how many another connector also had. */
export interface ChartConnector {
  connector_id: string
  name: string
  exits: number
  reused: number
  shared: number
}

// Colour-blind-checked categorical slots, in a fixed order, stepped for each
// theme. Past the last slot connectors go grey rather than reuse a colour;
// every chart labels them by name as well.
const LIGHT_SLOTS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948']
const DARK_SLOTS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']
const OVERFLOW = '#8a8f98'

/** A colour per connector, assigned in name order so a connector keeps its colour when the ranking changes. */
export function useConnectorColors(connectors: ChartConnector[]): Map<string, string> {
  const { isDark } = useTheme()
  return useMemo(() => {
    const slots = isDark ? DARK_SLOTS : LIGHT_SLOTS
    const byName = [...connectors].sort((a, b) => a.name.localeCompare(b.name))
    return new Map(byName.map((c, i) => [c.connector_id, slots[i] ?? OVERFLOW]))
  }, [connectors, isDark])
}

/** `part` as a percentage of `whole`, to one decimal. */
export function percent(part: number, whole: number): number {
  return whole > 0 ? Math.round((part / whole) * 1000) / 10 : 0
}

/** A round axis end a little above `max`: 1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6 or 8 times a power of ten. */
function niceCeil(max: number): number {
  if (max <= 0) return 1
  const raw = max * 1.08
  const power = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find((m) => m * power >= raw) ?? 10
  return step * power
}

const thousands = (v: number) => (v >= 1000 ? `${Number((v / 1000).toFixed(1))}k` : String(v))

interface Point {
  id: string
  name: string
  exits: number
  only: number
  ownCount: number
  reusedPct: number
  color: string
}

/**
 * Each connector as a dot: across, the exits it handed out in the window; up,
 * the share no other connector had. Top right is a large pool of its own.
 */
export function UniquenessScatter({ connectors, colors }: { connectors: ChartConnector[]; colors: Map<string, string> }) {
  const { isDark } = useTheme()
  const gridColor = isDark ? '#374151' : '#e5e7eb'
  const tickColor = '#9ca3af'
  const xMax = niceCeil(Math.max(...connectors.map((c) => c.exits), 1))
  const points: Point[] = useMemo(() => {
    return connectors.map((c) => ({
      id: c.connector_id,
      name: c.name,
      exits: c.exits,
      only: percent(c.exits - c.shared, c.exits),
      ownCount: c.exits - c.shared,
      reusedPct: percent(c.reused, c.exits),
      color: colors.get(c.connector_id) ?? OVERFLOW,
    }))
  }, [connectors, colors])

  return (
    <ResponsiveContainer width="100%" height={260}>
      <ScatterChart margin={{ top: 12, right: 16, bottom: 0, left: -12 }}>
        <CartesianGrid strokeDasharray="2 4" stroke={gridColor} />
        <XAxis
          type="number" dataKey="exits" name="Exits seen" domain={[0, xMax]} tickCount={4} allowDecimals={false}
          tickFormatter={thousands} tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false}
        />
        <YAxis
          type="number" dataKey="only" name="Only here" domain={[0, 100]} ticks={[0, 25, 50, 75, 100]}
          tickFormatter={(v: number) => `${v}%`} tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false} width={48}
        />
        <Tooltip content={ScatterTip} cursor={false} isAnimationActive={false} />
        <Scatter data={points} shape={ScatterDot} isAnimationActive={false} />
        <ScatterLabels points={points} />
      </ScatterChart>
    </ResponsiveContainer>
  )
}

function ScatterDot(props: ScatterShapeProps) {
  const { cx, cy } = props
  const p = props.payload as Point | undefined
  if (cx == null || cy == null || !p) return <g />
  return (
    <g>
      {/* A larger invisible target, so a touch lands on the dot. */}
      <circle cx={cx} cy={cy} r={16} fill="transparent" />
      <circle cx={cx} cy={cy} r={DOT_R} fill={p.color} style={{ stroke: 'var(--color-surface)' }} strokeWidth={2} />
    </g>
  )
}

const DOT_R = 6
// 11px text, from the top of its tallest letters to the bottom of its descenders, around the baseline.
const ABOVE_BASELINE = 10
const BELOW_BASELINE = 3
const LEADER_STEP = 15
// What a spot outside the chart costs: more than any overlap inside it.
const OUTSIDE = 1e9

type Anchor = 'start' | 'middle' | 'end'
interface Box { x0: number; y0: number; x1: number; y1: number }
interface Spot { dx: number; dy: number; anchor: Anchor; leader?: boolean }

// Beside, above and below the dot first; then further up or down on either side, joined to the dot by a line.
const NEAR_SPOTS: Spot[] = [
  { dx: 11, dy: 4, anchor: 'start' }, { dx: -11, dy: 4, anchor: 'end' },
  { dx: 0, dy: -11, anchor: 'middle' }, { dx: 0, dy: 19, anchor: 'middle' },
  { dx: 9, dy: -8, anchor: 'start' }, { dx: 9, dy: 16, anchor: 'start' },
  { dx: -9, dy: -8, anchor: 'end' }, { dx: -9, dy: 16, anchor: 'end' },
]
const SPOTS: Spot[] = [
  ...NEAR_SPOTS,
  ...[1, 2, 3, 4, 5, 6].flatMap((k) => [1, -1].flatMap((side) => [-1, 1].map((up) => ({
    dx: 20 * side, dy: 4 + up * k * LEADER_STEP, anchor: (side > 0 ? 'start' : 'end') as Anchor, leader: true,
  })))),
]

interface PlacedLabel { id: string; text: string; x: number; y: number; anchor: Anchor; leader: [number, number, number, number] | null }

function textBox(x: number, baseline: number, width: number, anchor: Anchor): Box {
  const x0 = anchor === 'start' ? x : anchor === 'end' ? x - width : x - width / 2
  return { x0: x0 - 2, y0: baseline - ABOVE_BASELINE, x1: x0 + width + 2, y1: baseline + BELOW_BASELINE }
}

const overlap = (a: Box, b: Box) =>
  Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0))

// How far a name is cut, longest first, when it fits nowhere inside the chart at full length.
const LABEL_LENGTHS = [24, 16, 12]

/**
 * Where each dot's name goes: the first spot around it that stays inside `bounds` and clear of every dot and every
 * label placed so far, larger connectors first; with no clear spot, the one that overlaps least. A name that fits
 * nowhere inside is cut shorter first.
 */
function placeLabels(
  dots: { id: string; name: string; cx: number; cy: number; exits: number }[],
  bounds: Box,
  widthOf: (text: string) => number,
): PlacedLabel[] {
  const taken: Box[] = dots.map((d) => ({ x0: d.cx - DOT_R - 1, y0: d.cy - DOT_R - 1, x1: d.cx + DOT_R + 1, y1: d.cy + DOT_R + 1 }))
  const placed: PlacedLabel[] = []
  for (const d of [...dots].sort((a, b) => b.exits - a.exits)) {
    let best: { spot: Spot; box: Box; text: string } | null = null
    for (const length of LABEL_LENGTHS) {
      // Cut in the middle: connectors often differ only at the end (`-2`, `-us`).
      const text = elideMiddle(d.name, length)
      const width = widthOf(text)
      let bestCost = Infinity
      for (const spot of SPOTS) {
        const box = textBox(d.cx + spot.dx, d.cy + spot.dy, width, spot.anchor)
        const outside = box.x0 < bounds.x0 || box.x1 > bounds.x1 || box.y0 < bounds.y0 || box.y1 > bounds.y1
        const cost = (outside ? OUTSIDE : 0) + taken.reduce((sum, t) => sum + overlap(box, t), 0)
        if (cost < bestCost) {
          best = { spot, box, text }
          bestCost = cost
          if (cost === 0) break
        }
      }
      if (bestCost < OUTSIDE) break
    }
    if (!best) continue
    taken.push(best.box)
    const { spot } = best
    const x = d.cx + spot.dx
    const baseline = d.cy + spot.dy
    const side = spot.anchor === 'end' ? -1 : 1
    placed.push({
      id: d.id, text: best.text, x, y: baseline, anchor: spot.anchor,
      leader: spot.leader ? [d.cx + side * (DOT_R + 1), d.cy, x - side * 3, baseline - 4] : null,
    })
  }
  return placed
}

let measureContext: CanvasRenderingContext2D | null | undefined
/** How wide `text` renders at 11px in the page's font; an estimate where the browser cannot measure. */
function labelWidth(text: string): number {
  if (measureContext === undefined) {
    measureContext = document.createElement('canvas').getContext('2d')
    if (measureContext) measureContext.font = `11px ${getComputedStyle(document.body).fontFamily}`
  }
  return measureContext ? measureContext.measureText(text).width : text.length * 6.2
}

/** The dots' names, placed together so none covers another name or a dot. Drawn inside the chart, from its scales. */
function ScatterLabels({ points }: { points: Point[] }) {
  const xScale = useXAxisScale()
  const yScale = useYAxisScale()
  const plot = usePlotArea()
  const chartWidth = useChartWidth()
  if (!xScale || !yScale || !plot || !chartWidth) return null
  const dots = points.flatMap((p) => {
    const cx = xScale(p.exits)
    const cy = yScale(p.only)
    return cx == null || cy == null ? [] : [{ id: p.id, name: p.name, cx, cy, exits: p.exits }]
  })
  const bounds = { x0: 0, y0: plot.y - 10, x1: chartWidth, y1: plot.y + plot.height }
  return (
    <g pointerEvents="none">
      {placeLabels(dots, bounds, labelWidth).map((l) => (
        <g key={l.id}>
          {l.leader && <line x1={l.leader[0]} y1={l.leader[1]} x2={l.leader[2]} y2={l.leader[3]} strokeWidth={1} style={{ stroke: 'var(--color-fg-subtle)' }} />}
          <text
            x={l.x} y={l.y} textAnchor={l.anchor} fontSize={11}
            style={{ fill: 'var(--color-fg)', stroke: 'var(--color-surface)', strokeWidth: 3, paintOrder: 'stroke', strokeLinejoin: 'round' }}
          >
            {l.text}
          </text>
        </g>
      ))}
    </g>
  )
}

function ScatterTip({ active, payload }: TooltipContentProps) {
  const p = active ? (payload?.[0]?.payload as Point | undefined) : undefined
  if (!p) return null
  return (
    <div className="rounded-lg border border-line bg-surface px-2.5 py-1.5 text-xs shadow-md max-w-64">
      <div className="font-medium text-fg flex items-center gap-1.5">
        <span className="w-2 h-2 rounded-full flex-none" style={{ backgroundColor: p.color }} />
        <span className="truncate">{p.name}</span>
      </div>
      <div className="text-fg-muted tabular-nums mt-0.5">{p.exits.toLocaleString()} exits seen</div>
      <div className="text-fg-muted tabular-nums">{p.only}% only here ({p.ownCount.toLocaleString()})</div>
      <div className="text-fg-muted tabular-nums">{p.reusedPct}% reused</div>
    </div>
  )
}

/** The width an element is laid out at, kept current as it resizes. */
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setWidth(el.clientWidth)
    const watch = new ResizeObserver(() => setWidth(el.clientWidth))
    watch.observe(el)
    return () => watch.disconnect()
  }, [])
  return [ref, width] as const
}

type Focus = { kind: 'pair'; a: string; b: string } | { kind: 'connector'; id: string }

interface Group {
  c: ChartConnector
  start: number
  end: number
  /** Where each other connector's ribbon meets this arc, and where the part no other connector had sits. */
  parts: Map<string, [number, number]>
}

// Below this width the names go in the legend only, so the circle can use the room.
const LABELLED_MIN_WIDTH = 400
const LABEL_ROOM = 104
const PAD_ANGLE = 0.04
const RING = 12

const point = (angle: number, radius: number) => [radius * Math.sin(angle), -radius * Math.cos(angle)] as const

function arcPath(a0: number, a1: number, inner: number, outer: number): string {
  const large = a1 - a0 > Math.PI ? 1 : 0
  const [ox0, oy0] = point(a0, outer)
  const [ox1, oy1] = point(a1, outer)
  const [ix1, iy1] = point(a1, inner)
  const [ix0, iy0] = point(a0, inner)
  return `M${ox0},${oy0}A${outer},${outer} 0 ${large} 1 ${ox1},${oy1}L${ix1},${iy1}A${inner},${inner} 0 ${large} 0 ${ix0},${iy0}Z`
}

function ribbonPath([s0, s1]: [number, number], [t0, t1]: [number, number], r: number): string {
  const [sx0, sy0] = point(s0, r)
  const [sx1, sy1] = point(s1, r)
  const [tx0, ty0] = point(t0, r)
  const [tx1, ty1] = point(t1, r)
  const sLarge = s1 - s0 > Math.PI ? 1 : 0
  const tLarge = t1 - t0 > Math.PI ? 1 : 0
  return `M${sx0},${sy0}A${r},${r} 0 ${sLarge} 1 ${sx1},${sy1}Q0,0 ${tx0},${ty0}A${r},${r} 0 ${tLarge} 1 ${tx1},${ty1}Q0,0 ${sx0},${sy0}Z`
}

/**
 * Connectors around a circle, an arc's length its exits seen in the window,
 * and a ribbon between two connectors as wide as the exits both handed out.
 * Hovering or tapping a ribbon or an arc names its numbers under the chart.
 */
export function SharingChord({ connectors, sharedOf, colors }: {
  connectors: ChartConnector[]
  /** Exits two connectors both handed out in the window. */
  sharedOf: (a: string, b: string) => number
  colors: Map<string, string>
}) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const labelled = width >= LABELLED_MIN_WIDTH
  const size = Math.max(0, Math.min(width, labelled ? 460 : 300))
  const outer = Math.max(40, (labelled ? size - 2 * LABEL_ROOM : size - 8) / 2)
  const inner = outer - RING
  // Room above and below for a name at the top or bottom: 8 px out from the ring, then the text's own height.
  const height = 2 * outer + (labelled ? 48 : 8)

  // Each arc holds the part no other connector had, then a part per connector it shares with, largest first. An IP
  // on three connectors sits in two ribbons, so arcs can run a little longer than the exits they stand for.
  const { groups, pairs } = useMemo(() => {
    const rows = connectors.map((c) => {
      const others = connectors
        .filter((o) => o.connector_id !== c.connector_id)
        .map((o) => ({ id: o.connector_id, n: sharedOf(c.connector_id, o.connector_id) }))
        .filter((o) => o.n > 0)
        .sort((x, y) => y.n - x.n)
      return { c, own: Math.max(c.exits - c.shared, 0), others }
    })
    const total = rows.reduce((sum, r) => sum + r.own + r.others.reduce((s, o) => s + o.n, 0), 0)
    const k = total > 0 ? (2 * Math.PI - PAD_ANGLE * rows.length) / total : 0
    let angle = 0
    const take = (n: number): [number, number] => {
      const from = angle
      angle += n * k
      return [from, angle]
    }
    const groups: Group[] = rows.map((r) => {
      const start = angle
      const parts = new Map<string, [number, number]>([[r.c.connector_id, take(r.own)]])
      for (const o of r.others) parts.set(o.id, take(o.n))
      const group = { c: r.c, start, end: angle, parts }
      angle += PAD_ANGLE
      return group
    })
    const pairs: { a: Group; b: Group; n: number }[] = []
    groups.forEach((a, i) => groups.slice(i + 1).forEach((b) => {
      const n = sharedOf(a.c.connector_id, b.c.connector_id)
      if (n > 0) pairs.push({ a, b, n })
    }))
    pairs.sort((x, y) => y.n - x.n)
    return { groups, pairs }
  }, [connectors, sharedOf])

  const [focus, setFocus] = useState<Focus | null>(null)
  // A refresh or another window can drop the focused connector; the default takes over then.
  const known = (id: string) => groups.some((g) => g.c.connector_id === id)
  const current = focus && (focus.kind === 'connector' ? known(focus.id) : known(focus.a) && known(focus.b)) ? focus : null
  const shown: Focus | null = current ?? (pairs[0] ? { kind: 'pair', a: pairs[0].a.c.connector_id, b: pairs[0].b.c.connector_id } : null)
  const isFocused = (a: string, b: string) => shown?.kind === 'pair' && ((shown.a === a && shown.b === b) || (shown.a === b && shown.b === a))
  const touches = (id: string) => shown?.kind === 'connector' && shown.id === id

  return (
    <div className="space-y-3">
      <div ref={ref} className="w-full">
        {width > 0 && (
          <svg width={width} height={height} role="img" aria-label="Chord diagram of the exits connectors share" onPointerLeave={() => setFocus(null)}>
            <g transform={`translate(${width / 2},${height / 2})`}>
              {pairs.map(({ a, b, n }) => {
                const larger = a.c.exits >= b.c.exits ? a : b
                const lit = isFocused(a.c.connector_id, b.c.connector_id) || touches(a.c.connector_id) || touches(b.c.connector_id)
                return (
                  <path
                    key={`${a.c.connector_id} ${b.c.connector_id}`}
                    d={ribbonPath(a.parts.get(b.c.connector_id)!, b.parts.get(a.c.connector_id)!, inner - 1)}
                    fill={colors.get(larger.c.connector_id)}
                    fillOpacity={shown ? (lit ? 0.8 : 0.2) : 0.55}
                    style={{ stroke: 'var(--color-surface)', cursor: 'pointer' }}
                    strokeWidth={1}
                    onPointerEnter={(e) => e.pointerType === 'mouse' && setFocus({ kind: 'pair', a: a.c.connector_id, b: b.c.connector_id })}
                    onClick={() => setFocus({ kind: 'pair', a: a.c.connector_id, b: b.c.connector_id })}
                  >
                    <title>{`${n.toLocaleString()} exits shared by ${a.c.name} and ${b.c.name}`}</title>
                  </path>
                )
              })}
              {groups.map((g) => {
                const mid = (g.start + g.end) / 2
                const [lx, ly] = point(mid, outer + 8)
                const anchor = Math.abs(lx) < 12 ? 'middle' : lx > 0 ? 'start' : 'end'
                return (
                  <g
                    key={g.c.connector_id}
                    style={{ cursor: 'pointer' }}
                    onPointerEnter={(e) => e.pointerType === 'mouse' && setFocus({ kind: 'connector', id: g.c.connector_id })}
                    onClick={() => setFocus({ kind: 'connector', id: g.c.connector_id })}
                  >
                    {/* The ring is thin; a wider invisible band takes the touch. */}
                    <path d={arcPath(g.start, g.end, inner - 6, outer + 6)} fill="transparent" />
                    <path d={arcPath(g.start, g.end, inner, outer)} fill={colors.get(g.c.connector_id)} />
                    {labelled && g.end - g.start > 0.12 && (
                      <text x={lx} y={ly + (ly > 0 ? 11 : -3)} textAnchor={anchor} fontSize={11} style={{ fill: 'var(--color-fg)' }}>
                        {elideMiddle(g.c.name, 16)}
                      </text>
                    )}
                    <title>{`${g.c.name}: ${g.c.exits.toLocaleString()} exits seen`}</title>
                  </g>
                )
              })}
            </g>
          </svg>
        )}
      </div>
      <ChordReadout focus={shown} connectors={connectors} sharedOf={sharedOf} />
      <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-fg-muted">
        {connectors.map((c) => (
          <button
            key={c.connector_id}
            type="button"
            onClick={() => setFocus({ kind: 'connector', id: c.connector_id })}
            className={cn('inline-flex items-center gap-1.5 min-w-0 max-w-full pointer-coarse:min-h-11 hover:text-fg', touches(c.connector_id) && 'text-fg')}
          >
            <span className="w-2.5 h-2.5 rounded-sm flex-none" style={{ backgroundColor: colors.get(c.connector_id) }} />
            <span className="truncate">{c.name}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function ChordReadout({ focus, connectors, sharedOf }: {
  focus: Focus | null
  connectors: ChartConnector[]
  sharedOf: (a: string, b: string) => number
}) {
  const byId = new Map(connectors.map((c) => [c.connector_id, c]))
  let text: string
  if (!focus) {
    text = 'No exit was handed out by two connectors in this window.'
  } else if (focus.kind === 'pair') {
    const a = byId.get(focus.a)!
    const b = byId.get(focus.b)!
    const n = sharedOf(a.connector_id, b.connector_id)
    text = `${a.name} and ${b.name} share ${n.toLocaleString()} exits: ${percent(n, a.exits)}% of ${a.name}'s and ${percent(n, b.exits)}% of ${b.name}'s.`
  } else {
    const c = byId.get(focus.id)!
    const partners = connectors
      .filter((o) => o.connector_id !== c.connector_id)
      .map((o) => ({ name: o.name, n: sharedOf(c.connector_id, o.connector_id) }))
      .filter((o) => o.n > 0)
      .sort((x, y) => y.n - x.n)
    const sharing = partners.length
      ? `shares ${partners.map((o) => `${o.n.toLocaleString()} with ${o.name}`).join(', ')}`
      : 'shares none'
    text = `${c.name}: ${c.exits.toLocaleString()} exits seen, ${(c.exits - c.shared).toLocaleString()} only here; ${sharing}.`
  }
  return <p className="text-xs text-fg-muted min-h-8" aria-live="polite">{text}</p>
}
