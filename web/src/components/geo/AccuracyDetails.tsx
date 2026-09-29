// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { ClaimBreakdown, ConnectorAccuracy, ConnectorExits, LevelAccuracy, PlaceLevel } from '../../api/client'
import { Inspector, InspectorSection, Input, KeyValue, Segmented } from '../ui'
import { cn } from '../../utils/cn'
import { coverageLabel } from './coverage'

export const LEVELS: PlaceLevel[] = ['country', 'state', 'city']

/** Rate as a percentage with one decimal, or null when nothing was judged. */
export function ratePercent(rate: number | null | undefined): number | null {
  return rate == null ? null : Math.round(rate * 1000) / 10
}

export function rateClass(pct: number): string {
  return pct >= 95 ? 'text-success' : pct >= 80 ? 'text-warning' : 'text-danger'
}

export function levelOf(row: ConnectorAccuracy, level: PlaceLevel): LevelAccuracy {
  return row[level]
}

/** Contradicted pairs of one level, most frequent first. */
export function wrongAt(breakdown: ClaimBreakdown[], level: PlaceLevel): ClaimBreakdown[] {
  return breakdown.filter((b) => b.level === level && b.claimed && b.observed).sort((a, b) => b.exits - a.exits)
}

function pairText(b: ClaimBreakdown): string {
  return `${b.claimed} → ${b.observed}`
}

/**
 * The contradicted pairs of a connector, a line per level, a few pairs each.
 * Used where there is no room for the full list: the accuracy table and the
 * connector inspector. `limit` is pairs per level; the rest is counted.
 */
export function MismatchSummary({ breakdown, limit = 2, className }: { breakdown: ClaimBreakdown[]; limit?: number; className?: string }) {
  const lines = LEVELS.map((level) => ({ level, pairs: wrongAt(breakdown, level) })).filter((l) => l.pairs.length > 0)
  if (lines.length === 0) return <span className="text-fg-subtle">-</span>
  return (
    <div className={cn('space-y-0.5 text-xs text-fg-muted', className)}>
      {lines.map(({ level, pairs }) => {
        const shown = pairs.slice(0, limit)
        const rest = pairs.length - shown.length
        const exits = pairs.reduce((n, b) => n + b.exits, 0)
        return (
          <div key={level} className="flex items-baseline gap-1.5 min-w-0" title={pairs.map((b) => `${pairText(b)} (${b.exits})`).join(', ')}>
            <span className="uppercase tracking-wide text-[10px] text-fg-subtle w-12 flex-none">{level}</span>
            <span className="truncate">{shown.map((b) => `${pairText(b)} (${b.exits})`).join(', ')}{rest > 0 && <span className="text-fg-subtle"> +{rest} more</span>}</span>
            <span className="ml-auto tabular-nums text-fg-subtle flex-none">{exits.toLocaleString()}</span>
          </div>
        )
      })}
    </div>
  )
}

/** One level's judgement as a row of the accuracy-by-level table. */
function LevelRow({ level, acc }: { level: PlaceLevel; acc: LevelAccuracy }) {
  const pct = ratePercent(acc.accuracy)
  const openLabel = 'open: no independent source answered at this level, or they disagreed'
  return (
    <tr className="border-t border-line">
      <td className="py-1.5 pr-3 capitalize text-fg">{level}</td>
      <td className="py-1.5 px-2 text-right tabular-nums">{acc.claimed.toLocaleString()}</td>
      <td className="py-1.5 px-2 text-right tabular-nums text-success">{acc.confirmed.toLocaleString()}</td>
      <td className="py-1.5 px-2 text-right tabular-nums text-danger">{acc.contradicted.toLocaleString()}</td>
      <td className="py-1.5 px-2 text-right tabular-nums text-fg-muted" title={openLabel}>{acc.open.toLocaleString()}</td>
      <td className="py-1.5 pl-2 text-right tabular-nums font-medium">
        {pct == null ? <span className="text-fg-subtle font-normal">{acc.claimed > 0 ? 'no verdict' : '-'}</span> : <span className={rateClass(pct)}>{pct}%</span>}
      </td>
    </tr>
  )
}

/**
 * Docked panel with everything the accuracy table has no room for: the
 * judgement per level and the full list of contradicted pairs, filterable.
 * Rendered by a page in its `panel` slot, like every other inspector.
 */
export function AccuracyDetailsInspector({ row, exits, days, projectName, onClose }: {
  row: ConnectorAccuracy
  exits?: ConnectorExits
  days: number
  projectName?: string | null
  onClose: () => void
}) {
  const [level, setLevel] = useState<'all' | PlaceLevel>('all')
  const [filter, setFilter] = useState('')
  const pairs = useMemo(() => {
    const wanted = level === 'all' ? LEVELS : [level]
    const needle = filter.trim().toLowerCase()
    return wanted
      .flatMap((l) => wrongAt(row.breakdown, l))
      .filter((b) => !needle || `${b.claimed} ${b.observed}`.toLowerCase().includes(needle))
      .sort((a, b) => b.exits - a.exits)
  }, [row.breakdown, level, filter])
  const totalWrong = LEVELS.reduce((n, l) => n + levelOf(row, l).contradicted, 0)
  const coverage = coverageLabel(exits?.coverage ?? row.coverage)
  const countsByLevel = LEVELS.map((l) => ({ level: l, wrong: wrongAt(row.breakdown, l).length }))

  return (
    <Inspector
      title={row.connector_name ?? row.connector_id}
      subtitle={`${projectName ? `${projectName} · ` : ''}exit locations, last ${days} day${days === 1 ? '' : 's'}`}
      onClose={onClose}
    >
      <InspectorSection title="Accuracy by level">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="text-[11px] uppercase tracking-wide text-fg-subtle">
              <th className="text-left font-medium pb-1 pr-3">Level</th>
              <th className="text-right font-medium pb-1 px-2" title="Exits with a claim at this level">Claimed</th>
              <th className="text-right font-medium pb-1 px-2">Confirmed</th>
              <th className="text-right font-medium pb-1 px-2">Contradicted</th>
              <th className="text-right font-medium pb-1 px-2" title="Claims with no verdict: no independent source knows the IP at this level, or they disagreed.">Open</th>
              <th className="text-right font-medium pb-1 pl-2">Rate</th>
            </tr>
          </thead>
          <tbody>
            {LEVELS.map((l) => <LevelRow key={l} level={l} acc={levelOf(row, l)} />)}
          </tbody>
        </table>
        <p className="text-xs text-fg-muted">
          Every level is judged the same way under the project's source policy and conflict rule: confirmed when the independent sources agree with the claim, contradicted when they say somewhere else, open when none knows the IP at that level or they disagree. Rates are confirmed over confirmed plus contradicted. Each exit IP counts once, with its latest verdict.
        </p>
        {coverage && <p className="text-xs text-warning">{coverage.title}</p>}
      </InspectorSection>

      {exits && (
        <InspectorSection title="Exits">
          <KeyValue label="Unique exit IPs" value={<span className="tabular-nums">{exits.unique_in_window.toLocaleString()}<span className="text-fg-subtle font-normal"> / {exits.unique_total.toLocaleString()} ever</span></span>} />
          {exits.unique_total > 0 && <KeyValue label="Reused IPs" value={`${Math.round((exits.reused / exits.unique_total) * 1000) / 10}%`} />}
        </InspectorSection>
      )}

      <InspectorSection
        title={`Where it was wrong${totalWrong > 0 ? ` (${totalWrong.toLocaleString()} exits)` : ''}`}
        action={countsByLevel.some((c) => c.wrong > 0) && (
          <Segmented
            size="sm"
            value={level}
            onChange={setLevel}
            options={[{ value: 'all' as const, label: 'All' }, ...countsByLevel.map((c) => ({ value: c.level, label: `${c.level[0].toUpperCase()}${c.level.slice(1)}${c.wrong > 0 ? ` ${c.wrong}` : ''}` }))]}
          />
        )}
      >
        {row.breakdown.length === 0 ? (
          <p className="text-xs text-fg-subtle">No contradicted claims in the window.</p>
        ) : (
          <>
            <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter by claimed or observed place" className="text-xs" />
            <div className="max-h-[60vh] overflow-y-auto -mx-1 px-1">
              {pairs.length === 0 ? (
                <p className="text-xs text-fg-subtle py-2">Nothing matches the filter.</p>
              ) : (
                <ul className="divide-y divide-line">
                  {pairs.map((b) => (
                    <li key={`${b.level}:${b.claimed}:${b.observed}`} className="flex items-center gap-2 py-1.5 text-[13px]">
                      <span className="uppercase tracking-wide text-[10px] text-fg-subtle w-12 flex-none">{b.level}</span>
                      <span className="font-mono text-xs min-w-0 truncate" title={pairText(b)}>
                        <span className="text-fg">{b.claimed}</span>
                        <span className="text-fg-subtle"> claimed, </span>
                        <span className="text-danger">{b.observed}</span>
                        <span className="text-fg-subtle"> observed</span>
                      </span>
                      <span className="ml-auto tabular-nums text-xs text-fg-muted flex-none">{b.exits.toLocaleString()} exit{b.exits === 1 ? '' : 's'}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        )}
      </InspectorSection>
    </Inspector>
  )
}
