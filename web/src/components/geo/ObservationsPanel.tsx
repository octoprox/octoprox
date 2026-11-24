// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { ColumnDef, ColumnFiltersState } from '@tanstack/react-table'
import { Globe } from 'lucide-react'
import { countGeoObservations, fetchGeoObservations, fetchProjectConnectors, fetchProjects, IpObservation, ObservationFilters, ObservationVerdict } from '../../api/client'
import { useCountAll } from '../../hooks/useCountAll'
import { DataTable, type DataTableFeatures } from '../DataTable'
import { EmptyState } from '../layout/Page'
import { Alert, Badge, Card, Select } from '../ui'
import { formatDateTime, relativeTime } from '../../utils/format'
import { HeaderWithTip, TIPS, VerdictCell, countryVerdict, placeLabel } from './columns'
import { UntilPicker } from './UntilPicker'
import { filterPatch, filterValues, linkedFilters, useDropGoneConnector, type FilterValues } from './headerFilters'
import { AdminLink } from '../AdminLink'

const SOURCE_OPTIONS = ['discovery', 'health_check', 'geo_lookup', 'manual', 'preflight'].map((s) => ({ value: s, label: s.replace('_', ' ') }))
const VERDICT_OPTIONS: { value: ObservationVerdict; label: string }[] = [
  { value: 'contradicted', label: 'contradicted' },
  { value: 'open', label: 'open' },
  { value: 'confirmed', label: 'confirmed' },
  { value: 'no_claim', label: 'no claim' },
]

const HEADER_FILTERS = ['connector', 'source', 'ip', 'claimed', 'resolved', 'verdict', 'proxy_id'] as const
/** The header filters by column id and Until (an ISO time), as a link carries them too. */
export const OBSERVATION_FILTERS = [...HEADER_FILTERS, 'until'] as const
// A link's select filters hold only what their selects offer.
const LINKED_CHOICES = { verdict: VERDICT_OPTIONS.map((o) => o.value), source: SOURCE_OPTIONS.map((o) => o.value) }

/** The applied header filters, keyed by column id, as the server's query parameters. */
function toServerFilters(values: FilterValues): ObservationFilters {
  return {
    connector_id: values.connector,
    source: values.source,
    ip: values.ip,
    claimed_country: values.claimed,
    resolved_country: values.resolved,
    verdict: values.verdict as ObservationVerdict | undefined,
    proxy_id: values.proxy_id,
  }
}

/** A linked Until as an ISO time, or undefined (now) when it does not read as one. */
function linkedUntil(value: string | undefined): string | undefined {
  const at = value ? Date.parse(value) : NaN
  return Number.isNaN(at) ? undefined : new Date(at).toISOString()
}

/**
 * Exit IP observations, newest first, one page at a time.
 *
 * The same header filters as every other table, but the table behind this
 * can hold millions of rows, so the filters and the paging run on the
 * server and the browser only ever holds the page it shows. The server
 * counts no further than 10,000 rows past the page, so the total can be a
 * lower bound; counting everything is a separate, explicit request, and
 * "Until" reaches back in time without paging there. Scoped to one project
 * when `projectId` is given; across every project, with a project filter
 * and the project shown under the connector, otherwise.
 */
export function ObservationsPanel({ projectId, initialFilters, onFilters }: {
  projectId?: string
  /** The header filters and Until a link opened the log with, by column id (OBSERVATION_FILTERS); read once. */
  initialFilters?: Record<string, string>
  /** Hears the filters once applied, each of OBSERVATION_FILTERS set or null, not every keystroke. */
  onFilters?: (applied: Record<string, string | null>) => void
}) {
  const [projectFilter, setProjectFilter] = useState('')
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>(() => linkedFilters(initialFilters, HEADER_FILTERS, LINKED_CHOICES))
  const [pageIndex, setPageIndex] = useState(0)
  const [pageSize, setPageSize] = useState(50)
  const [until, setUntil] = useState<string | undefined>(() => linkedUntil(initialFilters?.until))
  const scope = projectId ?? (projectFilter || undefined)

  // Typed filters should not fire a request per keystroke. A link's apply at once: through the debounce the log
  // would load unfiltered first.
  const [applied, setApplied] = useState<FilterValues>(() => filterValues(columnFilters))
  useEffect(() => {
    const handle = setTimeout(() => setApplied(filterValues(columnFilters)), 300)
    return () => clearTimeout(handle)
  }, [columnFilters])
  // Any filter change starts from the first page again.
  useEffect(() => { setPageIndex(0) }, [scope, applied, pageSize, until])
  useEffect(() => { onFilters?.({ ...filterPatch(applied, HEADER_FILTERS), until: until ?? null }) }, [applied, until])

  const filters: ObservationFilters = { ...toServerFilters(applied), project_id: scope, until }
  // A page the server called off for its time limit is not asked for again
  // until the filters or the page change: each try reads for the whole limit.
  const { data, isLoading, isPlaceholderData, isError, error } = useQuery({
    queryKey: ['geo-observations', filters, pageIndex, pageSize],
    queryFn: () => fetchGeoObservations({ ...filters, limit: pageSize, offset: pageIndex * pageSize }),
    refetchInterval: (query) => (query.state.status === 'error' ? false : 30_000),
    retry: false,
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
    // Keyed on when each query last delivered data: the query objects change identity every render.
    [connectorQueries.map((q) => q.dataUpdatedAt).join()],
  )
  const connectorsLoaded = (!!scope || !!projects) && connectorQueries.every((q) => q.isSuccess && !q.isFetching)
  useDropGoneConnector(initialFilters?.connector?.trim(), connectorsLoaded ? connectorOptions : null, () => {
    const next = columnFilters.filter((f) => f.id !== 'connector')
    setColumnFilters(next)
    setApplied(filterValues(next))
  })

  const columns: ColumnDef<DataTableFeatures, IpObservation>[] = useMemo(() => [
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
      id: 'claimed', accessorFn: (o: IpObservation) => o.claimed_country ?? '', size: 150, enableSorting: false,
      header: () => <HeaderWithTip label="Vendor said" tip={TIPS.vendorSaid} />,
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => <span className="font-mono text-xs">{placeLabel(row.original.claimed_country, row.original.claimed_state, row.original.claimed_city)}</span>,
    },
    {
      id: 'resolved', accessorFn: (o: IpObservation) => o.resolved_country ?? '', size: 180, enableSorting: false,
      header: () => <HeaderWithTip label="Resolved" tip={TIPS.resolved} />,
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <div className="min-w-0">
          <div className="font-mono text-xs truncate">{placeLabel(row.original.resolved_country, row.original.claimed_state ? row.original.resolved_state : null, row.original.claimed_city ? row.original.resolved_city : null)}</div>
          {row.original.resolved_source && <div className="text-[11px] text-fg-subtle">via {row.original.resolved_source}</div>}
        </div>
      ),
    },
    {
      id: 'verdict', accessorFn: countryVerdict, size: 160, enableSorting: false,
      header: () => <HeaderWithTip label="Verdict" tip={TIPS.verdict} />,
      meta: { filterVariant: 'select' as const, filterOptions: VERDICT_OPTIONS },
      cell: ({ row }) => <VerdictCell row={row.original} />,
    },
    { accessorKey: 'proxy_id', header: 'Proxy', size: 110, enableSorting: false, meta: { filterVariant: 'text' as const }, cell: ({ getValue }) => <span className="font-mono text-[11px] text-fg-muted" title={getValue<string | null>() ?? undefined}>{(getValue<string | null>() ?? '').slice(0, 8) || '-'}</span> },
  ], [projectId, connectorOptions])

  const rows = data?.observations ?? []
  const totals = useCountAll('geo-observations-count', filters, countGeoObservations, data, rows.length)
  const filtering = columnFilters.length > 0 || (!projectId && !!projectFilter) || !!until
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-fg-muted">Every time an exit IP was seen behind a proxy, newest first. Raw rows follow the retention window set in <AdminLink page="geo" query="tab=policy">IP attribution</AdminLink>, unless a project sets its own. Country and IP filters match exactly; Until shows the log up to an earlier moment.</p>
        <div className="flex items-center gap-2 flex-wrap max-w-full">
          {/* The picker's presets are wider than a phone: they scroll sideways in their own row, not the page. On touch
              screens its buttons and fields get 44 px targets and 16 px text, as on Activity. */}
          <div className="max-w-full overflow-x-auto [&_[role=radio]]:whitespace-nowrap pointer-coarse:[&_button]:min-h-11 pointer-coarse:[&_input]:h-11 pointer-coarse:[&_input]:text-base">
            <UntilPicker initial={until} onChange={setUntil} />
          </div>
          {!projectId && (
            <Select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)} className="w-44" aria-label="Project">
              <option value="">All projects</option>
              {(projects?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
          )}
        </div>
      </div>
      {isError && (
        <Alert variant="error">Could not load the observation log{error instanceof Error && error.message ? `: ${error.message}` : ''}</Alert>
      )}
      <Card className="p-0 overflow-hidden">
        {!isLoading && !isError && rows.length === 0 && !filtering ? (
          <EmptyState icon={<Globe className="w-5 h-5" />} reloading={isPlaceholderData} title="No observations yet" description="Observations arrive as proxies are discovered, health-checked, located or preflighted. They are written in batches, so allow a minute." />
        ) : (
          <DataTable
            columns={columns}
            data={rows}
            getRowId={(o) => String(o.id)}
            loading={isLoading}
            reloading={isPlaceholderData}
            emptyMessage="No observations match the filters."
            manualFiltering={{ columnFilters, onColumnFiltersChange: setColumnFilters }}
            manualPagination={{
              pageIndex,
              pageSize,
              ...totals,
              onPageChange: setPageIndex,
              onPageSizeChange: setPageSize,
            }}
          />
        )}
      </Card>
    </div>
  )
}
