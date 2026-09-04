// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Tiny inline trend line for stat tiles. Pure SVG, scales with its box.
 * The last point is marked so the current value reads as "now".
 */
export function Sparkline({ values, color, width = 64, height = 26, className }: {
  values: number[]
  color: string
  width?: number
  height?: number
  className?: string
}) {
  if (values.length < 2) return <svg width={width} height={height} className={className} />
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const pad = 2
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * width
    const y = pad + (1 - (v - min) / span) * (height - pad * 2)
    return [x, y] as const
  })
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const [lx, ly] = pts[pts.length - 1]
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={className} style={{ overflow: 'visible', flex: 'none' }} aria-hidden>
      <polyline points={line} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" opacity={0.6} />
      <circle cx={lx} cy={ly} r={2.5} fill={color} />
    </svg>
  )
}
