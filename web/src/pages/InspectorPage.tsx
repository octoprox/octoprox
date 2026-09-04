// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Trash2, Eye } from 'lucide-react'
import { fetchMitmRequests, clearMitmRequests, MitmRequestRecord } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { DataTable } from '../components/DataTable'
import { Page, EmptyState } from '../components/layout/Page'
import { MethodBadge, StatusCodeBadge, RequestPanel } from '../components/mitm/RequestDetail'
import { formatBytes, formatTime } from '../utils/format'
import { Button, ConfirmDialog } from '../components/ui'

export default function InspectorPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { canMutate } = useAuth()
  const toast = useToast()
  const [openId, setOpenId] = useState<string | null>(null)
  const [confirmClear, setConfirmClear] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['mitm-requests', selectedProjectId],
    queryFn: () => fetchMitmRequests(selectedProjectId!, 200),
    enabled: !!selectedProjectId,
    refetchInterval: 5000,
  })

  const clearMutation = useMutation({
    mutationFn: () => clearMitmRequests(selectedProjectId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mitm-requests', selectedProjectId] })
      setOpenId(null)
      setConfirmClear(false)
      toast.show('Captured requests cleared')
    },
    onError: (e: Error) => { setConfirmClear(false); toast.show(e.message || 'Failed to clear requests', 'error') },
  })

  const columns: ColumnDef<MitmRequestRecord, unknown>[] = useMemo(() => [
    {
      accessorKey: 'timestamp',
      header: 'Time',
      size: 90,
      cell: ({ getValue }) => <span className="font-mono text-xs text-fg-muted">{formatTime(getValue<string>())}</span>,
    },
    {
      accessorKey: 'method',
      header: 'Method',
      size: 84,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <MethodBadge method={getValue<string>()} />,
    },
    {
      accessorKey: 'status_code',
      header: 'Status',
      size: 80,
      filterFn: 'equalsString',
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <StatusCodeBadge code={getValue<number>()} />,
    },
    {
      accessorKey: 'target_host',
      header: 'Host',
      size: 180,
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => <span className="font-mono text-xs truncate block" title={getValue<string>()}>{getValue<string>()}</span>,
    },
    {
      id: 'path',
      header: 'Path',
      accessorFn: (row) => { try { const u = new URL(row.url); return u.pathname + u.search } catch { return row.url } },
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => <span className="font-mono text-xs truncate block" title={getValue<string>()}>{getValue<string>()}</span>,
    },
    {
      accessorKey: 'latency_ms',
      header: 'Latency',
      size: 90,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => <span className="text-xs">{Math.round(getValue<number>())} ms</span>,
    },
    {
      id: 'size',
      header: 'Req / Res',
      size: 120,
      enableSorting: false,
      meta: { align: 'right' as const },
      cell: ({ row }) => (
        <span className="text-xs text-fg-muted whitespace-nowrap">
          {formatBytes(row.original.request_body_size)} <span className="text-fg-subtle">/</span> {formatBytes(row.original.response_body_size)}
        </span>
      ),
    },
    {
      accessorKey: 'response_content_type',
      header: 'Content-Type',
      size: 150,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => {
        const ct = getValue<string>()
        return <span className="text-xs truncate block text-fg-muted" title={ct}>{ct.split(';')[0] || '—'}</span>
      },
    },
  ], [])

  const records = data?.records ?? []
  const open = openId ? records.find((r) => r.id === openId) ?? null : null

  return (
    <Page
      title="MITM Inspector"
      count={records.length || undefined}
      subtitle="Intercepted HTTPS requests and responses, live"
      actions={canMutate && records.length > 0 ? (
        <Button variant="outline" size="sm" onClick={() => setConfirmClear(true)} disabled={clearMutation.isPending}>
          <Trash2 className="w-3.5 h-3.5" /> Clear
        </Button>
      ) : undefined}
      panel={open ? <RequestPanel key={open.id} record={open} onClose={() => setOpenId(null)} /> : null}
    >
      {isLoading ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : records.length === 0 ? (
        <EmptyState
          icon={<Eye />}
          title="No intercepted requests"
          description="Requests appear here when TLS interception is enabled in the project settings and traffic flows through the proxy."
        />
      ) : (
        <DataTable
          columns={columns}
          data={records}
          defaultPageSize={50}
          enableColumnFilters
          getRowId={(row) => row.id}
          onRowClick={(row) => setOpenId(row.id)}
          activeRowId={openId}
          columnVisibility={open ? { response_content_type: false, size: false } : {}}
          emptyMessage="No requests match the current filters."
        />
      )}
      {confirmClear && (
        <ConfirmDialog
          title="Clear captured requests?"
          message="All captured requests for this project are discarded. New traffic keeps being captured."
          confirmLabel="Clear"
          onCancel={() => setConfirmClear(false)}
          onConfirm={() => clearMutation.mutate()}
          isLoading={clearMutation.isPending}
        />
      )}
    </Page>
  )
}
