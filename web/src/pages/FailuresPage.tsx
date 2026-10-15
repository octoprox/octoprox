// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { AlertTriangle, Trash2 } from 'lucide-react'
import { clearFailures, fetchFailures, FailureRecord } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { DataTable } from '../components/DataTable'
import { Page, EmptyState } from '../components/layout/Page'
import { StatusCodeBadge } from '../components/mitm/RequestDetail'
import { FailurePanel, GROUP_INFO, KindBadge, formatWhen, kindInfo } from '../components/failures/FailureDetail'
import { Alert, Button, Card, CardHeader, ConfirmDialog } from '../components/ui'
import { compact, formatDateTime, plural } from '../utils/format'
import { cn } from '../utils/cn'

/** Entries per request; older ones load on demand. */
const PAGE_SIZE = 500

/** Failures by kind across the loaded entries, a tally's breakdown included. */
function countByKind(records: FailureRecord[]): [string, number][] {
  const counts = new Map<string, number>()
  for (const r of records) {
    if (r.kind === 'suppressed' && r.breakdown) {
      for (const [kind, n] of Object.entries(r.breakdown)) counts.set(kind, (counts.get(kind) ?? 0) + n)
    } else {
      counts.set(r.kind, (counts.get(r.kind) ?? 0) + r.count)
    }
  }
  return [...counts].sort((a, b) => b[1] - a[1])
}

function retentionLabel(hours: number): string {
  return hours % 24 === 0 ? `${hours / 24} ${plural(hours / 24, 'day', 'days')}` : `${hours} ${plural(hours, 'hour', 'hours')}`
}

export default function FailuresPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId, selectedProject } = useProject()
  const { canMutate } = useAuth()
  const toast = useToast()
  const [openId, setOpenId] = useState<string | null>(null)
  const [kindFilter, setKindFilter] = useState<string | null>(null)
  const [confirmClear, setConfirmClear] = useState(false)

  const { data, isLoading, isError, error, fetchNextPage, hasNextPage, isFetchingNextPage } = useInfiniteQuery({
    queryKey: ['failures', selectedProjectId],
    queryFn: ({ pageParam }) => fetchFailures(selectedProjectId!, PAGE_SIZE, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    enabled: !!selectedProjectId,
    refetchInterval: 10000,
  })

  const clearMutation = useMutation({
    mutationFn: () => clearFailures(selectedProjectId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['failures', selectedProjectId] })
      setOpenId(null)
      setKindFilter(null)
      setConfirmClear(false)
      toast.show('Failure log cleared')
    },
    onError: (e: Error) => { setConfirmClear(false); toast.show(e.message || 'Failed to clear the failure log', 'error') },
  })

  const records = useMemo(() => data?.pages.flatMap((p) => p.records) ?? [], [data])
  const meta = data?.pages[0]
  const byKind = useMemo(() => countByKind(records), [records])
  const failureTotal = byKind.reduce((sum, [, n]) => sum + n, 0)
  const largest = byKind.length > 0 ? byKind[0][1] : 0
  const oldest = records.length > 0 ? records[records.length - 1].first_at : null

  const shown = useMemo(
    () => (kindFilter ? records.filter((r) => r.kind === kindFilter || (r.kind === 'suppressed' && !!r.breakdown?.[kindFilter])) : records),
    [records, kindFilter],
  )
  const open = openId ? records.find((r) => r.id === openId) ?? null : null

  const columns: ColumnDef<FailureRecord, unknown>[] = useMemo(() => [
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
      cell: ({ row }) => (row.original.kind === 'suppressed' ? <span className="text-fg-subtle">-</span> : (
        <span className="inline-flex items-center gap-1.5">
          <StatusCodeBadge code={row.original.status} />
          {row.original.upstream_status != null && <span className="text-[11px] text-fg-subtle whitespace-nowrap" title="The vendor's own status">vendor {row.original.upstream_status}</span>}
        </span>
      )),
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
      accessorKey: 'message',
      header: 'Message',
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => <span className="text-xs text-fg-muted truncate block" title={getValue<string>()}>{getValue<string>()}</span>,
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

  return (
    <Page
      title="Failures"
      count={meta ? meta.total : undefined}
      subtitle={selectedProject ? `Why requests through ${selectedProject.name} failed or were refused, newest first` : undefined}
      actions={canMutate && records.length > 0 ? (
        <Button variant="outline" size="sm" onClick={() => setConfirmClear(true)} disabled={clearMutation.isPending}>
          <Trash2 className="w-3.5 h-3.5" /> Clear
        </Button>
      ) : undefined}
      panel={open ? <FailurePanel key={open.id} record={open} onClose={() => setOpenId(null)} /> : null}
    >
      {meta && !meta.enabled && (
        <Alert variant="warning">
          The failure log is switched off (<code className="font-mono text-xs">failure_log.enabled</code>), so nothing new is recorded. What is shown was logged before it was turned off.
        </Alert>
      )}
      {isError && (
        <Alert variant="error">Could not load the failure log{error instanceof Error && error.message ? `: ${error.message}` : ''}.</Alert>
      )}

      {isLoading ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : records.length === 0 ? (
        <EmptyState
          icon={<AlertTriangle />}
          title="No failures logged"
          description="Requests that fail or are refused show up here within a few seconds, with what went wrong: the vendor's answer, a timeout, no proxy for the location, and so on."
        />
      ) : (
        <>
          <Card className="p-4">
            <CardHeader
              title={<>What failed <span className="text-fg-subtle font-normal">· {compact(failureTotal)} {plural(failureTotal, 'failure', 'failures')}{oldest ? ` since ${formatWhen(oldest)}` : ''}</span></>}
              action={kindFilter ? <button type="button" onClick={() => setKindFilter(null)} className="text-xs text-primary font-medium hover:brightness-110">Show all kinds</button> : undefined}
              className="mb-3"
            />
            <div className="grid grid-cols-1 @3xl:grid-cols-2 gap-x-6 gap-y-1">
              {byKind.map(([kind, n]) => {
                const info = kindInfo(kind)
                const active = kindFilter === kind
                return (
                  <button
                    key={kind}
                    type="button"
                    onClick={() => setKindFilter(active ? null : kind)}
                    className={cn(
                      'flex items-center gap-2.5 rounded-md px-2 py-1.5 -mx-2 text-left transition-colors min-w-0',
                      active ? 'bg-primary-soft' : 'hover:bg-surface-raised',
                      kindFilter && !active && 'opacity-50',
                    )}
                    title={GROUP_INFO[info.group].label}
                  >
                    <span className="inline-block w-2 h-2 rounded-full flex-none" style={{ background: GROUP_INFO[info.group].dot }} />
                    <span className="text-[13px] text-fg w-32 @2xl:w-44 flex-none truncate">{info.label}</span>
                    <span className="flex-1 h-1.5 rounded-full bg-surface-raised overflow-hidden min-w-[24px]">
                      <span className="block h-full rounded-full" style={{ width: `${Math.max(2, (n / largest) * 100)}%`, background: GROUP_INFO[info.group].dot }} />
                    </span>
                    <span className="text-[13px] tabular-nums font-medium w-12 text-right flex-none">{compact(n)}</span>
                  </button>
                )
              })}
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 mt-3 text-[11px] text-fg-subtle">
              {(['upstream', 'refused', 'intercepted'] as const).map((g) => (
                <span key={g} className="inline-flex items-center gap-1.5" title={GROUP_INFO[g].note}>
                  <span className="inline-block w-2 h-2 rounded-full" style={{ background: GROUP_INFO[g].dot }} />
                  {GROUP_INFO[g].label}{g === 'upstream' ? ' (counted on Overview)' : ''}
                </span>
              ))}
            </div>
          </Card>

          <DataTable
            columns={columns}
            data={shown}
            defaultPageSize={50}
            enableColumnFilters
            getRowId={(row) => row.id}
            onRowClick={(row) => setOpenId(row.id)}
            activeRowId={openId}
            columnVisibility={open ? { message: false, connector: false } : {}}
            emptyMessage="No failures match the current filters."
          />

          {meta && (
            <div className="flex items-center justify-between flex-wrap gap-2 text-xs text-fg-subtle">
              <span>
                {records.length.toLocaleString()} of {meta.total.toLocaleString()} {plural(meta.total, 'entry', 'entries')} loaded.
                {' '}The log keeps each project's last {meta.max_entries.toLocaleString()} entries for {retentionLabel(meta.retention_hours)}; identical failures within a few seconds share one entry.
              </span>
              {hasNextPage && (
                <Button variant="outline" size="sm" onClick={() => fetchNextPage()} disabled={isFetchingNextPage}>
                  {isFetchingNextPage ? 'Loading…' : 'Load older'}
                </Button>
              )}
            </div>
          )}
        </>
      )}
      {confirmClear && (
        <ConfirmDialog
          title="Clear the failure log?"
          message="Every logged failure for this project is discarded. The request counts on Overview are not affected, and new failures keep being logged."
          confirmLabel="Clear"
          onCancel={() => setConfirmClear(false)}
          onConfirm={() => clearMutation.mutate()}
          isLoading={clearMutation.isPending}
        />
      )}
    </Page>
  )
}
