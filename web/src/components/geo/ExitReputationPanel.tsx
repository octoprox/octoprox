// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useContext, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { ShieldAlert } from 'lucide-react'
import {
  fetchExitReputation, fetchExitReputationConnectors,
  type ExitReputationFilters, type ExitReputationMode, type ExitReputationRecord, type ReputationSort, type ReputationStatusFilter,
} from '../../api/client'
import { DataTable, type DataTableFeatures } from '../DataTable'
import { EmptyState, ProjectSettingsContext } from '../layout/Page'
import { Alert, Button, Card, Input, LogoLoader, Select } from '../ui'
import { LoadError, loadFailed } from '../ui/LoadError'
import { cn } from '../../utils/cn'
import { DOCS_URL } from '../../utils/docs'
import { formatDateTime, relativeTime } from '../../utils/format'
import { HeaderWithTip } from './columns'
import { ConnectorsBySite } from './ConnectorsBySite'
import { ReputationPill } from './ExitIpReputation'

const STATUS_OPTIONS: { value: ReputationStatusFilter; label: string }[] = [
  { value: 'any', label: 'Any status' },
  { value: 'avoided', label: 'Avoided' },
  { value: 'none', label: 'Not avoided' },
]

const SORT_OPTIONS: { value: ReputationSort; label: string }[] = [
  { value: 'last_seen', label: 'Last seen' },
  { value: 'reports', label: 'Most reports' },
]

// Filters and their button: 16 px text and 44 px targets on touch screens, compact under a mouse.
const FILTER_CONTROL = 'py-1.5 text-base pointer-fine:text-[13px] pointer-coarse:h-11'

const TIPS = {
  reports: 'Good and bad exit reports your clients sent about this IP for this site, over the record\'s life. The inspector shows the recent ones, faded by Remember for, that decide.',
  status: (
    <>
      <b>Avoided</b>: kept away from the site until the time shown, by bad reports.<br />
      Statuses are as of now.
    </>
  ),
}

/** The row a record is: its IP and site. */
export const recordId = (r: Pick<ExitReputationRecord, 'ip' | 'site'>) => `${r.ip}|${r.site}`

/** The filters as typed: nothing reloads until they are applied. */
interface Draft { ip: string; site: string; status: ReputationStatusFilter; sort: ReputationSort }

function toFilters(draft: Draft): ExitReputationFilters {
  return {
    ip: draft.ip.trim() || undefined,
    site: draft.site.trim() || undefined,
    status: draft.status === 'any' ? undefined : draft.status,
    sort: draft.sort === 'last_seen' ? undefined : draft.sort,
  }
}

const same = (a: ExitReputationFilters, b: ExitReputationFilters) =>
  a.ip === b.ip && a.site === b.site && a.status === b.status && a.sort === b.sort

/** IP, site, status and order, applied together on Apply or Enter rather than on every key. */
function ReputationFilters({ applied, onApply }: { applied: ExitReputationFilters; onApply: (filters: ExitReputationFilters) => void }) {
  const [draft, setDraft] = useState<Draft>({ ip: applied.ip ?? '', site: applied.site ?? '', status: applied.status ?? 'any', sort: applied.sort ?? 'last_seen' })
  const typed = toFilters(draft)
  const changed = !same(typed, applied)
  return (
    <form
      // Two to a row on a phone, one on the narrowest, in a line once there is room.
      className="grid grid-cols-2 @max-[18rem]:grid-cols-1 gap-2 @2xl:flex @2xl:flex-wrap @2xl:items-center"
      onSubmit={(e) => { e.preventDefault(); onApply(typed) }}
      aria-label="Filter the records"
    >
      <Input
        value={draft.ip}
        onChange={(e) => setDraft({ ...draft, ip: e.target.value })}
        placeholder="IP or its start"
        aria-label="IP"
        maxLength={45}
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        className={cn('min-w-0 px-3 font-mono placeholder:font-sans @2xl:w-44', FILTER_CONTROL)}
      />
      <Input
        value={draft.site}
        onChange={(e) => setDraft({ ...draft, site: e.target.value })}
        placeholder="Site"
        aria-label="Site"
        maxLength={253}
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        className={cn('min-w-0 px-3 @2xl:w-44', FILTER_CONTROL)}
      />
      <Select aria-label="Status" value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value as ReputationStatusFilter })} className={cn('min-w-0 px-3 @2xl:w-36', FILTER_CONTROL)}>
        {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </Select>
      <Select aria-label="Order" value={draft.sort} onChange={(e) => setDraft({ ...draft, sort: e.target.value as ReputationSort })} className={cn('min-w-0 px-3 @2xl:w-40', FILTER_CONTROL)}>
        {SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </Select>
      <Button type="submit" size="sm" variant={changed ? 'primary' : 'secondary'} disabled={!changed} className="col-span-2 @max-[18rem]:col-span-1 pointer-coarse:h-11">
        Apply
      </Button>
    </form>
  )
}

/** Above records kept while the mode is off: nothing new comes, and where to turn it on (a button for those who may). */
function OffNote() {
  const openSettings = useContext(ProjectSettingsContext)
  return (
    <Alert variant="info" className="text-[13px] flex flex-wrap items-center gap-x-3 gap-y-2">
      <span className="flex-1 min-w-[min(100%,16rem)]">
        Exit reputation is off for this project: nothing new is recorded and exit reports are refused. These records are what was kept before
        {openSettings ? '.' : <>; turn it on in <b>Project settings, Exit reputation tab</b>.</>}
      </span>
      {openSettings && (
        <Button type="button" size="sm" variant="outline" onClick={() => openSettings('reputation')} className="pointer-coarse:h-11">Turn it on</Button>
      )}
    </Alert>
  )
}

const DOCS = `${DOCS_URL}/exit-reputation`
const ACTION = 'inline-flex items-center justify-center h-8 px-3 rounded-lg text-[13px] font-medium pointer-coarse:h-11'

/** What an empty tab offers: the project's Exit reputation settings (for those who may change them) and the guide. */
function EmptyActions({ settings, docs, docsLabel }: { settings: string; docs: string; docsLabel: string }) {
  const openSettings = useContext(ProjectSettingsContext)
  return (
    <div className="flex flex-wrap justify-center gap-2">
      {openSettings && <Button type="button" size="sm" onClick={() => openSettings('reputation')} className="pointer-coarse:h-11">{settings}</Button>}
      <a href={docs} target="_blank" rel="noreferrer" className={cn(ACTION, 'border border-line-strong bg-surface text-fg hover:bg-surface-raised')}>{docsLabel}</a>
    </div>
  )
}

/**
 * What the project's exit reputation holds: every connector against the busiest sites, and one row per exit IP and
 * site of the connectors with IPs, what clients reported about the IP on the site and the status that makes now. The
 * records' filters, order and paging run on the server, which counts no further than 10,000 rows past the page. A row
 * opens every site of its IP in the inspector.
 */
export function ExitReputationPanel({ projectId, mode, inspecting, onInspect }: {
  projectId: string
  mode: ExitReputationMode
  /** The row open in the inspector. */
  inspecting: string | null
  onInspect: (record: ExitReputationRecord) => void
}) {
  const [applied, setApplied] = useState<ExitReputationFilters>({})
  const [pageIndex, setPageIndex] = useState(0)
  const [pageSize, setPageSize] = useState(50)
  const query = useQuery({
    queryKey: ['exit-reputation', projectId, applied, pageIndex, pageSize],
    queryFn: () => fetchExitReputation(projectId, { ...applied, limit: pageSize, offset: pageIndex * pageSize }),
    // Reports arrive and ends pass while the page is open.
    refetchInterval: 60_000,
    placeholderData: (previous) => previous,
  })
  const { data, isLoading, isPlaceholderData } = query
  // The place the connectors' cells are for (a LocationTarget key); none is the requests that name no location.
  const [place, setPlace] = useState('')
  const bySite = useQuery({
    queryKey: ['exit-reputation-connectors', projectId, place],
    queryFn: () => fetchExitReputationConnectors(projectId, place || undefined),
    refetchInterval: 60_000,
    placeholderData: (previous) => previous,
  })

  const columns: ColumnDef<DataTableFeatures, ExitReputationRecord>[] = useMemo(() => [
    { id: 'ip', accessorKey: 'ip', header: 'Exit IP', size: 140, enableSorting: false, cell: ({ getValue }) => <span className="font-mono text-xs" title={getValue<string>()}>{getValue<string>()}</span> },
    { id: 'site', accessorKey: 'site', header: 'Site', enableSorting: false, meta: { minWidth: 150 }, cell: ({ getValue }) => <span title={getValue<string>()}>{getValue<string>()}</span> },
    {
      id: 'status', header: () => <HeaderWithTip label="Status" tip={TIPS.status} />, size: 210, enableSorting: false,
      cell: ({ row }) => <ReputationPill record={row.original} />,
    },
    {
      id: 'reports', header: () => <HeaderWithTip label="Reports" tip={TIPS.reports} />, size: 112, enableSorting: false, meta: { align: 'right' as const },
      cell: ({ row }) => (
        <span title={`${row.original.good_reports.toLocaleString()} good, ${row.original.bad_reports.toLocaleString()} bad`}>
          <span className={row.original.good_reports ? 'text-success' : 'text-fg-subtle'}>{row.original.good_reports.toLocaleString()}</span>
          <span className="text-fg-subtle"> / </span>
          <span className={row.original.bad_reports ? 'text-danger' : 'text-fg-subtle'}>{row.original.bad_reports.toLocaleString()}</span>
        </span>
      ),
    },
    {
      id: 'last_report', header: 'Last report', size: 170, enableSorting: false,
      cell: ({ row }) => row.original.last_report_at ? (
        <span title={`${formatDateTime(row.original.last_report_at)}${row.original.last_reason ? ` · ${row.original.last_reason}` : ''}`}>
          {relativeTime(row.original.last_report_at)}
          {row.original.last_reason && <span className="text-fg-muted"> · {row.original.last_reason}</span>}
        </span>
      ) : <span className="text-fg-subtle">-</span>,
    },
    { id: 'last_seen', accessorKey: 'last_seen_at', header: 'Last seen', size: 112, enableSorting: false, cell: ({ getValue }) => <span title={formatDateTime(getValue<string>())}>{relativeTime(getValue<string>())}</span> },
  ], [])

  const rows = data?.records ?? []
  const filtering = Object.values(applied).some((value) => value !== undefined)
  const apply = (filters: ExitReputationFilters) => {
    setApplied(filters)
    setPageIndex(0)
  }
  const hasRecords = rows.length > 0 || filtering
  // A place picked keeps the table up even when nothing is kept for it.
  const hasConnectors = (bySite.data?.sites.length ?? 0) > 0 || place !== '' || loadFailed(bySite)
  // Both first reads are in (or failed): until then nothing says whether the tab is empty.
  const settled = (data !== undefined || loadFailed(query)) && (bySite.data !== undefined || loadFailed(bySite))
  const nothing = settled && !loadFailed(query) && !hasRecords && !hasConnectors
  return (
    <div className="space-y-3">
      <p className="text-xs text-fg-muted">
        What your clients reported about each site: how each connector fares there and, for connectors with IPs, each exit IP. A site is the exact host, so www.google.com and google.com are two sites. <a href={`${DOCS}#views`} target="_blank" rel="noreferrer" className="text-primary hover:brightness-110">How it works →</a>
      </p>
      {mode === 'off' && (hasRecords || hasConnectors) && <OffNote />}
      {mode === 'record' && (hasRecords || hasConnectors) && (
        <p className="text-xs text-fg-muted">The project records only: these are what <b>Avoid</b> would act on, and routing does not use them yet.</p>
      )}
      {!settled ? (
        <Card><LogoLoader className="py-10" /></Card>
      ) : nothing ? (
        mode === 'off' ? (
          <EmptyState
            icon={<ShieldAlert />}
            reloading={isPlaceholderData}
            title="Exit reputation is off"
            description="Nothing is recorded for this project and exit reports are refused. Turn it on in the project's Exit reputation settings to weigh connectors by what your clients report per site, and to keep exit IPs a site blocked away from it."
            action={<EmptyActions settings="Turn it on" docs={DOCS} docsLabel="What it does" />}
          />
        ) : (
          <EmptyState
            icon={<ShieldAlert />}
            reloading={isPlaceholderData}
            title="No records yet"
            description="Records appear when your clients send exit reports through the API: per connector and site for every connector, and per exit IP and site for connectors with IPs. They are written in batches, so allow a minute."
            action={<EmptyActions settings="Exit reputation settings" docs={`${DOCS}#reporting-exits`} docsLabel="How to report exits" />}
          />
        )
      ) : (
        <>
          {hasConnectors && <ConnectorsBySite query={bySite} place={place} onPlace={setPlace} avoids={mode === 'avoid'} />}
          <div className="space-y-1 pt-1">
            <h3 className="text-sm font-semibold text-fg">Exit IPs by site</h3>
            <p className="text-xs text-fg-muted">One row per exit IP and site of the connectors with IPs: what your clients reported, and where that leaves the IP now. Click a row for every site of its IP.</p>
          </div>
          {/* Nothing to filter until a record exists. */}
          {hasRecords && <ReputationFilters applied={applied} onApply={apply} />}
          <Card className="p-0 overflow-hidden">
            {loadFailed(query) ? (
              <LoadError what="the records" query={query} />
            ) : !hasRecords ? (
              <p className="text-xs text-fg-subtle px-4 py-3">
                No exit IP records yet. They come from reports about requests through connectors with IPs (static lists, ISP and datacenter ports, cloud instances, pooled slots); a dynamic connector is weighed as a whole, above.
              </p>
            ) : (
              <DataTable
                columns={columns}
                data={rows}
                getRowId={recordId}
                loading={isLoading}
                reloading={isPlaceholderData}
                emptyMessage="No records match the filters."
                onRowClick={onInspect}
                activeRowId={inspecting}
                // Beside an open inspector the table keeps to the IP, the site and where it stands.
                columnVisibility={{ last_report: !inspecting }}
                manualPagination={{
                  pageIndex,
                  pageSize,
                  total: data?.total ?? 0,
                  totalExact: data?.total_exact ?? true,
                  onPageChange: setPageIndex,
                  onPageSizeChange: (size) => { setPageSize(size); setPageIndex(0) },
                }}
              />
            )}
          </Card>
        </>
      )}
    </div>
  )
}
