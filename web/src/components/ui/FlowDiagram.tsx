// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import {
  createContext, useCallback, useContext, useId, useLayoutEffect, useRef, useState,
  type HTMLAttributes, type ReactNode,
} from 'react'
import { cn } from '../../utils/cn'

/*
 * A diagram of boxes laid out by ordinary CSS (a grid, usually) with arrows
 * drawn between them: the boxes are measured after every render and on every
 * resize, so the arrows follow the layout wherever the page's container
 * queries move the boxes. A box that is not displayed (display: none at some
 * width) measures empty, and its arrows are left out.
 */

type Side = 'top' | 'right' | 'bottom' | 'left'

/** Where an arrow meets a box: a side, and a point along it as a fraction (default the middle) or in pixels from its start. */
export interface FlowAnchor {
  node: string
  side: Side
  at?: number
  px?: number
}

/** quiet: a path that exists, with nothing to report; ok, warning, danger: a status; active: traffic or work moving. */
export type FlowTone = 'quiet' | 'ok' | 'warning' | 'danger' | 'active'

export interface FlowEdge {
  id: string
  from: FlowAnchor
  to: FlowAnchor
  tone: FlowTone
  /** Dashes run along the arrow while something moves on it. */
  flowing?: boolean
  /** Shown on the arrow's middle where there is room for it, as a dot in its tone where there is not. */
  label?: ReactNode
  /** Shown on hovering the arrow or its label. */
  title?: string
}

const TONE: Record<FlowTone, { stroke: string; fill: string; dot: string }> = {
  quiet: { stroke: 'stroke-line-strong', fill: 'fill-line-strong', dot: 'bg-line-strong' },
  ok: { stroke: 'stroke-success', fill: 'fill-success', dot: 'bg-success' },
  warning: { stroke: 'stroke-warning', fill: 'fill-warning', dot: 'bg-warning' },
  danger: { stroke: 'stroke-danger', fill: 'fill-danger', dot: 'bg-danger' },
  active: { stroke: 'stroke-primary', fill: 'fill-primary', dot: 'bg-primary' },
}

// A label needs about this much of the arrow's span to sit on it without
// running over the boxes at its ends.
const LABEL_ROOM_X = 64
const LABEL_ROOM_Y = 40

interface Box { x: number; y: number; w: number; h: number }

const Register = createContext<(id: string, el: HTMLElement | null) => void>(() => {})

export function FlowDiagram({ edges, className, children }: { edges: FlowEdge[]; className?: string; children: ReactNode }) {
  const container = useRef<HTMLDivElement>(null)
  const nodes = useRef(new Map<string, HTMLElement>())
  const observer = useRef<ResizeObserver | null>(null)
  const [boxes, setBoxes] = useState<Record<string, Box>>({})
  // useId's characters vary by React release; a url(#...) reference wants plain ones.
  const markerId = `flow-${useId().replace(/[^\w-]/g, '')}`

  const measure = useCallback(() => {
    const root = container.current
    if (!root) return
    const origin = root.getBoundingClientRect()
    const next: Record<string, Box> = {}
    for (const [id, el] of nodes.current) {
      const r = el.getBoundingClientRect()
      if (r.width > 0 || r.height > 0) next[id] = { x: r.left - origin.left, y: r.top - origin.top, w: r.width, h: r.height }
    }
    // Only a change re-renders, so measuring after every render settles.
    setBoxes((prev) => (sameBoxes(prev, next) ? prev : next))
  }, [])

  // Data changes move boxes without resizing the container; measure after each render.
  useLayoutEffect(measure)

  useLayoutEffect(() => {
    const root = container.current
    if (!root) return
    const watch = new ResizeObserver(() => measure())
    watch.observe(root)
    for (const el of nodes.current.values()) watch.observe(el)
    observer.current = watch
    return () => {
      watch.disconnect()
      observer.current = null
    }
  }, [measure])

  const register = useCallback((id: string, el: HTMLElement | null) => {
    const known = nodes.current.get(id)
    if (known && known !== el) observer.current?.unobserve(known)
    if (el) {
      nodes.current.set(id, el)
      observer.current?.observe(el)
    } else {
      nodes.current.delete(id)
    }
  }, [])

  const drawn = edges.flatMap((edge) => {
    const from = boxes[edge.from.node]
    const to = boxes[edge.to.node]
    return from && to ? [{ edge, path: curve(anchorPoint(from, edge.from), edge.from.side, anchorPoint(to, edge.to), edge.to.side) }] : []
  })

  return (
    <div ref={container} className={cn('relative', className)}>
      <svg className="absolute inset-0 w-full h-full pointer-events-none overflow-visible" aria-hidden>
        <defs>
          {(Object.keys(TONE) as FlowTone[]).map((tone) => (
            <marker key={tone} id={`${markerId}-${tone}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0.5 L7.5,4 L0,7.5 z" className={TONE[tone].fill} />
            </marker>
          ))}
        </defs>
        {drawn.map(({ edge, path }) => (
          <g key={edge.id}>
            <path
              d={path.d}
              fill="none"
              strokeWidth={1.5}
              className={cn(TONE[edge.tone].stroke, edge.flowing && 'opacity-35')}
              markerEnd={edge.flowing ? undefined : `url(#${markerId}-${edge.tone})`}
            />
            {/* The arrowhead rides on the dashes, so it keeps full strength over the faded line. */}
            {edge.flowing && (
              <path
                d={path.d}
                fill="none"
                strokeWidth={2}
                strokeDasharray="3 7"
                strokeLinecap="round"
                className={cn(TONE[edge.tone].stroke, 'animate-edge-flow')}
                markerEnd={`url(#${markerId}-${edge.tone})`}
              />
            )}
            {/* A wider, invisible stroke that takes the pointer, so the title shows on hovering the arrow itself. */}
            {edge.title && (
              <path d={path.d} fill="none" stroke="transparent" strokeWidth={12} pointerEvents="stroke">
                <title>{edge.title}</title>
              </path>
            )}
          </g>
        ))}
      </svg>
      <Register.Provider value={register}>{children}</Register.Provider>
      {drawn.map(({ edge, path }) => edge.label != null && (
        <div
          key={edge.id}
          className="absolute z-20 -translate-x-1/2 -translate-y-1/2"
          style={{ left: path.mid.x, top: path.mid.y }}
          title={edge.title}
        >
          {path.roomy ? (
            <div className="rounded-md border border-line bg-surface px-1.5 py-0.5 text-[10.5px] leading-tight text-center whitespace-nowrap tabular-nums shadow-xs">
              {edge.label}
            </div>
          ) : (
            <span className={cn('block w-2 h-2 rounded-full ring-2 ring-surface', TONE[edge.tone].dot)} />
          )}
        </div>
      ))}
    </div>
  )
}

/** A box of the diagram, which arrows name by its id; laid out by the diagram's own CSS. */
export function FlowNode({ id, className, ...props }: { id: string } & HTMLAttributes<HTMLDivElement>) {
  const register = useContext(Register)
  const ref = useCallback((el: HTMLDivElement | null) => register(id, el), [register, id])
  return <div ref={ref} className={cn('relative z-10', className)} {...props} />
}

function sameBoxes(a: Record<string, Box>, b: Record<string, Box>): boolean {
  const keys = Object.keys(b)
  if (keys.length !== Object.keys(a).length) return false
  return keys.every((k) => {
    const p = a[k]
    const q = b[k]
    return p && Math.abs(p.x - q.x) < 0.5 && Math.abs(p.y - q.y) < 0.5 && Math.abs(p.w - q.w) < 0.5 && Math.abs(p.h - q.h) < 0.5
  })
}

interface Point { x: number; y: number }

function anchorPoint(box: Box, anchor: FlowAnchor): Point {
  const along = (length: number) => anchor.px ?? length * (anchor.at ?? 0.5)
  switch (anchor.side) {
    case 'top': return { x: box.x + along(box.w), y: box.y }
    case 'bottom': return { x: box.x + along(box.w), y: box.y + box.h }
    case 'left': return { x: box.x, y: box.y + along(box.h) }
    case 'right': return { x: box.x + box.w, y: box.y + along(box.h) }
  }
}

const NORMAL: Record<Side, Point> = { top: { x: 0, y: -1 }, right: { x: 1, y: 0 }, bottom: { x: 0, y: 1 }, left: { x: -1, y: 0 } }

/** A cubic curve leaving and entering square to the sides, its middle, and whether a label fits there. */
function curve(start: Point, startSide: Side, end: Point, endSide: Side): { d: string; mid: Point; roomy: boolean } {
  const dx = end.x - start.x
  const dy = end.y - start.y
  const reach = Math.min(120, Math.max(24, Math.hypot(dx, dy) * 0.45))
  const c1 = { x: start.x + NORMAL[startSide].x * reach, y: start.y + NORMAL[startSide].y * reach }
  const c2 = { x: end.x + NORMAL[endSide].x * reach, y: end.y + NORMAL[endSide].y * reach }
  const mid = { x: (start.x + 3 * c1.x + 3 * c2.x + end.x) / 8, y: (start.y + 3 * c1.y + 3 * c2.y + end.y) / 8 }
  return {
    d: `M${start.x},${start.y} C${c1.x},${c1.y} ${c2.x},${c2.y} ${end.x},${end.y}`,
    mid,
    roomy: Math.abs(dx) >= LABEL_ROOM_X || Math.abs(dy) >= LABEL_ROOM_Y,
  }
}
