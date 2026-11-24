// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { ColumnDef, ColumnFiltersState } from '@tanstack/react-table'
import { Network } from 'lucide-react'
import { countGeoExitIps, fetchExitReputationSummary, fetchGeoExitIps, fetchProjectConnectors, fetchProjects, ExitIp, ExitIpFilters, ExitIpReputationSummary, ObservationVerdict } from '../../api/client'
import { useCountAll } from '../../hooks/useCountAll'
import { useProject } from '../../contexts/ProjectContext'
import { DataTable, type DataTableFeatures } from '../DataTable'
import { EmptyState } from '../layout/Page'
import { Badge, Card, Select } from '../ui'
import { formatDateTime, relativeTime } from '../../utils/format'
import { HeaderWithTip, TIPS, VerdictCell, countryVerdict, placeLabel } from './columns'
import { endLabel } from './ExitIpReputation'
import { filterPatch, filterValues, linkedFilters, useDropGoneConnector, type FilterValues } from './headerFilters'
import { AdminLink } from '../AdminLink'

const VERDICT_OPTIONS: { value: ObservationVerdict; label: string }[] = [
  { value: 'contradicted', label: 'contradicted' },
  { value: 'open', label: 'open' },
  { value: 'confirmed', label: 'confirmed' },
  { value: 'no_claim', label: 'no claim' },
]

/** The header filters by column id, as a link carries them too. */
export const EXIT_IP_FILTERS = ['connector', 'ip', 'claimed', 'resolved', 'verdict', 'proxy_id'] as const
// A link's select filters hold only what their selects offer.
const LINKED_CHOICES = { verdict: VERDICT_OPTIONS.map((o) => o.value) }

/** The applied header filters, keyed by column id, as the server's query parameters. */
function toServerFilters(values: FilterValues): ExitIpFilters {
  return {
    connector_id: values.connector,
    ip: values.ip,
    claimed_country: values.claimed,
    country: values.resolved,
    verdict: values.verdict as ObservationVerdict | undefined,
    proxy_id: values.proxy_id,
  }
}

/**
 * Distinct exit IPs per connector with the state of their latest observation.
 *
 * The de-duplicated counterpart of the observation log: one row per connector
 * and IP however often it was seen, so restarts and IP changes do not pile up
 * rows. Filters and paging run on the server, which counts no further than
 * 10,000 rows past the page; counting everything is an explicit request.
 * Scoped to one project when `projectId` is given; across every project
 * otherwise.
 */
export function ExitIpsPanel({ projectId, initialFilters, onFilters }: {
  projectId?: string
  /** The header filters a link opened the table with, by column id (EXIT_IP_FILTERS); read once. */
  initialFilters?: Record<string, string>
  /** Hears the header filters once applied, each of EXIT_IP_FILTERS set or null, not every keystroke. */
  onFilters?: (applied: Record<string, string | null>) => void
}) {
  const [projectFilter, setProjectFilter] = useState('')
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>(() => linkedFilters(initialFilters, EXIT_IP_FILTERS, LINKED_CHOICES))
  const [pageIndex, setPageIndex] = useState(0)
  const [pageSize, setPageSize] = useState(50)
  const scope = projectId ?? (projectFilter || undefined)

  // A link's filters apply at once: through the debounce the table would load unfiltered first.
  const [applied, setApplied] = useState<FilterValues>(() => filterValues(columnFilters))
  useEffect(() => {
    const handle = setTimeout(() => setApplied(filterValues(columnFilters)), 300)
    return () => clearTimeout(handle)
  }, [columnFilters])
  useEffect(() => { setPageIndex(0) }, [scope, applied, pageSize])
  useEffect(() => { onFilters?.(filterPatch(applied, EXIT_IP_FILTERS)) }, [applied])

  const filters: ExitIpFilters = { ...toServerFilters(applied), project_id: scope }
  const { data, isLoading, isPlaceholderData } = useQuery({
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
    [connectorQueries.map((q) => q.dataUpdatedAt).join()],
  )
  const connectorsLoaded = (!!scope || !!projects) && connectorQueries.every((q) => q.isSuccess && !q.isFetching)
  useDropGoneConnector(initialFilters?.connector?.trim(), connectorsLoaded ? connectorOptions : null, () => {
    const next = columnFilters.filter((f) => f.id !== 'connector')
    setColumnFilters(next)
    setApplied(filterValues(next))
  })

  // Exit reputation per IP, for a project that records it: read once the page is in, for the page's IPs.
  const { selectedProject } = useProject()
  const reputation = !!projectId && selectedProject?.id === projectId && selectedProject.exit_reputation !== 'off'
  // In record mode the decisions are made and shown, but routing does not act on them.
  const recordOnly = reputation && selectedProject?.exit_reputation === 'record'
  const pageIps = useMemo(() => [...new Set((data?.ips ?? []).map((e) => e.ip))], [data])
  const summaryQuery = useQuery({
    queryKey: ['exit-reputation-summary', projectId, pageIps],
    queryFn: () => fetchExitReputationSummary(projectId!, pageIps),
    enabled: reputation && pageIps.length > 0,
    refetchInterval: 60_000,
    placeholderData: (previous) => previous,
  })
  const summaries = summaryQuery.data
  const byIp = useMemo(() => new Map((summaries ?? []).map((s) => [s.ip, s])), [summaries])
  // An IP left out of the answer has no record; until the page's answer is in, or when it failed, nobody knows yet.
  const summaryState: SummaryState = summaries === undefined
    ? (summaryQuery.isError ? 'failed' : 'loading')
    : summaryQuery.isPlaceholderData ? 'loading' : 'ready'

  const columns: ColumnDef<DataTableFeatures, ExitIp>[] = useMemo(() => [
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
      id: 'claimed', accessorFn: (e: ExitIp) => e.claimed_country ?? '', size: 150, enableSorting: false,
      header: () => <HeaderWithTip label="Vendor said" tip={TIPS.vendorSaid} />,
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => <span className="font-mono text-xs">{placeLabel(row.original.claimed_country, row.original.claimed_state, row.original.claimed_city)}</span>,
    },
    {
      id: 'resolved', accessorFn: (e: ExitIp) => e.country ?? '', size: 180, enableSorting: false,
      header: () => <HeaderWithTip label="Resolved" tip={TIPS.resolved} />,
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <div className="min-w-0">
          <div className="font-mono text-xs truncate">{placeLabel(row.original.country, row.original.claimed_state ? row.original.resolved_state : null, row.original.claimed_city ? row.original.resolved_city : null)}</div>
          {row.original.resolved_source && <div className="text-[11px] text-fg-subtle">via {row.original.resolved_source}</div>}
        </div>
      ),
    },
    {
      id: 'verdict', accessorFn: countryVerdict, size: 160, enableSorting: false,
      header: () => <HeaderWithTip label="Verdict" tip={TIPS.latestState} />,
      meta: { filterVariant: 'select' as const, filterOptions: VERDICT_OPTIONS },
      cell: ({ row }) => <VerdictCell row={row.original} note={row.original.source ? `via ${row.original.source.replace('_', ' ')}` : undefined} />,
    },
    ...(reputation ? reputationColumns(byIp, summaryState, recordOnly) : []),
    { accessorKey: 'proxy_id', header: 'Proxy', size: 110, enableSorting: false, meta: { filterVariant: 'text' as const }, cell: ({ getValue }) => <span className="font-mono text-[11px] text-fg-muted" title={getValue<string | null>() ?? undefined}>{(getValue<string | null>() ?? '').slice(0, 8) || '-'}</span> },
  ], [projectId, connectorOptions, reputation, recordOnly, byIp, summaryState])

  const rows = data?.ips ?? []
  const totals = useCountAll('geo-exit-ips-count', filters, countGeoExitIps, data, rows.length)
  const filtering = columnFilters.length > 0 || (!projectId && !!projectFilter)
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-fg-muted">Every distinct exit IP a connector has handed out, most recently seen first, with what the latest observation found. One row per IP however often it was seen; the observation log has every sighting. Kept for the exit IP retention window set in <AdminLink page="geo" query="tab=policy">IP attribution</AdminLink>. Country and IP filters match exactly.</p>
        {!projectId && (
          <Select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)} className="w-44" aria-label="Project">
            <option value="">All projects</option>
            {(projects?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
        )}
      </div>
      <Card className="p-0 overflow-hidden">
        {!isLoading && rows.length === 0 && !filtering ? (
          <EmptyState icon={<Network className="w-5 h-5" />} reloading={isPlaceholderData} title="No exit IPs yet" description="Exits appear once a proxy's IP has been seen: on discovery, a health check, a lookup or preflight. They are written in batches, so allow a minute." />
        ) : (
          <DataTable
            columns={columns}
            data={rows}
            getRowId={(e) => `${e.connector_id}:${e.ip}`}
            loading={isLoading}
            reloading={isPlaceholderData}
            emptyMessage="No exit IPs match the filters."
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

const REPUTATION_TIPS = {
  reports: 'Good and bad exit reports about this IP, over every site, for the life of its records. The Reputation tab has them per site.',
  avoided: 'Sites that avoid this IP now, after bad reports; the time is when the last avoid ends.',
  avoidedRecord: 'Sites that would avoid this IP under avoid; in record mode routing does not act on them. The time is when the last avoid ends.',
}

/** Where the page's summary read stands: still out (or out again for a new page), failed, or answered. */
type SummaryState = 'loading' | 'failed' | 'ready'

/** A cell whose IP the summary left out: no record once answered, blank while the read is out, "?" when it failed. */
function NoSummary({ state }: { state: SummaryState }) {
  if (state === 'loading') return null
  if (state === 'failed') return <span className="text-fg-subtle" title="The exit reputation could not be loaded">?</span>
  return <span className="text-fg-subtle">-</span>
}

/** The exit reputation columns: the IP's reports and the sites avoiding it, from one summary read for the page's IPs. */
function reputationColumns(byIp: Map<string, ExitIpReputationSummary>, state: SummaryState, recordOnly: boolean): ColumnDef<DataTableFeatures, ExitIp>[] {
  return [
    {
      id: 'reports', header: () => <HeaderWithTip label="Reports" tip={REPUTATION_TIPS.reports} />, size: 110, enableSorting: false, meta: { align: 'right' as const },
      cell: ({ row }) => {
        const s = byIp.get(row.original.ip)
        if (!s) return <NoSummary state={state} />
        if (s.good_reports + s.bad_reports === 0) return <span className="text-fg-subtle">-</span>
        return (
          <span title={`${s.good_reports.toLocaleString()} good, ${s.bad_reports.toLocaleString()} bad, over ${s.sites} ${s.sites === 1 ? 'site' : 'sites'}`}>
            <span className={s.good_reports ? 'text-success' : 'text-fg-subtle'}>{s.good_reports.toLocaleString()}</span>
            <span className="text-fg-subtle"> / </span>
            <span className={s.bad_reports ? 'text-danger' : 'text-fg-subtle'}>{s.bad_reports.toLocaleString()}</span>
          </span>
        )
      },
    },
    {
      id: 'avoided', header: () => <HeaderWithTip label="Avoided" tip={recordOnly ? REPUTATION_TIPS.avoidedRecord : REPUTATION_TIPS.avoided} />, size: 130, enableSorting: false,
      cell: ({ row }) => {
        const s = byIp.get(row.original.ip)
        if (!s) return <NoSummary state={state} />
        if (s.avoided_sites === 0) return <span className="text-fg-subtle">-</span>
        return (
          <Badge color="red" className="whitespace-nowrap py-0.5" title={s.avoided_until ? `Until ${endLabel(s.avoided_until)} at the latest` : undefined}>
            {s.avoided_sites} {s.avoided_sites === 1 ? 'site' : 'sites'}
          </Badge>
        )
      },
    },
  ]
}
