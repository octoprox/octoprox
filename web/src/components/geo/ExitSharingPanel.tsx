// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Share2 } from 'lucide-react'
import { fetchGeoExitSharing, type ConnectorSharing, type SharedPair } from '../../api/client'
import { useProviders } from '../../hooks/useProviders'
import { DataTable, type DataTableFeatures } from '../DataTable'
import { EmptyState } from '../layout/Page'
import { ProviderLogo } from '../ProviderLogo'
import { Alert, Card, CardHeader, Segmented } from '../ui'
import { cn } from '../../utils/cn'
import { HeaderWithTip, TIPS } from './columns'
import { coverageLabel } from './coverage'
import { SharingChord, UniquenessScatter, percent, useConnectorColors } from './ExitSharingCharts'
import { ACCURACY_RANGES, accuracyDays, useAccuracyRange } from './ProviderAccuracyPanel'

type SharingRow = ConnectorSharing & {
  name: string
  /** The connector this one shares the most exits with, if any. */
  partner: { name: string; shared: number } | null
}

// The heatmap's shading: the primary colour mixed into the surface, from a
// faint tint for any overlap to this much at 100%, so cell text stays legible
// in every theme.
const SHADE_MIN = 8
const SHADE_MAX = 60

/** A share as text; rounding never shows none or all of `whole` when it is not. */
function percentText(part: number, whole: number): string {
  const pct = percent(part, whole)
  if (pct === 0 && part > 0) return '<0.1%'
  if (pct === 100 && part < whole) return '>99.9%'
  return `${pct}%`
}

function shade(part: number, whole: number): string | undefined {
  if (part <= 0 || whole <= 0) return undefined
  const mix = SHADE_MIN + (Math.min(part / whole, 1) * (SHADE_MAX - SHADE_MIN))
  return `color-mix(in oklab, var(--color-primary) ${mix.toFixed(1)}%, var(--color-surface))`
}

const pairKey = (a: string, b: string) => `${a} ${b}`

/** Shared exits by connector pair, either way round. */
function pairCounts(pairs: SharedPair[]): Map<string, number> {
  const counts = new Map<string, number>()
  for (const p of pairs) {
    counts.set(pairKey(p.connector_id, p.other_id), p.shared)
    counts.set(pairKey(p.other_id, p.connector_id), p.shared)
  }
  return counts
}

/**
 * Whether a project's connectors hand out the same exit IPs: per connector
 * its distinct exits seen in the window, reuse and overlap, and every pair's
 * overlap as a heatmap. One project at a time.
 */
export function ExitSharingPanel({ projectId }: { projectId: string }) {
  const [range, setRange] = useAccuracyRange()
  const days = accuracyDays(range)
  // A comparison the server called off for its time limit is not asked for again until the window changes: each try
  // reads for the whole limit.
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['geo-exit-sharing', projectId, days],
    queryFn: () => fetchGeoExitSharing({ project_id: projectId, days }),
    refetchInterval: (query) => (query.state.status === 'error' ? false : 60_000),
    retry: false,
  })

  const shared = useMemo(() => pairCounts(data?.pairs ?? []), [data])
  const sharedOf = useCallback((a: string, b: string) => shared.get(pairKey(a, b)) ?? 0, [shared])
  const rows: SharingRow[] = useMemo(() => {
    const connectors = data?.connectors ?? []
    const names = new Map(connectors.map((c) => [c.connector_id, c.connector_name ?? c.connector_id]))
    return connectors.map((c) => {
      let partner: SharingRow['partner'] = null
      for (const other of connectors) {
        const n = other.connector_id === c.connector_id ? 0 : shared.get(pairKey(c.connector_id, other.connector_id)) ?? 0
        if (n > 0 && (!partner || n > partner.shared)) partner = { name: names.get(other.connector_id)!, shared: n }
      }
      return { ...c, name: names.get(c.connector_id)!, partner }
    })
  }, [data, shared])
  const colors = useConnectorColors(rows)
  const top = useMemo(() => {
    const best = (data?.pairs ?? []).reduce<SharedPair | null>((b, p) => (!b || p.shared > b.shared ? p : b), null)
    if (!best) return null
    const nameOf = (id: string) => rows.find((r) => r.connector_id === id)?.name ?? id
    return { shared: best.shared, names: [nameOf(best.connector_id), nameOf(best.other_id)] }
  }, [data, rows])

  const columns: ColumnDef<DataTableFeatures, SharingRow>[] = useMemo(() => [
    {
      accessorKey: 'name', header: 'Connector',
      cell: ({ row }) => <ConnectorName row={row.original} color={colors.get(row.original.connector_id)} />,
    },
    {
      accessorKey: 'exits', header: () => <HeaderWithTip label="Exits seen" tip={TIPS.sharingExits} />, size: 140, meta: { align: 'right' as const },
      cell: ({ row }) => {
        const coverage = coverageLabel(row.original.coverage)
        return (
          <span className="tabular-nums" title={coverage?.title}>
            {row.original.exits.toLocaleString()}
            {coverage && <span className="block text-[10px] uppercase tracking-wide text-warning">{coverage.text}</span>}
          </span>
        )
      },
    },
    {
      id: 'reused', accessorFn: (r: SharingRow) => percent(r.reused, r.exits), header: () => <HeaderWithTip label="Reused" tip={TIPS.sharingReused} />, size: 110, meta: { align: 'right' as const },
      cell: ({ row }) => (
        <span className="tabular-nums">
          {percentText(row.original.reused, row.original.exits)}
          <span className="block text-[11px] text-fg-subtle">{row.original.reused.toLocaleString()}</span>
        </span>
      ),
    },
    {
      id: 'shared', accessorFn: (r: SharingRow) => percent(r.shared, r.exits), header: () => <HeaderWithTip label="Shared" tip={TIPS.shared} />, size: 170, meta: { align: 'right' as const },
      cell: ({ row }) => {
        const { shared: n, exits } = row.original
        return (
          <div className="flex items-center justify-end gap-2">
            {/* The bar repeats the rate, so it gives up room before the numbers do. */}
            <div className="h-1.5 w-16 min-w-6 shrink rounded-full bg-surface-raised overflow-hidden" aria-hidden>
              {n > 0 && <div className="h-full rounded-full bg-primary" style={{ width: `${Math.max(percent(n, exits), 2)}%` }} />}
            </div>
            <span className="tabular-nums w-14 flex-none">
              {percentText(n, exits)}
              <span className="block text-[11px] text-fg-subtle">{n.toLocaleString()}</span>
            </span>
          </div>
        )
      },
    },
    {
      id: 'exclusive', accessorFn: (r: SharingRow) => r.exits - r.shared, header: () => <HeaderWithTip label="Only here" tip={TIPS.exclusive} />, size: 120, meta: { align: 'right' as const },
      cell: ({ getValue }) => <span className="tabular-nums">{getValue<number>().toLocaleString()}</span>,
    },
    {
      id: 'partner', accessorFn: (r: SharingRow) => r.partner?.shared ?? 0, header: () => <HeaderWithTip label="Overlaps most with" tip={TIPS.overlapsMost} />, size: 200,
      cell: ({ row }) => {
        const p = row.original.partner
        if (!p) return <span className="text-fg-subtle">-</span>
        return (
          <div className="min-w-0">
            <div className="truncate" title={p.name}>{p.name}</div>
            <div className="text-[11px] text-fg-subtle tabular-nums">{p.shared.toLocaleString()} shared</div>
          </div>
        )
      },
    },
  ], [colors])

  const connectorCount = rows.length
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-fg-muted max-w-2xl">
          Whether this project's connectors hand out the same exit IPs. Per connector, the distinct exits seen in the window, how many it handed out more than once, and how many another connector also handed out in the window. Two connectors on one provider usually share a pool; two providers sharing many exits are likely selling the same network. Only observed exits count, so a connector marked "sampled" is undercounted.
        </p>
        <Segmented options={ACCURACY_RANGES.map((r) => ({ value: r.value, label: r.label }))} value={range} onChange={setRange} size="sm" />
      </div>
      {isError && (
        <Alert variant="error">Could not compare the exits{error instanceof Error && error.message ? `: ${error.message}` : ''}</Alert>
      )}
      {isLoading && <p className="text-sm text-fg-muted">Comparing the exits…</p>}
      {!isLoading && !isError && connectorCount === 0 ? (
        <EmptyState icon={<Share2 />} title="No exits seen in this window" description="Sharing appears once the project's connectors have handed out exit IPs that were observed. Try a longer window." />
      ) : data && (
        <>
          <Card className="grid grid-cols-2 @lg:grid-cols-3 gap-px bg-line overflow-hidden *:bg-surface">
            <Kpi label="Distinct exits" value={data.exits.toLocaleString()} sub={`across ${connectorCount} ${connectorCount === 1 ? 'connector' : 'connectors'}`} />
            <Kpi label="Shared" value={percentText(data.shared, data.exits)} sub={`${data.shared.toLocaleString()} IPs on 2+ connectors`} />
            <Kpi
              className="col-span-2 @lg:col-span-1"
              label="Largest overlap"
              value={top ? `${top.shared.toLocaleString()} IPs` : '-'}
              sub={top ? `${top.names[0]} and ${top.names[1]}` : 'no exit behind two connectors'}
            />
          </Card>
          <div className="grid grid-cols-1 @4xl:grid-cols-2 gap-3">
            <Card className="p-4 space-y-2 min-w-0">
              <CardHeader title={<>Size vs. uniqueness <span className="text-fg-subtle font-normal">· right: exits seen, up: share no other connector had</span></>} />
              <UniquenessScatter connectors={rows} colors={colors} />
            </Card>
            <Card className="p-4 space-y-3 min-w-0">
              <CardHeader title={<>Shared exits <span className="text-fg-subtle font-normal">· a ribbon is the exits two connectors both handed out</span></>} />
              {connectorCount < 2 ? (
                <p className="text-sm text-fg-muted">Sharing needs a second connector with exits seen in the window.</p>
              ) : (
                <SharingChord connectors={rows} sharedOf={sharedOf} colors={colors} />
              )}
            </Card>
          </div>
          <Card className="p-0 overflow-hidden">
            <DataTable columns={columns} data={rows} getRowId={(r) => r.connector_id} defaultPageSize={25} />
          </Card>
          <Card className="p-4 space-y-3">
            <CardHeader
              title={<>Overlap between connectors <span className="text-fg-subtle font-normal">· share of the row's exits the column also handed out</span></>}
            />
            {connectorCount < 2 ? (
              <p className="text-sm text-fg-muted">Overlap needs a second connector with exits seen in the window.</p>
            ) : (
              <>
                <OverlapMatrix rows={rows} shared={shared} />
                <ShadeLegend />
              </>
            )}
          </Card>
        </>
      )}
    </div>
  )
}

function ConnectorName({ row, color }: { row: SharingRow; color?: string }) {
  const { labelFor } = useProviders()
  return (
    <div className="flex items-center gap-2 min-w-0">
      <ProviderLogo type={row.provider} className="w-5 h-5 text-[20px]" />
      <div className="min-w-0">
        <div className="font-medium flex items-center gap-1.5 min-w-0">
          {color && <span className="w-2 h-2 rounded-full flex-none" style={{ backgroundColor: color }} aria-hidden />}
          <span className="truncate">{row.name}</span>
        </div>
        <div className="text-xs text-fg-muted truncate">{labelFor(row.provider)}</div>
      </div>
    </div>
  )
}

/** Connector by connector: what share of the row's exits the column's connector also handed out, shaded by that share. */
function OverlapMatrix({ rows, shared }: { rows: SharingRow[]; shared: Map<string, number> }) {
  return (
    <div className="overflow-x-auto -mx-4 px-4">
      <table className="text-xs border-separate border-spacing-0.5" aria-label="Overlap between connectors">
        <thead>
          <tr>
            <th className="sticky left-0 z-10 bg-surface" />
            {rows.map((c) => (
              <th key={c.connector_id} scope="col" className="px-1 pb-1 font-medium text-fg-muted align-bottom">
                <div className="w-20 line-clamp-3 text-center [overflow-wrap:anywhere]" title={c.name}>{c.name}</div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.connector_id}>
              <th scope="row" className="sticky left-0 z-10 bg-surface pr-2 text-left font-medium text-fg-muted">
                <div className="w-28 @lg:w-48 line-clamp-2 [overflow-wrap:anywhere]" title={row.name}>{row.name}</div>
              </th>
              {rows.map((col) => {
                if (col.connector_id === row.connector_id) {
                  return <td key={col.connector_id} className="h-11 w-20 rounded-md bg-surface-raised text-center text-fg-subtle" aria-label="same connector">-</td>
                }
                const n = shared.get(pairKey(row.connector_id, col.connector_id)) ?? 0
                return (
                  <td
                    key={col.connector_id}
                    className="h-11 w-20 rounded-md border border-line text-center leading-tight"
                    style={{ backgroundColor: shade(n, row.exits) }}
                    title={`${n.toLocaleString()} of ${row.name}'s ${row.exits.toLocaleString()} exits (${percentText(n, row.exits)}) were also handed out by ${col.name}`}
                  >
                    <div className={n > 0 ? 'font-semibold tabular-nums text-fg' : 'tabular-nums text-fg-subtle'}>{percentText(n, row.exits)}</div>
                    {n > 0 && <div className="text-[10px] tabular-nums text-fg">{n.toLocaleString()}</div>}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ShadeLegend() {
  return (
    <div className="flex items-center gap-2 text-[11px] text-fg-subtle">
      <span>0%</span>
      <div
        className="h-2 w-32 rounded-full border border-line"
        style={{ background: `linear-gradient(to right, ${shade(1, 1000)}, ${shade(1, 1)})` }}
        aria-hidden
      />
      <span>100% of the row's exits</span>
    </div>
  )
}

function Kpi({ label, value, sub, className }: { label: string; value: string; sub?: string; className?: string }) {
  return (
    <div className={cn('px-3 py-2.5 @lg:px-4 @lg:py-3 flex flex-col gap-1 min-w-0', className)}>
      <div className="text-xs text-fg-muted truncate">{label}</div>
      <div className="text-[17px] leading-6 @lg:text-[21px] @lg:leading-7 font-semibold tabular-nums truncate" title={value}>{value}</div>
      {sub && <div className="text-[11px] text-fg-subtle truncate" title={sub}>{sub}</div>}
    </div>
  )
}
