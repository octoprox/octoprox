// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { CheckCircle2 } from 'lucide-react'
import { fetchGeoAccuracy, fetchGeoExits, fetchProjects, ConnectorAccuracy, ConnectorExits, ExitCoverage } from '../../api/client'
import { DataTable } from '../DataTable'
import { EmptyState } from '../layout/Page'
import { Card, Segmented, Select } from '../ui'
import { cn } from '../../utils/cn'
import { HeaderWithTip, TIPS } from './columns'

type AccuracyRow = ConnectorAccuracy & { exit_stats?: ConnectorExits; project_name?: string | null }

/** Short label for a dynamic-sessions connector whose session-less traffic is not fully observed. */
export function coverageLabel(coverage: ExitCoverage | null | undefined): { text: string; title: string } | null {
  if (!coverage || !coverage.dynamic || coverage.sampled_percent >= 100) return null
  if (!coverage.preflight_on) {
    return { text: 'preflight off', title: 'Dynamic sessions with preflight off: exits are not observed. Turn preflight on for the project to see them.' }
  }
  return {
    text: `sampled ${coverage.sampled_percent}%`,
    title: `Dynamic sessions: every client session is observed once; requests without a session are echoed for ${coverage.sampled_percent}% of requests. Distinct-exit and reuse figures for session-less traffic are undercounts.`,
  }
}

// The same ranges as the graph pages; claim statistics are daily, so nothing below a day.
const RANGES = [
  { value: '24h', label: '24h', days: 1 },
  { value: '7d', label: '7d', days: 7 },
  { value: '30d', label: '30d', days: 30 },
  { value: '90d', label: '90d', days: 90 },
] as const
type Range = (typeof RANGES)[number]['value']

/**
 * Per-connector vendor location accuracy and distinct exit IPs.
 *
 * Scoped to one project when `projectId` is given (the project pages), or
 * across every project with a project column and filter when it is not (the
 * admin settings page). Same component, same numbers, either way.
 */
export function ProviderAccuracyPanel({ projectId }: { projectId?: string }) {
  const [range, setRange] = useState<Range>('30d')
  const days = RANGES.find((r) => r.value === range)?.days ?? 30
  const [projectFilter, setProjectFilter] = useState('')
  const scope = projectId ?? (projectFilter || undefined)
  const { data, isLoading } = useQuery({
    queryKey: ['geo-accuracy', scope ?? 'all', days],
    queryFn: () => fetchGeoAccuracy({ days, project_id: scope }),
    refetchInterval: 60_000,
  })
  const { data: exits } = useQuery({
    queryKey: ['geo-exits', scope ?? 'all', days],
    queryFn: () => fetchGeoExits({ days, project_id: scope }),
    refetchInterval: 60_000,
  })
  const { data: projects } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects, enabled: !projectId })
  const projectNames = useMemo(() => new Map((projects?.projects ?? []).map((p) => [p.id, p.name])), [projects])

  const rows: AccuracyRow[] = useMemo(() => {
    const byConnector = new Map((exits?.connectors ?? []).map((e) => [e.connector_id, e]))
    const seen = new Set<string>()
    const merged: AccuracyRow[] = (data?.connectors ?? []).map((c) => {
      seen.add(c.connector_id)
      return { ...c, exit_stats: byConnector.get(c.connector_id), project_name: c.project_id ? projectNames.get(c.project_id) ?? null : null }
    })
    // Connectors with exits but no vendor claims still deserve a row.
    for (const e of exits?.connectors ?? []) {
      if (!seen.has(e.connector_id)) {
        merged.push({
          connector_id: e.connector_id, connector_name: e.connector_name, project_id: e.project_id,
          project_name: e.project_id ? projectNames.get(e.project_id) ?? null : null,
          exits: e.unique_in_window, claimed: 0, confirmed: 0, contradicted: 0, uncertain: 0, accuracy: null, breakdown: [], coverage: e.coverage, exit_stats: e,
        })
      }
    }
    return merged
  }, [data, exits, projectNames])

  const columns: ColumnDef<AccuracyRow>[] = useMemo(() => [
    {
      accessorKey: 'connector_name', header: 'Connector', meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <div className="min-w-0">
          <div className="font-medium truncate">{row.original.connector_name ?? row.original.connector_id}</div>
          {!projectId && <div className="text-xs text-fg-muted truncate">{row.original.project_name ?? row.original.project_id ?? '-'}</div>}
        </div>
      ),
    },
    { accessorKey: 'claimed', header: () => <HeaderWithTip label="Exits with a claim" tip={TIPS.claimsChecked} />, size: 205, meta: { align: 'right' as const }, cell: ({ getValue }) => getValue<number>().toLocaleString() },
    {
      accessorKey: 'confirmed', header: () => <HeaderWithTip label="Confirmed" tip={TIPS.confirmed} />, size: 150, meta: { align: 'right' as const },
      cell: ({ row }) => (
        <span className="tabular-nums" title={`${row.original.contradicted.toLocaleString()} contradicted, ${row.original.uncertain.toLocaleString()} uncertain`}>
          {row.original.confirmed.toLocaleString()}
          {row.original.contradicted > 0 && <span className="text-danger"> / {row.original.contradicted.toLocaleString()}</span>}
        </span>
      ),
    },
    {
      accessorKey: 'accuracy', header: () => <HeaderWithTip label="Accuracy" tip={TIPS.accuracy} />, size: 170,
      cell: ({ getValue }) => {
        const rate = getValue<number | null>()
        if (rate == null) return <span className="text-fg-subtle">no claims</span>
        const pct = Math.round(rate * 1000) / 10
        return (
          <div className="flex items-center gap-2">
            <div className="h-1.5 flex-1 rounded-full bg-surface-raised overflow-hidden"><div className={cn('h-full rounded-full', pct >= 95 ? 'bg-success' : pct >= 80 ? 'bg-warning' : 'bg-danger')} style={{ width: `${pct}%` }} /></div>
            <span className="tabular-nums text-xs w-12 text-right">{pct}%</span>
          </div>
        )
      },
    },
    {
      id: 'unique', header: () => <HeaderWithTip label="Unique exits" tip={TIPS.uniqueExits} />, size: 160, meta: { align: 'right' as const },
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
      id: 'reuse', header: () => <HeaderWithTip label="Reused" tip={TIPS.reused} />, size: 120, meta: { align: 'right' as const },
      cell: ({ row }) => {
        const e = row.original.exit_stats
        if (!e || e.unique_total === 0) return <span className="text-fg-subtle">-</span>
        const pct = Math.round((e.reused / e.unique_total) * 1000) / 10
        return <span className="tabular-nums" title={`${e.reused.toLocaleString()} IPs handed out more than once; the most reused ${e.max_sightings} times`}>{pct}%</span>
      },
    },
    {
      id: 'mismatches', header: () => <HeaderWithTip label="Where it was wrong" tip={TIPS.whereWrong} />,
      cell: ({ row }) => {
        const wrong = row.original.breakdown.filter((b) => b.claimed_country && b.observed_country && b.claimed_country !== b.observed_country).slice(0, 4)
        return wrong.length === 0 ? <span className="text-fg-subtle">-</span> : (
          <span className="text-xs text-fg-muted">{wrong.map((b) => `${b.claimed_country} → ${b.observed_country} (${b.exits})`).join(', ')}</span>
        )
      },
    },
  ], [projectId])

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-fg-muted max-w-2xl">
          Of the distinct exit IPs each connector handed out in the window, how many the vendor made a location claim for and how many of those claims attribution confirmed, each IP counted once with its latest verdict. Also how many distinct exits the connector handed out (first seen in the window / ever) and how often it reuses them. Rates below 100% are normal for residential pools whose ranges databases have not caught up with; a rate that keeps falling is a vendor problem.
        </p>
        <div className="flex items-center gap-2">
          {!projectId && (
            <Select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)} className="w-44">
              <option value="">All projects</option>
              {(projects?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
          )}
          <Segmented options={RANGES.map((r) => ({ value: r.value, label: r.label }))} value={range} onChange={setRange} size="sm" />
        </div>
      </div>
      <Card className="p-0 overflow-hidden">
        {!isLoading && rows.length === 0 ? (
          <EmptyState icon={<CheckCircle2 className="w-5 h-5" />} title="Nothing to compare yet" description="Accuracy appears once proxies with a vendor-declared country have been attributed; unique exits once any exit IP has been seen." />
        ) : (
          <DataTable columns={columns} data={rows} getRowId={(c) => c.connector_id} enableColumnFilters defaultPageSize={25} />
        )}
      </Card>
    </div>
  )
}
