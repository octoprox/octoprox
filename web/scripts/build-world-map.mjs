// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0
//
// Generates src/assets/world-map.json from @svg-maps/world (a devDependency).
// The source paths use relative moves with three-decimal precision and every
// islet; that is 1.2 MB of JavaScript. For a panel-sized choropleth we
// re-emit each country as closed polylines with 0.1 px precision (the viewBox
// is 1010 px wide) and drop sub-paths smaller than MIN_DIAG px, unless they
// are all a country has, then straighten runs of near-collinear points.
// Run: node scripts/build-world-map.mjs
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const MIN_DIAG = 1.5
const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, '../node_modules/@svg-maps/world/index.js'), 'utf8')
const world = JSON.parse(source.replace(/^export default /, '').replace(/;\s*$/, ''))

/** Parse an SVG path made of m/l/z commands (relative or absolute) into absolute point lists. */
function parse(d) {
  const tokens = d.match(/[a-zA-Z]|-?\d*\.?\d+(?:e-?\d+)?/g) ?? []
  const subpaths = []
  let cmd = null
  let x = 0, y = 0, startX = 0, startY = 0
  let current = null
  let i = 0
  const next = () => Number(tokens[i++])
  while (i < tokens.length) {
    const token = tokens[i]
    if (/^[a-zA-Z]$/.test(token)) {
      cmd = token
      i++
      if (cmd === 'z' || cmd === 'Z') { x = startX; y = startY; cmd = null }
      continue
    }
    if (cmd === null) throw new Error(`number without command in ${d.slice(0, 40)}`)
    const dx = next(), dy = next()
    if (cmd === 'm' || cmd === 'M') {
      if (cmd === 'm') { x += dx; y += dy } else { x = dx; y = dy }
      current = []
      subpaths.push(current)
      current.push([x, y])
      startX = x; startY = y
      cmd = cmd === 'm' ? 'l' : 'L'
    } else if (cmd === 'l' || cmd === 'L') {
      if (cmd === 'l') { x += dx; y += dy } else { x = dx; y = dy }
      current.push([x, y])
    } else {
      throw new Error(`unsupported path command ${cmd}`)
    }
  }
  return subpaths
}

const round = (v) => Math.round(v * 10) / 10
const fmt = (v) => String(round(v)).replace(/^(-?)0\./, '$1.')

function diagonal(points) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const [x, y] of points) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y) }
  return Math.hypot(x1 - x0, y1 - y0)
}

/** Ramer-Douglas-Peucker: drop points that deviate less than EPSILON px from the line between their neighbours. */
function simplify(points, epsilon) {
  if (points.length < 3) return points
  const keep = new Array(points.length).fill(false)
  keep[0] = keep[points.length - 1] = true
  const stack = [[0, points.length - 1]]
  while (stack.length) {
    const [a, b] = stack.pop()
    const [ax, ay] = points[a], [bx, by] = points[b]
    const len = Math.hypot(bx - ax, by - ay)
    let worst = -1, worstDist = 0
    for (let i = a + 1; i < b; i++) {
      const [px, py] = points[i]
      const dist = len === 0 ? Math.hypot(px - ax, py - ay) : Math.abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / len
      if (dist > worstDist) { worstDist = dist; worst = i }
    }
    if (worst !== -1 && worstDist > epsilon) { keep[worst] = true; stack.push([a, worst], [worst, b]) }
  }
  return points.filter((_, i) => keep[i])
}

const EPSILON = 0.3

function emit(subpaths) {
  const sized = subpaths.map((points) => ({ points, diag: diagonal(points) }))
  let kept = sized.filter((s) => s.diag >= MIN_DIAG)
  if (kept.length === 0) kept = [sized.reduce((a, b) => (a.diag >= b.diag ? a : b))]
  let out = ''
  for (const { points } of kept) {
    const dedupe = (source) => {
      const result = []
      for (const [x, y] of source) {
        const p = [round(x), round(y)]
        const q = result[result.length - 1]
        if (!q || q[0] !== p[0] || q[1] !== p[1]) result.push(p)
      }
      return result
    }
    // Tiny shapes collapse under simplification; keep their raw outline so
    // every country stays on the map (a city state is still a hover target).
    let rounded = dedupe(simplify(points, EPSILON))
    if (rounded.length < 4) rounded = dedupe(points)
    if (rounded.length < 3) {
      const [cx, cy] = points[0]
      rounded = [[round(cx - 0.6), round(cy - 0.6)], [round(cx + 0.6), round(cy - 0.6)], [round(cx + 0.6), round(cy + 0.6)], [round(cx - 0.6), round(cy + 0.6)]]
    }
    out += `M${fmt(rounded[0][0])} ${fmt(rounded[0][1])}`
    let [px, py] = rounded[0]
    for (let k = 1; k < rounded.length; k++) {
      const [x, y] = rounded[k]
      out += `l${fmt(x - px)} ${fmt(y - py)}`
      px = x; py = y
    }
    out += 'z'
  }
  return out.replace(/ -/g, '-')
}

const locations = world.locations
  .map((l) => ({ id: l.id.toUpperCase(), name: l.name, d: emit(parse(l.path)) }))
  .filter((l) => l.d.length > 0)
  .sort((a, b) => a.id.localeCompare(b.id))

const output = { viewBox: world.viewBox, locations }
const target = join(here, '../src/assets/world-map.json')
writeFileSync(target, JSON.stringify(output))
console.log(`wrote ${target}: ${locations.length} countries, ${JSON.stringify(output).length} bytes`)
