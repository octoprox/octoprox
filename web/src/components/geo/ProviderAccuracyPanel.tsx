// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { CheckCircle2, PanelRightOpen } from 'lucide-react'
import { fetchProjects } from '../../api/client'
import { useTimeRange } from '../../contexts/TimeRangeContext'
import { useAccuracyRows, type AccuracyRow } from '../../hooks/useAccuracyRows'
import { DataTable, type DataTableFeatures } from '../DataTable'
import { EmptyState } from '../layout/Page'
import { Card, Inspector, LogoLoader, Segmented, Select } from '../ui'
import { cn } from '../../utils/cn'
import { HeaderWithTip, TIPS } from './columns'
import { coverageLabel } from './coverage'
import { AccuracyDetailsInspector, LEVELS, levelOf, rateClass, ratePercent, wrongAt } from './AccuracyDetails'

// The same ranges as the graph pages; claim statistics are daily, so nothing below a day.
export const ACCURACY_RANGES = [
  { value: '24h', label: '24h', days: 1 },
  { value: '7d', label: '7d', days: 7 },
  { value: '30d', label: '30d', days: 30 },
  { value: '90d', label: '90d', days: 90 },
] as const
type Range = (typeof ACCURACY_RANGES)[number]['value']
const ACCURACY_RANGE_VALUES = ACCURACY_RANGES.map((r) => r.value)

/** The shared time range narrowed to the daily windows claim statistics come in. */
export function useAccuracyRange() {
  return useTimeRange(ACCURACY_RANGE_VALUES)
}

export function accuracyDays(range: Range): number {
  return ACCURACY_RANGES.find((r) => r.value === range)?.days ?? 30
}

/**
 * Per-connector vendor location accuracy and distinct exit IPs.
 *
 * Scoped to one project when `projectId` is given (the project pages), or
 * across every project with a project column and filter when it is not (the
 * admin settings page). Same component, same numbers, either way. The page
 * hosting it passes `onInspect` and shows the details in its panel slot; the
 * table itself keeps only what fits in a row.
 */
export function ProviderAccuracyPanel({ projectId, projectFilter = '', onProjectFilter, onInspect, inspecting }: {
  projectId?: string
  /** Across every project: the one the table is narrowed to ('' for all), kept by the page so its inspector reads the same rows. */
  projectFilter?: string
  onProjectFilter?: (projectId: string) => void
  onInspect?: (row: AccuracyRow) => void
  /** Connector id whose details are open, to mark its row. */
  inspecting?: string | null
}) {
  const [range, setRange] = useAccuracyRange()
  const days = accuracyDays(range)
  const scope = projectId ?? (projectFilter || undefined)
  const { rows, isLoading, reloading } = useAccuracyRows(scope, days, { named: !projectId })
  const { data: projects } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects, enabled: !projectId })

  const columns: ColumnDef<DataTableFeatures, AccuracyRow>[] = useMemo(() => [
    {
      accessorKey: 'connector_name', header: 'Connector', meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <div className="min-w-0">
          <div className="font-medium truncate">{row.original.connector_name ?? row.original.connector_id}</div>
          {!projectId && <div className="text-xs text-fg-muted truncate">{row.original.project_name ?? row.original.project_id ?? '-'}</div>}
        </div>
      ),
    },
    {
      id: 'accuracy', accessorFn: (r: AccuracyRow) => r.country.accuracy ?? -1, header: () => <HeaderWithTip label="Accuracy" tip={TIPS.accuracy} />, size: 400,
      cell: ({ row }) => {
        const levels = LEVELS.map((level) => ({ level, acc: row.original[level] })).filter(({ acc }) => acc.claimed > 0)
        if (levels.length === 0) return <span className="text-fg-subtle">no claims</span>
        return (
          <div className="space-y-1 py-0.5">
            {levels.map(({ level, acc }) => {
              const pct = ratePercent(acc.accuracy)
              const title = `${level}: ${acc.confirmed.toLocaleString()} confirmed, ${acc.contradicted.toLocaleString()} contradicted, ${acc.open.toLocaleString()} open of ${acc.claimed.toLocaleString()} exits with a claim`
              return (
                <div key={level} className="flex items-center gap-2 text-xs" title={title}>
                  <span className="w-12 flex-none uppercase tracking-wide text-[10px] text-fg-subtle">{level}</span>
                  {/* The bar repeats the rate, so it gives up room before the counts do. */}
                  <div className="h-1.5 w-20 min-w-6 shrink rounded-full bg-surface-raised overflow-hidden">
                    {pct != null && <div className={cn('h-full rounded-full', pct >= 95 ? 'bg-success' : pct >= 80 ? 'bg-warning' : 'bg-danger')} style={{ width: `${pct}%` }} />}
                  </div>
                  <span className={cn('tabular-nums w-12 text-right', pct == null ? 'text-fg-subtle' : rateClass(pct))}>{pct == null ? 'open' : `${pct}%`}</span>
                  <span className="tabular-nums text-fg-subtle flex-none whitespace-nowrap">
                    <span className="text-success">{acc.confirmed.toLocaleString()}</span> / <span className="text-danger">{acc.contradicted.toLocaleString()}</span>{acc.open > 0 && <> / {acc.open.toLocaleString()} open</>}
                  </span>
                </div>
              )
            })}
          </div>
        )
      },
    },
    {
      id: 'unique', header: () => <HeaderWithTip label="Unique exits" tip={TIPS.uniqueExits} />, size: 150, meta: { align: 'right' as const },
      cell: ({ row }) => {
        const e = row.original.exit_stats
        if (!e) return <span className="text-fg-subtle">-</span>
        const coverage = coverageLabel(e.coverage ?? row.original.coverage)
        return (
          <span className="tabular-nums" title={`${e.unique_in_window.toLocaleString()} first seen in the window, ${e.unique_total.toLocaleString()} ever${coverage ? `. ${coverage.title}` : ''}`}>
            {e.unique_in_window.toLocaleString()}
            <span className="text-fg-subtle"> / {e.unique_total.toLocaleString()}</span>
            {coverage && <span className="ml-1.5 text-[10px] uppercase tracking-wide text-warning">{coverage.text}</span>}
          </span>
        )
      },
    },
    {
      id: 'reuse', header: () => <HeaderWithTip label="Reused" tip={TIPS.reused} />, size: 100, meta: { align: 'right' as const },
      cell: ({ row }) => {
        const e = row.original.exit_stats
        if (!e || e.unique_total === 0) return <span className="text-fg-subtle">-</span>
        const pct = Math.round((e.reused / e.unique_total) * 1000) / 10
        return <span className="tabular-nums" title={`${e.reused.toLocaleString()} IPs handed out more than once; the most reused ${e.max_sightings} times`}>{pct}%</span>
      },
    },
    {
      id: 'mismatches', accessorFn: (r: AccuracyRow) => LEVELS.reduce((n, l) => n + levelOf(r, l).contradicted, 0),
      header: () => <HeaderWithTip label="Where it was wrong" tip={TIPS.whereWrong} />, size: 230,
      cell: ({ row }) => {
        const wrongLevels = LEVELS.map((l) => ({ level: l, count: levelOf(row.original, l).contradicted, top: wrongAt(row.original.breakdown, l)[0] })).filter((w) => w.count > 0)
        const open = onInspect ? () => onInspect(row.original) : undefined
        return (
          <div className="flex items-center gap-2 min-w-0">
            {wrongLevels.length === 0 ? <span className="text-fg-subtle">-</span> : (
              <div className="min-w-0 text-xs text-fg-muted space-y-0.5">
                {wrongLevels.map((w) => (
                  <div key={w.level} className="truncate" title={w.top ? `${w.top.claimed} claimed, ${w.top.observed} observed (${w.top.exits})` : undefined}>
                    <span className="uppercase tracking-wide text-[10px] text-fg-subtle mr-1">{w.level}</span>
                    <span className="tabular-nums text-danger">{w.count.toLocaleString()}</span>
                    {w.top && <span className="text-fg-subtle"> · {w.top.claimed} → {w.top.observed}</span>}
                  </div>
                ))}
              </div>
            )}
            {open && (
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); open() }}
                className={cn('ml-auto flex-none p-1 rounded-md text-fg-subtle hover:text-fg hover:bg-surface-raised', inspecting === row.original.connector_id && 'text-primary')}
                title="Accuracy by level and every contradicted pair"
              >
                <PanelRightOpen className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        )
      },
    },
  ], [projectId, onInspect, inspecting])

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-fg-muted max-w-2xl">
          Of the distinct exit IPs each connector handed out in the window, how many the vendor made a location claim for and how many of those claims attribution confirmed, each IP counted once with its latest verdict; the same for the states and cities requests asked for. Also how many distinct exits the connector handed out (first seen in the window / ever) and how often it reuses them. Rates below 100% are normal for residential pools whose ranges databases have not caught up with; a rate that keeps falling is a vendor problem.
        </p>
        <div className="flex items-center gap-2">
          {!projectId && (
            <Select value={projectFilter} onChange={(e) => onProjectFilter?.(e.target.value)} className="w-44">
              <option value="">All projects</option>
              {(projects?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
          )}
          <Segmented options={ACCURACY_RANGES.map((r) => ({ value: r.value, label: r.label }))} value={range} onChange={setRange} size="sm" />
        </div>
      </div>
      <Card className="p-0 overflow-hidden">
        {!isLoading && rows.length === 0 ? (
          <EmptyState icon={<CheckCircle2 className="w-5 h-5" />} reloading={reloading} title="Nothing to compare yet" description="Accuracy appears once proxies with a vendor-declared country have been attributed; unique exits once any exit IP has been seen." />
        ) : (
          <DataTable columns={columns} data={rows} getRowId={(c) => c.connector_id} enableColumnFilters defaultPageSize={25} loading={isLoading} reloading={reloading} onRowClick={onInspect ? (row) => onInspect(row) : undefined} />
        )}
      </Card>
    </div>
  )
}

/**
 * The details of the connector a page has open by id, its row read through `useAccuracyRows` so they follow the window
 * shown: a loader while that answer is out, and a note when the connector has no exits in the window, so a wider one
 * can bring it back.
 */
export function AccuracyInspector({ row, loading, name, days, projectName, onClose }: {
  row?: AccuracyRow
  loading: boolean
  /** The connector's name when it has no row. */
  name?: string
  days: number
  projectName?: string | null
  onClose: () => void
}) {
  if (row) return <AccuracyDetailsInspector key={row.connector_id} row={row} exits={row.exit_stats} days={days} projectName={projectName} onClose={onClose} />
  return (
    <Inspector title={name ?? 'Provider accuracy'} subtitle={`exit locations, last ${days} day${days === 1 ? '' : 's'}`} onClose={onClose}>
      {loading ? <LogoLoader className="py-10" /> : <p className="text-sm text-fg-muted">No exits seen in this window. A longer one may have some.</p>}
    </Inspector>
  )
}
