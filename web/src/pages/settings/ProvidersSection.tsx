// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Plus, Puzzle, Trash2 } from 'lucide-react'
import { deleteProvider, fetchProvider, ProviderDetail, ProviderSpec, ProviderSummary } from '../../api/client'
import { useProviders } from '../../hooks/useProviders'
import { useToast } from '../../contexts/ToastContext'
import { DataTable } from '../../components/DataTable'
import { Page, EmptyState } from '../../components/layout/Page'
import { ProviderLogo } from '../../components/ProviderLogo'
import { ProviderBuilder, duplicateSpec } from '../../components/providers/ProviderBuilder'
import { ProviderDetailPanel } from '../../components/providers/ProviderDetailPanel'
import { Badge, Button, ConfirmDialog } from '../../components/ui'

type PanelState = { kind: 'view'; id: string } | { kind: 'edit'; id: string } | { kind: 'new'; spec?: ProviderSpec } | null

/** Admin catalog of provider types: shipped ones read-only, custom descriptors editable in the builder. */
export default function ProvidersSection() {
  const queryClient = useQueryClient()
  const { providers, isLoading, byId } = useProviders()
  const toast = useToast()
  const [panel, setPanel] = useState<PanelState>(null)
  const [pendingDelete, setPendingDelete] = useState<ProviderSummary | null>(null)

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProvider(id),
    onSuccess: (_r, id) => {
      queryClient.invalidateQueries({ queryKey: ['providers'] })
      if (panel && 'id' in panel && panel.id === id) setPanel(null)
      setPendingDelete(null)
      toast.show('Provider deleted')
    },
    onError: (e: Error) => { setPendingDelete(null); toast.show(e.message || 'Failed to delete provider', 'error') },
  })

  const columns: ColumnDef<ProviderSummary>[] = useMemo(() => [
    {
      accessorKey: 'name',
      header: 'Provider',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-2.5 max-w-full">
          <ProviderLogo type={row.original.id} name={row.original.name} className="w-5 h-5 text-[20px]" />
          <span className="font-medium truncate">{row.original.name}</span>
          {row.original.beta && <Badge color="yellow" className="py-0 text-[10px]">beta</Badge>}
          <span className="text-fg-subtle font-mono text-[11px] truncate">{row.original.id}</span>
        </span>
      ),
    },
    {
      id: 'source',
      accessorFn: (row: ProviderSummary) => (row.kind === 'code' ? 'built-in code' : row.source === 'builtin' ? 'built-in' : row.source),
      header: 'Source',
      size: 120,
      meta: { filterVariant: 'select' as const },
      cell: ({ row, getValue }) => <Badge color={row.original.source === 'custom' ? 'purple' : row.original.kind === 'code' ? 'slate' : 'blue'}>{getValue<string>()}</Badge>,
    },
    {
      id: 'modes',
      header: 'Proxy types',
      enableSorting: false,
      accessorFn: (row: ProviderSummary) => row.proxy_types.map((t) => t.label).join(', '),
      cell: ({ row }) => <span className="text-fg-muted text-xs truncate block">{row.original.kind === 'code' ? (row.original.cloud ? 'Cloud instances' : 'Static list') : row.original.proxy_types.map((t) => `${t.label} (${t.mode})`).join(', ')}</span>,
    },
    {
      id: 'hosts',
      header: 'Credentials sent to',
      enableSorting: false,
      accessorFn: (row: ProviderSummary) => row.egress_hosts.join(', '),
      cell: ({ getValue }) => <span className="text-fg-muted font-mono text-[11px] truncate block">{getValue<string>() || '—'}</span>,
    },
    {
      id: 'usage',
      header: 'In use',
      size: 110,
      accessorFn: (row: ProviderSummary) => row.credential_count,
      cell: ({ row }) => <span className="text-fg-muted text-xs tabular-nums">{row.original.credential_count} cred · {row.original.connector_count} conn</span>,
    },
    {
      id: 'actions',
      header: '',
      size: 56,
      enableSorting: false,
      cell: ({ row }) => row.original.editable ? (
        <div className="flex justify-end">
          <button onClick={(e) => { e.stopPropagation(); setPendingDelete(row.original) }} className="p-1 rounded text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Delete">
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ) : null,
    },
  ], [])

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'view' && byId[panel.id]) {
    const p = byId[panel.id]
    panelNode = (
      <ProviderDetailPanel
        key={p.id}
        provider={p}
        onClose={() => setPanel(null)}
        onDuplicate={(spec) => setPanel({ kind: 'new', spec: duplicateSpec(spec) })}
        onEdit={p.editable ? () => setPanel({ kind: 'edit', id: p.id }) : undefined}
      />
    )
  } else if (panel?.kind === 'edit' && byId[panel.id]) {
    panelNode = (
      <EditBuilder
        key={panel.id}
        providerId={panel.id}
        onClose={() => setPanel(null)}
        onDelete={() => setPendingDelete(byId[panel.id])}
        onSaved={(p) => { setPanel({ kind: 'view', id: p.id }); toast.show('Provider saved') }}
      />
    )
  } else if (panel?.kind === 'new') {
    panelNode = (
      <ProviderBuilder
        key={panel.spec?.id ?? 'new'}
        initialSpec={panel.spec}
        onClose={() => setPanel(null)}
        onSaved={(p) => { setPanel({ kind: 'view', id: p.id }); toast.show(`Provider "${p.name}" created`) }}
      />
    )
  }

  const custom = providers.filter((p) => p.source === 'custom').length

  return (
    <Page
      title="Providers"
      count={providers.length}
      subtitle="Proxy vendors Octoprox can provision from. Shipped providers are read-only; add your own as declarative descriptors — no code, no redeploy."
      actions={<Button size="sm" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> New provider</Button>}
      panel={panelNode}
    >
      {isLoading ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : providers.length === 0 ? (
        <EmptyState icon={<Puzzle />} title="No providers" description="The catalog is empty, which should not happen." />
      ) : (
        <>
          <DataTable
            columns={columns}
            data={providers}
            getRowId={(row) => row.id}
            onRowClick={(row) => setPanel(row.editable ? { kind: 'edit', id: row.id } : { kind: 'view', id: row.id })}
            activeRowId={panel && 'id' in panel ? panel.id : null}
            columnVisibility={panel ? { hosts: false, usage: false, actions: false } : {}}
          />
          {custom === 0 && (
            <p className="text-xs text-fg-muted">Tip: open a shipped provider and choose “Duplicate as custom” to start from a working descriptor.</p>
          )}
        </>
      )}
      {pendingDelete && (
        <ConfirmDialog
          title="Delete provider?"
          message={<>Remove <b className="text-fg">{pendingDelete.name}</b>. {pendingDelete.credential_count > 0 ? `It is used by ${pendingDelete.credential_count} credential(s) and cannot be deleted until they are removed.` : 'Existing credentials of this type would stop working, so deletion is refused while any exist.'}</>}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => deleteMutation.mutate(pendingDelete.id)}
          isLoading={deleteMutation.isPending}
          confirmDisabled={pendingDelete.credential_count > 0}
        />
      )}
    </Page>
  )
}

function EditBuilder({ providerId, onClose, onSaved, onDelete }: { providerId: string; onClose: () => void; onSaved: (p: ProviderDetail) => void; onDelete: () => void }) {
  const { data } = useQuery({ queryKey: ['provider', providerId], queryFn: () => fetchProvider(providerId) })
  if (!data) return <div className="w-[760px] flex-none border-l border-line bg-surface p-6 text-sm text-fg-muted">Loading…</div>
  return <ProviderBuilder existing={data} onClose={onClose} onSaved={onSaved} onDelete={onDelete} />
}
