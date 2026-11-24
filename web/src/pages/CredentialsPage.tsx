// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Fragment, useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { KeyRound, Plus, Trash2 } from 'lucide-react'
import {
  fetchProjectCredentials, fetchProjectCredential, fetchProjectConnectors, deleteProjectCredential,
  Credential, CredentialDetail,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { useDropGone, useUrlParam, useUrlTab } from '../hooks/useUrlState'
import { DataTable, type DataTableFeatures } from '../components/DataTable'
import { Page } from '../components/layout/Page'
import { AdminLink } from '../components/AdminLink'
import { CredentialForm, NewCredentialPanel, TypeCard } from '../components/CredentialForm'
import { ConnectorLink } from '../components/RouteList'
import { EntityHistory } from '../components/audit/EntityHistory'
import { useProviders } from '../hooks/useProviders'
import { ProviderLogo } from '../components/ProviderLogo'
import { formatDate, formatDateTime } from '../utils/format'
import { Button, Inspector, ConfirmDialog, KeyValue, InspectorSection, LogoLoader, QueryContent, Tabs } from '../components/ui'

/** What an open credential shows: its settings, or its History in their place. */
type CredentialView = 'settings' | 'history'
const CREDENTIAL_VIEWS: readonly CredentialView[] = ['settings', 'history']

/** A connector using a credential. */
type CredentialUser = { id: string; name: string }

export default function CredentialsPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { canMutate, isAdmin } = useAuth()
  const { labelFor } = useProviders()
  const toast = useToast()
  // The open credential and its tab are in the address, so a link opens them; the new credential form is not.
  const [openId, setOpenId] = useUrlParam('open')
  const [view, setView, viewReveal] = useUrlTab(CREDENTIAL_VIEWS, 'settings', 'view')
  const [adding, setAdding] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<Credential | null>(null)
  const openPanel = (id: string | null) => setOpenId(id, { view: null })
  const openNew = () => { setAdding(true); openPanel(null) }
  // A credential opened by a link replaces a new credential form left open.
  useEffect(() => { if (openId) setAdding(false) }, [openId])

  const credentialsQuery = useQuery({
    queryKey: ['credentials', selectedProjectId],
    queryFn: () => fetchProjectCredentials(selectedProjectId!),
    enabled: !!selectedProjectId,
  })
  const { data } = credentialsQuery
  const { data: connectorsData } = useQuery({
    queryKey: ['connectors', selectedProjectId],
    queryFn: () => fetchProjectConnectors(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: false,
  })
  const usedBy = useMemo(() => {
    const map: Record<string, CredentialUser[]> = {}
    for (const c of connectorsData?.connectors ?? []) (map[c.credential_id] ||= []).push({ id: c.id, name: c.name })
    return map
  }, [connectorsData])
  const listed = !!openId && !!data?.credentials.some((c) => c.id === openId)
  useDropGone(['open', 'view'], !!openId && !!data && !credentialsQuery.isFetching && !listed, 'That credential no longer exists.')

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProjectCredential(selectedProjectId!, id),
    onSuccess: (_r, id) => {
      queryClient.invalidateQueries({ queryKey: ['credentials', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
      if (openId === id) openPanel(null)
      setPendingDelete(null)
      toast.show('Credential deleted')
    },
    onError: (e: Error) => { setPendingDelete(null); toast.show(e.message || 'Failed to delete credential', 'error') },
  })

  const columns: ColumnDef<DataTableFeatures, Credential>[] = useMemo(() => [
    {
      accessorKey: 'name',
      header: 'Credential',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-2.5 max-w-full">
          <ProviderLogo type={row.original.type} className="w-5 h-5 text-[20px]" />
          <span className="font-medium truncate">{row.original.name}</span>
        </span>
      ),
    },
    {
      accessorFn: (row: Credential) => labelFor(row.type),
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
      accessorFn: (row: Credential) => (usedBy[row.id] ?? []).map((c) => c.name).join(', '),
      cell: ({ getValue }) => <span className="text-fg-muted truncate block">{getValue<string>() || '-'}</span>,
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
          <button onClick={() => setPendingDelete(row.original)} className="p-1 rounded-sm text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Delete">
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    }] as ColumnDef<DataTableFeatures, Credential>[] : []),
  ], [canMutate, labelFor, usedBy])

  let panelNode: React.ReactNode = null
  // A linked credential shows once the list holds it, or the link is dropped; meanwhile no new credential form shows.
  if (openId && listed) {
    panelNode = (
      <EditCredentialPanel
        key={openId}
        credentialId={openId}
        usedBy={usedBy[openId] ?? []}
        canMutate={canMutate}
        view={view}
        onViewChange={setView}
        viewReveal={viewReveal}
        onClose={() => openPanel(null)}
        onDelete={() => { const c = data?.credentials.find((x) => x.id === openId); if (c) setPendingDelete(c) }}
        onSaved={() => { openPanel(null); toast.show('Credential saved') }}
      />
    )
  } else if (!openId && adding) {
    panelNode = (
      <NewCredentialPanel
        onClose={() => setAdding(false)}
        onCreated={(c) => { setAdding(false); toast.show(`Credential "${c.name}" created`) }}
      />
    )
  }

  const credentials = data?.credentials ?? []

  return (
    <Page
      title="Credentials"
      count={data?.total}
      subtitle={<>Provider and cloud accounts this project can provision proxies from{isAdmin && <>. The providers themselves are in <AdminLink page="providers">Providers</AdminLink>, for every project.</>}</>}
      actions={canMutate ? <Button size="sm" onClick={openNew}><Plus className="w-3.5 h-3.5" /> Add credential</Button> : undefined}
      panel={panelNode}
    >
      <QueryContent query={credentialsQuery} what="the credentials">
        {credentials.length === 0 ? (
          <EmptyCredentials canMutate={canMutate} onAdd={openNew} />
        ) : (
          <DataTable
            columns={columns}
            data={credentials}
            getRowId={(row) => row.id}
            onRowClick={(row) => openPanel(row.id)}
            activeRowId={openId}
            columnVisibility={panelNode ? { created_at: false, actions: false } : {}}
          />
        )}
      </QueryContent>
      {pendingDelete && (
        <ConfirmDialog
          title="Delete credential?"
          message={
            (usedBy[pendingDelete.id] ?? []).length > 0
              ? <><b className="text-fg">{pendingDelete.name}</b> is used by {usedBy[pendingDelete.id].map((c) => c.name).join(', ')}. Those connectors will stop provisioning until they get another credential.</>
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
      <KeyRound className="w-10 h-10 text-fg-subtle mx-auto mb-3" />
      <h3 className="text-base font-medium">No credentials yet</h3>
      <p className="text-sm text-fg-muted mt-1 max-w-md mx-auto">Add a cloud account or proxy provider login. Connectors use credentials to provision proxies.</p>
      {canMutate && <Button size="sm" className="mt-4" onClick={onAdd}><Plus className="w-3.5 h-3.5" /> Add credential</Button>}
    </div>
  )
}

function EditCredentialPanel({ credentialId, usedBy, canMutate, view, onViewChange, viewReveal, onClose, onDelete, onSaved }: {
  credentialId: string
  usedBy: CredentialUser[]
  canMutate: boolean
  view: CredentialView
  onViewChange: (view: CredentialView) => void
  viewReveal: object | null
  onClose: () => void
  onDelete: () => void
  onSaved: (c: CredentialDetail) => void
}) {
  const { selectedProjectId } = useProject()
  const credentialQuery = useQuery({
    queryKey: ['credential', selectedProjectId, credentialId],
    queryFn: () => fetchProjectCredential(selectedProjectId!, credentialId),
    enabled: !!selectedProjectId,
    refetchInterval: false,
  })
  const { data: detail } = credentialQuery
  const { labelFor } = useProviders()
  const typeLabel = detail ? labelFor(detail.type) : ''

  return (
    <Inspector
      title={detail ? detail.name : 'Credential'}
      subtitle={detail ? `${typeLabel} credential` : undefined}
      onClose={onClose}
      footer={canMutate && view === 'settings' ? (
        <>
          <Button type="button" variant="danger-ghost" size="sm" onClick={onDelete}><Trash2 className="w-3.5 h-3.5" /> Delete</Button>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="credential-form" size="sm" disabled={!detail}>Save changes</Button>
        </>
      ) : undefined}
    >
      <QueryContent query={credentialQuery} what="the credential" loader={<LogoLoader className="py-6" />}>
        {(detail) => (
          <>
            <Tabs<CredentialView>
              tabs={[{ id: 'settings', label: 'Settings' }, { id: 'history', label: 'History' }]}
              active={view}
              onChange={onViewChange}
              reveal={viewReveal}
              className="pointer-coarse:*:min-h-11"
            />
            {/* Hidden rather than unmounted while History shows: the form holds the edits not saved yet. */}
            <div hidden={view === 'history'} className="space-y-4">
              <TypeCard type={detail.type} />
              <CredentialForm key={detail.id} type={detail.type} credential={detail} onSaved={onSaved} formId="credential-form" />
              <InspectorSection title="Usage">
                <KeyValue
                  label="Used by"
                  value={usedBy.length && selectedProjectId
                    // The value clips its overflow, so the 44 px touch target grows the row here, as the Failures panel's do.
                    ? usedBy.map((c, i) => <Fragment key={c.id}>{i > 0 && ', '}<ConnectorLink projectId={selectedProjectId} connectorId={c.id} className="pointer-coarse:my-0">{c.name}</ConnectorLink></Fragment>)
                    : 'no connectors yet'}
                />
                <KeyValue label="Created" value={formatDateTime(detail.created_at)} />
                <KeyValue label="Updated" value={formatDateTime(detail.updated_at)} />
              </InspectorSection>
            </div>
            {view === 'history' && <EntityHistory entityType="credential" entityId={detail.id} />}
          </>
        )}
      </QueryContent>
    </Inspector>
  )
}
