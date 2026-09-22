// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { ColumnDef, ColumnFiltersState } from '@tanstack/react-table'
import { AlertTriangle, Globe } from 'lucide-react'
import { fetchGeoObservations, fetchProjectConnectors, fetchProjects, IpObservation, ObservationFilters, ObservationVerdict } from '../../api/client'
import { DataTable } from '../DataTable'
import { EmptyState } from '../layout/Page'
import { Badge, Card, Select } from '../ui'
import { formatDateTime, relativeTime } from '../../utils/format'
import { HeaderWithTip, TIPS } from './columns'

function verdictOf(o: IpObservation): ObservationVerdict {
  if (o.conflict) return 'contradicted'
  if (o.disagreement) return 'uncertain'
  return o.claimed_country ? 'confirmed' : 'no_claim'
}

const SOURCE_OPTIONS = ['discovery', 'health_check', 'geo_lookup', 'manual', 'preflight', 'reattribute'].map((s) => ({ value: s, label: s.replace('_', ' ') }))
const VERDICT_OPTIONS: { value: ObservationVerdict; label: string }[] = [
  { value: 'contradicted', label: 'contradicted' },
  { value: 'uncertain', label: 'uncertain' },
  { value: 'confirmed', label: 'confirmed' },
  { value: 'no_claim', label: 'no claim' },
]

/** The header filters, keyed by column id, as the server's query parameters. */
function toServerFilters(columnFilters: ColumnFiltersState): ObservationFilters {
  const value = (id: string) => {
    const v = columnFilters.find((f) => f.id === id)?.value
    return typeof v === 'string' && v.trim() ? v.trim() : undefined
  }
  return {
    connector_id: value('connector'),
    source: value('source'),
    ip: value('ip'),
    claimed_country: value('claimed'),
    resolved_country: value('resolved'),
    verdict: value('verdict') as ObservationVerdict | undefined,
    proxy_id: value('proxy_id'),
  }
}

/**
 * Exit IP observations, newest first, one page at a time.
 *
 * The same header filters as every other table, but the table behind this
 * can hold millions of rows, so the filters and the paging run on the
 * server and the browser only ever holds the page it shows. Scoped to one
 * project when `projectId` is given; across every project, with a project
 * filter and the project shown under the connector, otherwise.
 */
export function ObservationsPanel({ projectId }: { projectId?: string }) {
  const [projectFilter, setProjectFilter] = useState('')
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([])
  const [pageIndex, setPageIndex] = useState(0)
  const [pageSize, setPageSize] = useState(50)
  const scope = projectId ?? (projectFilter || undefined)

  // Typed filters should not fire a request per keystroke.
  const [applied, setApplied] = useState<ObservationFilters>({})
  useEffect(() => {
    const handle = setTimeout(() => setApplied(toServerFilters(columnFilters)), 300)
    return () => clearTimeout(handle)
  }, [columnFilters])
  // Any filter change starts from the first page again.
  useEffect(() => { setPageIndex(0) }, [scope, applied, pageSize])

  const filters: ObservationFilters = { ...applied, project_id: scope }
  const { data, isLoading } = useQuery({
    queryKey: ['geo-observations', filters, pageIndex, pageSize],
    queryFn: () => fetchGeoObservations({ ...filters, limit: pageSize, offset: pageIndex * pageSize }),
    refetchInterval: 30_000,
    placeholderData: (previous) => previous,
  })

  // Connector choices: the scoped project's connectors, or every project's when unscoped.
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [connectorQueries.map((q) => q.data).join()],
  )

  const columns: ColumnDef<IpObservation>[] = useMemo(() => [
    { accessorKey: 'observed_at', header: 'When', size: 130, enableSorting: false, cell: ({ getValue }) => <span title={formatDateTime(getValue<string>())}>{relativeTime(getValue<string>())}</span> },
    {
      id: 'source', accessorFn: (o: IpObservation) => o.source, size: 130, enableSorting: false,
      header: () => <HeaderWithTip label="Source" tip={TIPS.source} />,
      meta: { filterVariant: 'select' as const, filterOptions: SOURCE_OPTIONS },
      cell: ({ getValue }) => <span className="text-xs">{getValue<string>().replace('_', ' ')}</span>,
    },
    {
      id: 'connector', accessorFn: (o: IpObservation) => o.connector_id ?? '', size: 190, header: 'Connector', enableSorting: false,
      meta: { filterVariant: 'select' as const, filterOptions: connectorOptions },
      cell: ({ row }) => {
        const o = row.original
        if (!o.connector_id) return <span className="text-fg-subtle">-</span>
        if (!o.connector_name) {
          // The connector is gone; the raw history outlives it until retention.
          return (
            <div className="min-w-0">
              <Badge color="gray" className="py-0 text-[10px]" title={o.connector_id}>deleted connector</Badge>
              <div className="font-mono text-[11px] text-fg-subtle mt-0.5">{o.connector_id.slice(0, 8)}</div>
            </div>
          )
        }
        return (
          <div className="min-w-0">
            <div className="truncate">{o.connector_name}</div>
            {!projectId && o.project_name && <div className="text-[11px] text-fg-muted truncate">{o.project_name}</div>}
          </div>
        )
      },
    },
    { accessorKey: 'ip', header: 'Exit IP', size: 140, enableSorting: false, meta: { filterVariant: 'text' as const }, cell: ({ getValue }) => <span className="font-mono text-xs">{getValue<string>()}</span> },
    {
      id: 'claimed', accessorFn: (o: IpObservation) => o.claimed_country ?? '', size: 120, enableSorting: false,
      header: () => <HeaderWithTip label="Vendor said" tip={TIPS.vendorSaid} />,
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => <span className="font-mono text-xs">{getValue<string>() || '-'}</span>,
    },
    {
      id: 'resolved', accessorFn: (o: IpObservation) => o.resolved_country ?? '', size: 150, enableSorting: false,
      header: () => <HeaderWithTip label="Resolved" tip={TIPS.resolved} />,
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="font-mono text-xs">
          {row.original.resolved_country ?? '-'}
          {row.original.resolved_source && <span className="text-fg-subtle font-sans"> via {row.original.resolved_source}</span>}
        </span>
      ),
    },
    {
      id: 'verdict', accessorFn: verdictOf, size: 150, enableSorting: false,
      header: () => <HeaderWithTip label="Verdict" tip={TIPS.verdict} />,
      meta: { filterVariant: 'select' as const, filterOptions: VERDICT_OPTIONS },
      cell: ({ row }) => {
        const v = verdictOf(row.original)
        if (v === 'contradicted') return <Badge color="red" className="inline-flex items-center gap-1"><AlertTriangle className="w-3 h-3" /> contradicted</Badge>
        if (v === 'uncertain') return <Badge color="yellow">uncertain</Badge>
        return v === 'confirmed' ? <Badge color="green">confirmed</Badge> : <Badge color="gray">no claim</Badge>
      },
    },
    { accessorKey: 'proxy_id', header: 'Proxy', size: 110, enableSorting: false, meta: { filterVariant: 'text' as const }, cell: ({ getValue }) => <span className="font-mono text-[11px] text-fg-muted" title={getValue<string | null>() ?? undefined}>{(getValue<string | null>() ?? '').slice(0, 8) || '-'}</span> },
  ], [projectId, connectorOptions])

  const rows = data?.observations ?? []
  const filtering = columnFilters.length > 0 || (!projectId && !!projectFilter)
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-fg-muted">Every time an exit IP was seen behind a proxy, newest first. Raw rows follow the retention window set in Settings → IP attribution. Country and IP filters match exactly.</p>
        {!projectId && (
          <Select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)} className="w-44" aria-label="Project">
            <option value="">All projects</option>
            {(projects?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
        )}
      </div>
      <Card className="p-0 overflow-hidden">
        {!isLoading && rows.length === 0 && !filtering ? (
          <EmptyState icon={<Globe className="w-5 h-5" />} title="No observations yet" description="Observations arrive as proxies are discovered, health-checked, located or preflighted. They are written in batches, so allow a minute." />
        ) : (
          <DataTable
            columns={columns}
            data={rows}
            getRowId={(o) => String(o.id)}
            emptyMessage="No observations match the filters."
            manualFiltering={{ columnFilters, onColumnFiltersChange: setColumnFilters }}
            manualPagination={{ pageIndex, pageSize, total: data?.total ?? rows.length, onPageChange: setPageIndex, onPageSizeChange: setPageSize }}
          />
        )}
      </Card>
    </div>
  )
}
