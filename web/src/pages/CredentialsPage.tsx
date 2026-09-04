// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Key, Plus, Trash2 } from 'lucide-react'
import {
  fetchProjectCredentials, fetchProjectCredential, fetchProjectConnectors, deleteProjectCredential,
  Credential, CredentialDetail,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useTheme } from '../contexts/ThemeContext'
import { useToast } from '../contexts/ToastContext'
import { DataTable } from '../components/DataTable'
import { Page } from '../components/layout/Page'
import { CredentialForm, NewCredentialPanel, TypeCard } from '../components/CredentialForm'
import { CREDENTIAL_TYPES } from '../utils/credentials'
import { formatDate, formatDateTime } from '../utils/format'
import { Button, Inspector, ConfirmDialog, KeyValue, InspectorSection } from '../components/ui'

type PanelState = { kind: 'edit'; id: string } | { kind: 'new' } | null

export default function CredentialsPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { canMutate } = useAuth()
  const { isDark } = useTheme()
  const toast = useToast()
  const [panel, setPanel] = useState<PanelState>(null)
  const [pendingDelete, setPendingDelete] = useState<Credential | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['credentials', selectedProjectId],
    queryFn: () => fetchProjectCredentials(selectedProjectId!),
    enabled: !!selectedProjectId,
  })
  const { data: connectorsData } = useQuery({
    queryKey: ['connectors', selectedProjectId],
    queryFn: () => fetchProjectConnectors(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: false,
  })
  const usedBy = useMemo(() => {
    const map: Record<string, string[]> = {}
    for (const c of connectorsData?.connectors ?? []) (map[c.credential_id] ||= []).push(c.name)
    return map
  }, [connectorsData])

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProjectCredential(selectedProjectId!, id),
    onSuccess: (_r, id) => {
      queryClient.invalidateQueries({ queryKey: ['credentials', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
      if (panel?.kind === 'edit' && panel.id === id) setPanel(null)
      setPendingDelete(null)
      toast.show('Credential deleted')
    },
    onError: (e: Error) => { setPendingDelete(null); toast.show(e.message || 'Failed to delete credential', 'error') },
  })

  const columns: ColumnDef<Credential>[] = useMemo(() => [
    {
      accessorKey: 'name',
      header: 'Credential',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => {
        const ct = CREDENTIAL_TYPES.find((t) => t.value === row.original.type)
        return (
          <span className="inline-flex items-center gap-2.5 max-w-full">
            {ct && <img src={isDark ? ct.logoDark : ct.logo} alt="" className="w-5 h-5 object-contain flex-none" />}
            <span className="font-medium truncate">{row.original.name}</span>
          </span>
        )
      },
    },
    {
      accessorFn: (row: Credential) => CREDENTIAL_TYPES.find((t) => t.value === row.type)?.label || row.type,
      id: 'type',
      header: 'Type',
      size: 160,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <span className="text-fg-muted">{getValue<string>()}</span>,
    },
    {
      id: 'used_by',
      header: 'Used by',
      enableSorting: false,
      accessorFn: (row: Credential) => (usedBy[row.id] ?? []).join(', '),
      cell: ({ getValue }) => <span className="text-fg-muted truncate block">{getValue<string>() || '—'}</span>,
    },
    {
      accessorKey: 'created_at',
      header: 'Created',
      size: 130,
      cell: ({ getValue }) => <span className="text-fg-muted">{formatDate(getValue<string>())}</span>,
    },
    ...(canMutate ? [{
      id: 'actions',
      header: '',
      size: 56,
      enableSorting: false,
      cell: ({ row }: { row: { original: Credential } }) => (
        <div className="flex justify-end">
          <button onClick={() => setPendingDelete(row.original)} className="p-1 rounded text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Delete">
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    }] as ColumnDef<Credential>[] : []),
  ], [canMutate, isDark, usedBy])

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'edit') {
    panelNode = (
      <EditCredentialPanel
        key={panel.id}
        credentialId={panel.id}
        usedBy={usedBy[panel.id] ?? []}
        canMutate={canMutate}
        onClose={() => setPanel(null)}
        onDelete={() => { const c = data?.credentials.find((x) => x.id === panel.id); if (c) setPendingDelete(c) }}
        onSaved={() => { setPanel(null); toast.show('Credential saved') }}
      />
    )
  } else if (panel?.kind === 'new') {
    panelNode = (
      <NewCredentialPanel
        onClose={() => setPanel(null)}
        onCreated={(c) => { setPanel(null); toast.show(`Credential "${c.name}" created`) }}
      />
    )
  }

  const credentials = data?.credentials ?? []

  return (
    <Page
      title="Credentials"
      count={data?.total}
      subtitle="Provider and cloud accounts this project can provision proxies from"
      actions={canMutate ? <Button size="sm" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> Add credential</Button> : undefined}
      panel={panelNode}
    >
      {isLoading ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : credentials.length === 0 ? (
        <EmptyCredentials canMutate={canMutate} onAdd={() => setPanel({ kind: 'new' })} />
      ) : (
        <DataTable
          columns={columns}
          data={credentials}
          getRowId={(row) => row.id}
          onRowClick={(row) => setPanel({ kind: 'edit', id: row.id })}
          activeRowId={panel?.kind === 'edit' ? panel.id : null}
          columnVisibility={panel ? { created_at: false, actions: false } : {}}
        />
      )}
      {pendingDelete && (
        <ConfirmDialog
          title="Delete credential?"
          message={
            (usedBy[pendingDelete.id] ?? []).length > 0
              ? <><b className="text-fg">{pendingDelete.name}</b> is used by {usedBy[pendingDelete.id].join(', ')}. Those connectors will stop provisioning until they get another credential.</>
              : <>Remove <b className="text-fg">{pendingDelete.name}</b>. This cannot be undone.</>
          }
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => deleteMutation.mutate(pendingDelete.id)}
          isLoading={deleteMutation.isPending}
        />
      )}
    </Page>
  )
}

function EmptyCredentials({ canMutate, onAdd }: { canMutate: boolean; onAdd: () => void }) {
  return (
    <div className="bg-surface rounded-lg border border-line p-10 text-center">
      <Key className="w-10 h-10 text-fg-subtle mx-auto mb-3" />
      <h3 className="text-base font-medium">No credentials yet</h3>
      <p className="text-sm text-fg-muted mt-1 max-w-md mx-auto">Add a cloud account or proxy provider login. Connectors use credentials to provision proxies.</p>
      {canMutate && <Button size="sm" className="mt-4" onClick={onAdd}><Plus className="w-3.5 h-3.5" /> Add credential</Button>}
    </div>
  )
}

function EditCredentialPanel({ credentialId, usedBy, canMutate, onClose, onDelete, onSaved }: {
  credentialId: string
  usedBy: string[]
  canMutate: boolean
  onClose: () => void
  onDelete: () => void
  onSaved: (c: CredentialDetail) => void
}) {
  const { selectedProjectId } = useProject()
  const { data: detail, isLoading } = useQuery({
    queryKey: ['credential', selectedProjectId, credentialId],
    queryFn: () => fetchProjectCredential(selectedProjectId!, credentialId),
    enabled: !!selectedProjectId,
    refetchInterval: false,
  })
  const typeLabel = detail ? CREDENTIAL_TYPES.find((t) => t.value === detail.type)?.label : ''

  return (
    <Inspector
      title={detail ? detail.name : 'Credential'}
      subtitle={detail ? `${typeLabel} credential` : undefined}
      onClose={onClose}
      width={460}
      footer={canMutate ? (
        <>
          <Button type="button" variant="danger-ghost" size="sm" onClick={onDelete}><Trash2 className="w-3.5 h-3.5" /> Delete</Button>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="credential-form" size="sm" disabled={!detail}>Save changes</Button>
        </>
      ) : undefined}
    >
      {isLoading || !detail ? (
        <div className="text-sm text-fg-muted py-6 text-center">Loading…</div>
      ) : (
        <>
          <TypeCard type={detail.type} />
          <CredentialForm key={detail.id} type={detail.type} credential={detail} onSaved={onSaved} formId="credential-form" />
          <InspectorSection title="Usage">
            <KeyValue label="Used by" value={usedBy.length ? usedBy.join(', ') : 'no connectors yet'} />
            <KeyValue label="Created" value={formatDateTime(detail.created_at)} />
            <KeyValue label="Updated" value={formatDateTime(detail.updated_at)} />
          </InspectorSection>
        </>
      )}
    </Inspector>
  )
}
