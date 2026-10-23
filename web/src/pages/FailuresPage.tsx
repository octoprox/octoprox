// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Why a project's requests failed. Two sources, kept apart on purpose:
 *
 * - "What failed" reads the failure counts from the metrics history: every
 *   failure, for the selected time range, refreshed every half minute.
 * - "Examples" reads the failure log: one row per distinct failure, kept
 *   per kind, which every gateway adds to. It does not refresh under the
 *   reader: new or recurring examples are announced and loaded on request,
 *   and an opened example stays open whatever arrives.
 */

import { useEffect, useMemo, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { AlertTriangle, ArrowUp, Trash2, X } from 'lucide-react'
import {
  clearFailures, fetchFailureHead, fetchFailures, fetchFailureSummary,
  FailureRecord, FailureSummary, HISTORY_RANGES,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { useTheme } from '../contexts/ThemeContext'
import { formatRangeTick, rangeDomain, useTimeRange } from '../contexts/TimeRangeContext'
import { DataTable, type DataTableFeatures } from '../components/DataTable'
import { Page, EmptyState } from '../components/layout/Page'
import { StatusCodeBadge } from '../components/mitm/RequestDetail'
import {
  FailurePanel, GROUP_INFO, GROUP_ORDER, KindBadge, formatWhen, kindInfo, type FailureGroup,
} from '../components/failures/FailureDetail'
import { Alert, Button, Card, ConfirmDialog, InfoTip, Segmented } from '../components/ui'
import { compact, formatDateTime, parseApiDate, plural } from '../utils/format'
import { cn } from '../utils/cn'

/** Examples per request; older ones load on demand. */
const PAGE_SIZE = 200

function retentionLabel(hours: number): string {
  return hours % 24 === 0 ? `${hours / 24} ${plural(hours / 24, 'day', 'days')}` : `${hours} ${plural(hours, 'hour', 'hours')}`
}

type ChartPoint = { time: number } & Record<string, number>

/** One row per bucket with a column per kind, so the bars stack. Kinds are plain identifiers, safe as data keys. */
function chartData(summary: FailureSummary | undefined): ChartPoint[] {
  if (!summary || summary.series.length === 0) return []
  const rows = summary.series[0].points.map((p) => ({ time: parseApiDate(p.timestamp)?.getTime() ?? 0 }) as ChartPoint)
  for (const series of summary.series) {
    series.points.forEach((p, i) => { rows[i][series.kind] = p.count })
  }
  return rows
}

export default function FailuresPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId, selectedProject } = useProject()
  const { canMutate } = useAuth()
  const toast = useToast()
  const { isDark } = useTheme()
  const [range, setRange] = useTimeRange(HISTORY_RANGES)
  // The example itself, not its id: it stays open even once newer ones push it off the loaded pages.
  const [open, setOpen] = useState<FailureRecord | null>(null)
  const [kindFilter, setKindFilter] = useState<string | null>(null)
  const [confirmClear, setConfirmClear] = useState(false)
  // Held as a record, it would otherwise stay open over another project's page (back/forward between projects).
  useEffect(() => { setOpen(null) }, [selectedProjectId])

  const summaryQuery = useQuery({
    queryKey: ['failure-summary', selectedProjectId, range],
    queryFn: () => fetchFailureSummary(selectedProjectId!, range),
    enabled: !!selectedProjectId,
    refetchInterval: 30000,
    placeholderData: (previous) => previous,
  })
  const summary = summaryQuery.data

  const examplesKey = ['failures', selectedProjectId, kindFilter] as const
  const examples = useInfiniteQuery({
    queryKey: examplesKey,
    queryFn: ({ pageParam }) => fetchFailures(selectedProjectId!, { kind: kindFilter, count: PAGE_SIZE, cursor: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    enabled: !!selectedProjectId,
    // Loaded once and kept: the table only changes when the reader asks for newer examples.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })
  const records = useMemo(() => examples.data?.pages.flatMap((p) => p.records) ?? [], [examples.data])
  // An example keeps its id while it recurs: once reloaded, the open one shows its latest count and times.
  const shown = useMemo(() => (open ? (records.find((r) => r.id === open.id) ?? open) : null), [open, records])
  const meta = examples.data?.pages[0]
  // Keyed by the load it is set against, and only polled once that load is
  // in: a head fetched for an earlier load is never shown against this one.
  const head = useQuery({
    queryKey: ['failures-head', selectedProjectId, kindFilter, meta?.newest],
    queryFn: () => fetchFailureHead(selectedProjectId!, meta!.newest, kindFilter),
    enabled: !!selectedProjectId && !!meta,
    refetchInterval: 10000,
  })
  // How many examples are new, or happened again, since the first page was loaded.
  const newer = head.data?.changed ?? 0

  // Heads kept for earlier loads are emptied too, so a key that comes round again starts without stale data.
  const resetHeads = () => queryClient.resetQueries({ queryKey: ['failures-head', selectedProjectId] })

  const showNewest = () => {
    queryClient.resetQueries({ queryKey: examplesKey })
    resetHeads()
  }

  const clearMutation = useMutation({
    mutationFn: () => clearFailures(selectedProjectId!),
    onSuccess: () => {
      queryClient.resetQueries({ queryKey: ['failures', selectedProjectId] })
      resetHeads()
      setOpen(null)
      setConfirmClear(false)
      toast.show('Failure examples cleared')
    },
    onError: (e: Error) => { setConfirmClear(false); toast.show(e.message || 'Failed to clear the failure log', 'error') },
  })

  const columns: ColumnDef<DataTableFeatures, FailureRecord, unknown>[] = useMemo(() => [
    {
      accessorKey: 'last_at',
      header: 'Time',
      size: 136,
      cell: ({ getValue }) => <span className="font-mono text-xs text-fg-muted whitespace-nowrap" title={formatDateTime(getValue<string>())}>{formatWhen(getValue<string>())}</span>,
    },
    {
      id: 'kind',
      header: 'Kind',
      size: 190,
      accessorFn: (row) => kindInfo(row.kind).label,
      enableColumnFilter: false,
      cell: ({ row }) => <KindBadge kind={row.original.kind} />,
    },
    {
      accessorKey: 'status',
      header: 'Status',
      size: 132,
      filterFn: 'equalsString',
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-1.5">
          <StatusCodeBadge code={row.original.status} />
          {row.original.upstream_status != null && <span className="text-[11px] text-fg-subtle whitespace-nowrap" title="The vendor's own status">vendor {row.original.upstream_status}</span>}
        </span>
      ),
    },
    {
      id: 'target',
      header: 'Target',
      size: 220,
      accessorFn: (row) => row.target ?? '',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => {
        const r = row.original
        if (!r.target) return <span className="text-fg-subtle">-</span>
        return (
          <div className="min-w-0">
            <div className="font-mono text-xs truncate" title={r.target}>
              {r.method && r.method !== 'CONNECT' && <span className="text-fg-muted">{r.method} </span>}{r.target}
            </div>
            {r.peer_id && <div className="text-[11px] text-fg-subtle truncate">device {r.peer_name || r.peer_id}</div>}
          </div>
        )
      },
    },
    {
      id: 'connector',
      header: 'Connector',
      size: 180,
      accessorFn: (row) => row.connector_name ?? '',
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => {
        const r = row.original
        if (!r.connector_id) return <span className="text-fg-subtle">-</span>
        return (
          <div className="min-w-0">
            <div className="text-xs truncate">{r.connector_name || 'deleted connector'}</div>
            {r.proxy && <div className="font-mono text-[11px] text-fg-subtle truncate" title={r.proxy}>{r.proxy}</div>}
          </div>
        )
      },
    },
    {
      id: 'message',
      header: 'Message',
      // The vendor's own words follow ours, so the filter finds either.
      accessorFn: (row) => (row.upstream_message ? `${row.message} · ${row.upstream_message}` : row.message),
      meta: { filterVariant: 'text' as const },
      cell: ({ row, getValue }) => (
        <span className="text-xs text-fg-muted truncate block" title={getValue<string>()}>
          {row.original.message}
          {row.original.upstream_message && <span className="text-fg"> · {row.original.upstream_message}</span>}
        </span>
      ),
    },
    {
      accessorKey: 'count',
      header: 'Count',
      size: 80,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => {
        const n = getValue<number>()
        return <span className={cn('text-xs tabular-nums', n > 1 ? 'font-semibold' : 'text-fg-subtle')}>{n > 1 ? `×${compact(n)}` : '1'}</span>
      },
    },
  ], [])

  // The empty state replaces the summary and its range control, so the page
  // header carries the range then: the counts outlive the examples, and a
  // wider range can still have some.
  const nothingAtAll = !summaryQuery.isLoading && !examples.isLoading && summary?.total === 0 && records.length === 0 && !kindFilter
  const loadError = summaryQuery.error ?? examples.error

  return (
    <Page
      title="Failures"
      subtitle={selectedProject ? `Why requests through ${selectedProject.name} failed or were refused` : undefined}
      actions={nothingAtAll ? (
        <Segmented options={HISTORY_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} size="sm" />
      ) : undefined}
      panel={shown ? <FailurePanel key={shown.id} record={shown} onClose={() => setOpen(null)} /> : null}
    >
      {meta && !meta.enabled && (
        <Alert variant="warning">
          The failure log is switched off (<code className="font-mono text-xs">failure_log.enabled</code>), so nothing new is counted or kept. What is shown was recorded before it was turned off.
        </Alert>
      )}
      {loadError && (
        <Alert variant="error">Could not load the failures{loadError instanceof Error && loadError.message ? `: ${loadError.message}` : ''}.</Alert>
      )}

      {nothingAtAll ? (
        <EmptyState
          icon={<AlertTriangle />}
          title="No failures"
          description={`Nothing failed in the last ${range}. Requests that fail or are refused are counted here, with examples of what went wrong: the vendor's answer, a timeout, no proxy for the location, and so on.`}
        />
      ) : (
        <>
          <WhatFailed
            summary={summary}
            updatedAt={summaryQuery.dataUpdatedAt}
            loading={summaryQuery.isLoading}
            failed={!!summaryQuery.error}
            range={range}
            onRange={setRange}
            isDark={isDark}
            kindFilter={kindFilter}
            onKind={(kind) => setKindFilter((current) => (current === kind ? null : kind))}
          />

          <section className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-3 h-8">
              <div className="flex items-center gap-2 min-w-0">
                <h2 className="text-sm font-semibold text-fg flex-none">Examples</h2>
                {kindFilter ? (
                  <button
                    type="button"
                    onClick={() => setKindFilter(null)}
                    className="inline-flex items-center gap-1 rounded-full bg-primary-soft text-primary-soft-fg px-2 py-0.5 text-xs font-medium min-w-0 truncate"
                    title="Show every kind"
                  >
                    {kindInfo(kindFilter).label} <X className="w-3 h-3" />
                  </button>
                ) : (
                  <span className="text-xs text-fg-subtle truncate">last seen first, every kind</span>
                )}
              </div>
              {newer > 0 && (
                <Button variant="outline" size="sm" onClick={showNewest} className="flex-none">
                  <ArrowUp className="w-3.5 h-3.5" /> {newer > 999 ? '999+' : newer.toLocaleString()} new or recurring, show
                </Button>
              )}
            </div>

            {examples.isLoading ? (
              <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
            ) : (
              <DataTable
                columns={columns}
                data={records}
                defaultPageSize={50}
                enableColumnFilters
                getRowId={(row) => row.id}
                onRowClick={(row) => setOpen(row)}
                activeRowId={open?.id ?? null}
                columnVisibility={open ? { message: false, connector: false } : {}}
                emptyMessage={kindFilter ? `No examples of ${kindInfo(kindFilter).label.toLowerCase()} are kept right now.` : 'No examples kept right now.'}
              />
            )}

            {meta && (
              <div className="flex items-center justify-between flex-wrap gap-2 text-xs text-fg-subtle">
                <span>
                  {records.length.toLocaleString()} of {meta.total.toLocaleString()} {plural(meta.total, 'example', 'examples')} loaded.
                  {' '}Identical failures share one example, which counts them and moves up each time they happen again. The {meta.max_entries.toLocaleString()} seen last of each kind are kept, each until it has not happened for {retentionLabel(meta.retention_hours)}. The counts above include every failure.
                </span>
                <span className="flex items-center gap-2">
                  {examples.hasNextPage && (
                    <Button variant="outline" size="sm" onClick={() => examples.fetchNextPage()} disabled={examples.isFetchingNextPage}>
                      {examples.isFetchingNextPage ? 'Loading…' : 'Load older'}
                    </Button>
                  )}
                  {canMutate && records.length > 0 && (
                    <Button variant="outline" size="sm" onClick={() => setConfirmClear(true)} disabled={clearMutation.isPending}>
                      <Trash2 className="w-3.5 h-3.5" /> Clear examples
                    </Button>
                  )}
                </span>
              </div>
            )}
          </section>
        </>
      )}
      {confirmClear && (
        <ConfirmDialog
          title="Clear the failure examples?"
          message="Every kept example for this project is discarded. The counts are part of the metrics history and stay, and new failures keep being recorded."
          confirmLabel="Clear"
          onCancel={() => setConfirmClear(false)}
          onConfirm={() => clearMutation.mutate()}
          isLoading={clearMutation.isPending}
        />
      )}
    </Page>
  )
}

/** The counted summary: kinds by group with the vendor's statuses, failures over time by kind, and by connector. */
function WhatFailed({ summary, updatedAt, loading, failed, range, onRange, isDark, kindFilter, onKind }: {
  summary: FailureSummary | undefined
  /** When the summary arrived; the chart's axis ends there. */
  updatedAt: number
  loading: boolean
  failed: boolean
  range: (typeof HISTORY_RANGES)[number]
  onRange: (range: (typeof HISTORY_RANGES)[number]) => void
  isDark: boolean
  kindFilter: string | null
  onKind: (kind: string) => void
}) {
  const data = useMemo(() => chartData(summary), [summary])
  // Anchored to when the data arrived, so every refetch moves the axis with the window.
  const timeDomain = useMemo(() => rangeDomain(range, updatedAt), [range, updatedAt])
  const largest = summary && summary.kinds.length > 0 ? summary.kinds[0].count : 0
  const groups = useMemo(() => {
    const byGroup = new Map<FailureGroup, FailureSummary['kinds']>()
    for (const k of summary?.kinds ?? []) {
      const group = kindInfo(k.kind).group
      byGroup.set(group, [...(byGroup.get(group) ?? []), k])
    }
    return GROUP_ORDER.filter((g) => byGroup.has(g)).map((g) => ({ group: g, kinds: byGroup.get(g)! }))
  }, [summary])
  const gridColor = isDark ? '#374151' : '#e5e7eb'
  const tickColor = '#9ca3af'
  const tooltipStyle = isDark
    ? { backgroundColor: '#1f2937', border: '1px solid #374151', color: '#f3f4f6', borderRadius: 8, fontSize: 12 }
    : { borderRadius: 8, fontSize: 12, border: '1px solid #e5e7eb' }

  // Nothing in this card wraps or grows with its content: a docked example
  // narrows the page, and a card that grew taller then would push the
  // examples down under the reader's pointer. The body has a fixed height
  // and the kind list scrolls if it ever outgrows it.
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-1.5 min-w-0">
          <h2 className="text-sm font-semibold text-fg flex-none">What failed</h2>
          <InfoTip>
            Every failure is counted, from the metrics history, so the numbers trail live traffic by up to a minute. Upstream failures are what Overview counts as failures; requests refused by Octoprox are not counted there. Click a kind to see its examples.
          </InfoTip>
        </div>
        <Segmented options={HISTORY_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={onRange} size="sm" className="flex-none" />
      </div>
      {loading && !summary ? (
        <div className="h-[260px] text-sm text-fg-muted flex items-center justify-center">Loading…</div>
      ) : !summary && failed ? (
        <div className="h-[260px] text-sm text-fg-subtle flex items-center justify-center">The counts could not be loaded.</div>
      ) : !summary || summary.total === 0 ? (
        <div className="h-[260px] text-sm text-fg-subtle flex items-center justify-center">No failures counted in the last {range}.</div>
      ) : (
        <div className="grid grid-cols-1 @md:grid-cols-[minmax(240px,2fr)_minmax(0,3fr)] gap-x-6 gap-y-4 @md:h-[260px]">
          <div className="flex flex-col gap-3 min-w-0 @md:overflow-y-auto @md:pr-1">
            <div className="text-[12.5px] text-fg-muted truncate flex-none">
              <span className="font-semibold text-fg tabular-nums">{summary.total.toLocaleString()}</span> {plural(summary.total, 'failure', 'failures')} in the last {range}
            </div>
            {groups.map(({ group, kinds }) => (
              <div key={group} className="min-w-0">
                <div className="flex items-baseline justify-between gap-2 mb-0.5" title={GROUP_INFO[group].note}>
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle truncate">
                    {GROUP_INFO[group].heading}
                    {group === 'upstream' && <span className="normal-case tracking-normal font-normal"> · counted on Overview</span>}
                  </h3>
                  <span className="text-[11px] tabular-nums text-fg-subtle flex-none">{compact(kinds.reduce((s, k) => s + k.count, 0))}</span>
                </div>
                {kinds.map((k) => {
                  const info = kindInfo(k.kind)
                  const active = kindFilter === k.kind
                  return (
                    <button
                      key={k.kind}
                      type="button"
                      onClick={() => onKind(k.kind)}
                      className={cn(
                        'w-full flex flex-col rounded-md px-2 py-1 -mx-2 text-left transition-colors min-w-0',
                        active ? 'bg-primary-soft' : 'hover:bg-surface-raised',
                        kindFilter && !active && 'opacity-50',
                      )}
                      title={active ? 'Show examples of every kind' : `Show examples of ${info.label.toLowerCase()}`}
                    >
                      <span className="flex items-center gap-2.5 min-w-0 w-full">
                        <span className="inline-block w-2 h-2 rounded-full flex-none" style={{ background: info.color }} />
                        <span className="text-[13px] text-fg w-32 @2xl:w-44 flex-none truncate">{info.label}</span>
                        <span className="flex-1 h-1.5 rounded-full bg-surface-raised overflow-hidden min-w-[24px]">
                          <span className="block h-full rounded-full" style={{ width: `${Math.max(2, (k.count / largest) * 100)}%`, background: info.color }} />
                        </span>
                        <span className="text-[13px] tabular-nums font-medium w-12 text-right flex-none">{compact(k.count)}</span>
                      </span>
                      {k.upstream_statuses.length > 0 && (
                        <span className="pl-[18px] text-[11px] text-fg-subtle truncate">
                          vendor {k.upstream_statuses.map((s) => `${s.status} ×${compact(s.count)}`).join(' · ')}
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            ))}
            {summary.connectors.length > 0 && (
              <div className="min-w-0 pt-2 border-t border-line">
                <h3 className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle mb-0.5">By connector</h3>
                {summary.connectors.map((c) => (
                  <div key={c.connector_id ?? 'none'} className="flex items-center justify-between gap-3 py-0.5 text-[12.5px]">
                    <span className={cn('truncate', c.connector_id ? 'text-fg' : 'text-fg-muted italic')}>
                      {c.connector_id ? (c.name || 'deleted connector') : 'no proxy chosen'}
                    </span>
                    <span className="tabular-nums text-fg-muted flex-none">{compact(c.count)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="min-w-0 h-[220px] @md:h-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} margin={{ top: 4, right: 4, left: -12, bottom: 0 }} barCategoryGap={1} aria-label="Failures over time">
                <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
                <XAxis dataKey="time" type="number" scale="time" domain={timeDomain} tickFormatter={(v: number) => formatRangeTick(v, range)} tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false} minTickGap={48} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false} tickFormatter={(v: number) => compact(v)} />
                <Tooltip
                  labelFormatter={(v) => new Date(Number(v)).toLocaleString()}
                  formatter={(v, kind) => [Number(v).toLocaleString(), kindInfo(String(kind)).label]}
                  contentStyle={tooltipStyle}
                  cursor={{ fill: isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)' }}
                  itemSorter={(item) => -(item.value as number)}
                />
                {[...summary.series].reverse().map((s) => (
                  <Bar
                    key={s.kind}
                    dataKey={s.kind}
                    name={s.kind}
                    stackId="kinds"
                    fill={kindInfo(s.kind).color}
                    fillOpacity={kindFilter && kindFilter !== s.kind ? 0.25 : 0.9}
                    isAnimationActive={false}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </Card>
  )
}
