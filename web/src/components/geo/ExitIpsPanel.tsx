// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { ColumnDef, ColumnFiltersState } from '@tanstack/react-table'
import { AlertTriangle, Network } from 'lucide-react'
import { fetchGeoExitIps, fetchProjectConnectors, fetchProjects, ExitIp, ExitIpFilters, ObservationVerdict } from '../../api/client'
import { DataTable } from '../DataTable'
import { EmptyState } from '../layout/Page'
import { Badge, Card, Select } from '../ui'
import { formatDateTime, relativeTime } from '../../utils/format'
import { HeaderWithTip, TIPS } from './columns'

function verdictOf(e: ExitIp): ObservationVerdict {
  if (e.conflict) return 'contradicted'
  if (e.disagreement) return 'uncertain'
  return e.claimed_country ? 'confirmed' : 'no_claim'
}

const VERDICT_OPTIONS: { value: ObservationVerdict; label: string }[] = [
  { value: 'contradicted', label: 'contradicted' },
  { value: 'uncertain', label: 'uncertain' },
  { value: 'confirmed', label: 'confirmed' },
  { value: 'no_claim', label: 'no claim' },
]

/** The header filters, keyed by column id, as the server's query parameters. */
function toServerFilters(columnFilters: ColumnFiltersState): ExitIpFilters {
  const value = (id: string) => {
    const v = columnFilters.find((f) => f.id === id)?.value
    return typeof v === 'string' && v.trim() ? v.trim() : undefined
  }
  return {
    connector_id: value('connector'),
    ip: value('ip'),
    claimed_country: value('claimed'),
    country: value('resolved'),
    verdict: value('verdict') as ObservationVerdict | undefined,
    proxy_id: value('proxy_id'),
  }
}

/**
 * Distinct exit IPs per connector with the state of their latest observation.
 *
 * The de-duplicated counterpart of the observation log: one row per connector
 * and IP however often it was seen, so restarts and IP changes do not pile up
 * rows. Filters and paging run on the server. Scoped to one project when
 * `projectId` is given; across every project otherwise.
 */
export function ExitIpsPanel({ projectId }: { projectId?: string }) {
  const [projectFilter, setProjectFilter] = useState('')
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([])
  const [pageIndex, setPageIndex] = useState(0)
  const [pageSize, setPageSize] = useState(50)
  const scope = projectId ?? (projectFilter || undefined)

  const [applied, setApplied] = useState<ExitIpFilters>({})
  useEffect(() => {
    const handle = setTimeout(() => setApplied(toServerFilters(columnFilters)), 300)
    return () => clearTimeout(handle)
  }, [columnFilters])
  useEffect(() => { setPageIndex(0) }, [scope, applied, pageSize])

  const filters: ExitIpFilters = { ...applied, project_id: scope }
  const { data, isLoading } = useQuery({
    queryKey: ['geo-exit-ips', filters, pageIndex, pageSize],
    queryFn: () => fetchGeoExitIps({ ...filters, limit: pageSize, offset: pageIndex * pageSize }),
    refetchInterval: 60_000,
    placeholderData: (previous) => previous,
  })

  const { data: projects } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects, enabled: !projectId })
  const connectorProjectIds = scope ? [scope] : (projects?.projects ?? []).map((p) => p.id)
  const connectorQueries = useQueries({
    queries: connectorProjectIds.map((id) => ({ queryKey: ['connectors', id], queryFn: () => fetchProjectConnectors(id) })),
  })
  const connectorOptions = useMemo(
    () => connectorQueries
      .flatMap((q) => q.data?.connectors ?? [])
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((c) => ({ value: c.id, label: c.name })),
    // Keyed on when each query last delivered data: the query objects change identity every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [connectorQueries.map((q) => q.dataUpdatedAt).join()],
  )

  const columns: ColumnDef<ExitIp>[] = useMemo(() => [
    {
      id: 'connector', accessorFn: (e: ExitIp) => e.connector_id, size: 190, header: 'Connector', enableSorting: false,
      meta: { filterVariant: 'select' as const, filterOptions: connectorOptions },
      cell: ({ row }) => {
        const e = row.original
        if (!e.connector_name) {
          return (
            <div className="min-w-0">
              <Badge color="gray" className="py-0 text-[10px]" title={e.connector_id}>deleted connector</Badge>
              <div className="font-mono text-[11px] text-fg-subtle mt-0.5">{e.connector_id.slice(0, 8)}</div>
            </div>
          )
        }
        return (
          <div className="min-w-0">
            <div className="truncate">{e.connector_name}</div>
            {!projectId && e.project_name && <div className="text-[11px] text-fg-muted truncate">{e.project_name}</div>}
          </div>
        )
      },
    },
    { accessorKey: 'ip', header: 'Exit IP', size: 140, enableSorting: false, meta: { filterVariant: 'text' as const }, cell: ({ getValue }) => <span className="font-mono text-xs">{getValue<string>()}</span> },
    { accessorKey: 'first_seen', header: 'First seen', size: 120, enableSorting: false, cell: ({ getValue }) => <span title={formatDateTime(getValue<string>())}>{relativeTime(getValue<string>())}</span> },
    { accessorKey: 'last_seen', header: 'Last seen', size: 120, enableSorting: false, cell: ({ getValue }) => <span title={formatDateTime(getValue<string>())}>{relativeTime(getValue<string>())}</span> },
    { accessorKey: 'sightings', header: () => <HeaderWithTip label="Sightings" tip={TIPS.sightings} />, size: 110, enableSorting: false, meta: { align: 'right' as const }, cell: ({ getValue }) => getValue<number>().toLocaleString() },
    {
      id: 'claimed', accessorFn: (e: ExitIp) => e.claimed_country ?? '', size: 120, enableSorting: false,
      header: () => <HeaderWithTip label="Vendor said" tip={TIPS.vendorSaid} />,
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => <span className="font-mono text-xs">{getValue<string>() || '-'}</span>,
    },
    {
      id: 'resolved', accessorFn: (e: ExitIp) => e.country ?? '', size: 150, enableSorting: false,
      header: () => <HeaderWithTip label="Resolved" tip={TIPS.resolved} />,
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="font-mono text-xs">
          {row.original.country ?? '-'}
          {row.original.resolved_source && <span className="text-fg-subtle font-sans"> via {row.original.resolved_source}</span>}
        </span>
      ),
    },
    {
      id: 'verdict', accessorFn: verdictOf, size: 150, enableSorting: false,
      header: () => <HeaderWithTip label="Verdict" tip={TIPS.latestState} />,
      meta: { filterVariant: 'select' as const, filterOptions: VERDICT_OPTIONS },
      cell: ({ row }) => {
        const v = verdictOf(row.original)
        const seen = row.original.source ? <span className="text-[11px] text-fg-subtle ml-1">via {row.original.source.replace('_', ' ')}</span> : null
        if (v === 'contradicted') return <><Badge color="red" className="inline-flex items-center gap-1"><AlertTriangle className="w-3 h-3" /> contradicted</Badge>{seen}</>
        if (v === 'uncertain') return <><Badge color="yellow">uncertain</Badge>{seen}</>
        return <>{v === 'confirmed' ? <Badge color="green">confirmed</Badge> : <Badge color="gray">no claim</Badge>}{seen}</>
      },
    },
    { accessorKey: 'proxy_id', header: 'Proxy', size: 110, enableSorting: false, meta: { filterVariant: 'text' as const }, cell: ({ getValue }) => <span className="font-mono text-[11px] text-fg-muted" title={getValue<string | null>() ?? undefined}>{(getValue<string | null>() ?? '').slice(0, 8) || '-'}</span> },
  ], [projectId, connectorOptions])

  const rows = data?.ips ?? []
  const filtering = columnFilters.length > 0 || (!projectId && !!projectFilter)
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-fg-muted">Every distinct exit IP a connector has handed out, most recently seen first, with what the latest observation found. One row per IP however often it was seen; the observation log has every sighting. Kept for the exit IP retention window set in Settings → IP attribution. Country and IP filters match exactly.</p>
        {!projectId && (
          <Select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)} className="w-44" aria-label="Project">
            <option value="">All projects</option>
            {(projects?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
        )}
      </div>
      <Card className="p-0 overflow-hidden">
        {!isLoading && rows.length === 0 && !filtering ? (
          <EmptyState icon={<Network className="w-5 h-5" />} title="No exit IPs yet" description="Exits appear once a proxy's IP has been seen: on discovery, a health check, a lookup or preflight. They are written in batches, so allow a minute." />
        ) : (
          <DataTable
            columns={columns}
            data={rows}
            getRowId={(e) => `${e.connector_id}:${e.ip}`}
            emptyMessage="No exit IPs match the filters."
            manualFiltering={{ columnFilters, onColumnFiltersChange: setColumnFilters }}
            manualPagination={{ pageIndex, pageSize, total: data?.total ?? rows.length, onPageChange: setPageIndex, onPageSizeChange: setPageSize }}
          />
        )}
      </Card>
    </div>
  )
}
