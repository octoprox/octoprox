// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useMemo, useCallback, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef, VisibilityState } from '@tanstack/react-table'
import { Plus, Trash2, Upload, Lock, ShieldOff } from 'lucide-react'
import {
  fetchProjectProxies, fetchProjectConnectors, createProjectProxy, updateProjectProxy, deleteProjectProxy,
  unquarantineProjectProxy, uploadProjectProxies, Proxy, ProxyCreate, ProxyUpdate, ProxyUploadResponse, CredentialType,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { DataTable, createSelectionColumn } from '../components/DataTable'
import { Page } from '../components/layout/Page'
import { ProxyStatusDot, ProxyStatusBadge, formatQuarantineRemaining } from '../components/ProxyStatus'
import { ProviderLogo } from '../components/ProviderLogo'
import { formatBytes, formatDate, formatDateTime } from '../utils/format'
import { Button, Input, Select, Label, Alert, Inspector, InspectorSection, StatGrid, KeyValue, ConfirmDialog } from '../components/ui'

type PanelState =
  | { kind: 'proxy'; id: string }
  | { kind: 'new' }
  | { kind: 'upload' }
  | null

const PROTOCOLS = ['http', 'https', 'socks4', 'socks5']

export default function ProxiesPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { canMutate } = useAuth()
  const toast = useToast()
  const [panel, setPanel] = useState<PanelState>(null)
  const [pendingDelete, setPendingDelete] = useState<Proxy[] | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['proxies', selectedProjectId],
    queryFn: () => fetchProjectProxies(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: 10000, // statuses and counters move
  })
  const { data: connectorsData } = useQuery({
    queryKey: ['connectors', selectedProjectId],
    queryFn: () => fetchProjectConnectors(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: false,
  })
  const staticConnectors = useMemo(
    () => connectorsData?.connectors.filter((c) => c.credential_type === 'static_proxy_provider') || [],
    [connectorsData]
  )

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['proxies', selectedProjectId] })
    queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
    queryClient.invalidateQueries({ queryKey: ['metrics', selectedProjectId] })
  }

  const unquarantineMutation = useMutation({
    mutationFn: (id: string) => unquarantineProjectProxy(selectedProjectId!, id),
    onSuccess: () => { invalidate(); toast.show('Released from quarantine') },
    onError: (e: Error) => toast.show(e.message || 'Failed to release proxy', 'error'),
  })

  const deleteMutation = useMutation({
    mutationFn: async (proxies: Proxy[]) => {
      await Promise.all(proxies.map((p) => deleteProjectProxy(selectedProjectId!, p.id)))
      return proxies.length
    },
    onSuccess: (n, proxies) => {
      invalidate()
      if (panel?.kind === 'proxy' && proxies.some((p) => p.id === panel.id)) setPanel(null)
      setPendingDelete(null)
      toast.show(n === 1 ? 'Proxy deleted' : `${n} proxies deleted`)
    },
    onError: (e: Error) => { setPendingDelete(null); toast.show(e.message || 'Delete failed', 'error') },
  })

  const proxies = data?.proxies ?? []
  const openProxy = panel?.kind === 'proxy' ? proxies.find((p) => p.id === panel.id) ?? null : null

  // If the open proxy disappears (deleted elsewhere), close the panel.
  useEffect(() => {
    if (panel?.kind === 'proxy' && data && !proxies.some((p) => p.id === panel.id)) setPanel(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])

  const columns: ColumnDef<Proxy>[] = useMemo(() => [
    ...(canMutate ? [createSelectionColumn<Proxy>()] : []),
    {
      accessorFn: (row: Proxy) => `${row.display_host}:${row.port}`,
      id: 'host',
      header: 'Host',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="font-mono text-xs inline-flex items-center gap-1.5 max-w-full">
          {row.original.username ? <Lock className="w-3 h-3 text-fg-subtle flex-none" /> : <span className="w-3 flex-none" />}
          <span className="truncate">{row.original.display_host}:{row.original.port}</span>
        </span>
      ),
    },
    {
      accessorKey: 'protocol',
      header: 'Proto',
      size: 80,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <span className="uppercase text-[11px] font-semibold text-fg-subtle">{getValue<string>()}</span>,
    },
    {
      accessorKey: 'connector_name',
      header: 'Connector',
      enableSorting: false,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <span className="text-fg-muted truncate block">{getValue<string>() || '—'}</span>,
    },
    {
      id: 'status',
      accessorFn: (row: Proxy) => (row.quarantined ? 'quarantined' : !row.connector_enabled ? 'disabled' : row.status),
      header: 'Status',
      size: 160,
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => <ProxyStatusDot proxy={row.original} />,
    },
    { accessorKey: 'request_count', header: 'Reqs', size: 80, meta: { align: 'right' as const }, cell: ({ getValue }) => getValue<number>().toLocaleString() },
    {
      accessorKey: 'success_rate',
      header: 'Success',
      size: 100,
      meta: { align: 'right' as const },
      cell: ({ getValue, row }) => (
        <span className={row.original.request_count > 0 && getValue<number>() < 90 ? 'text-danger' : ''}>
          {row.original.request_count > 0 ? `${getValue<number>().toFixed(1)}%` : '—'}
        </span>
      ),
    },
    {
      accessorKey: 'avg_latency_ms',
      header: 'Latency',
      size: 90,
      meta: { align: 'right' as const },
      cell: ({ getValue, row }) => (row.original.request_count > 0 ? `${Math.round(getValue<number>()).toLocaleString()} ms` : '—'),
    },
    {
      id: 'traffic',
      header: '↑ / ↓',
      size: 130,
      enableSorting: false,
      meta: { align: 'right' as const },
      cell: ({ row }) => (
        <span className="text-xs text-fg-muted whitespace-nowrap">
          {formatBytes(row.original.bytes_sent)} <span className="text-fg-subtle">/</span> {formatBytes(row.original.bytes_received)}
        </span>
      ),
    },
    ...(canMutate ? [{
      id: 'actions',
      header: '',
      size: 72,
      enableSorting: false,
      cell: ({ row }: { row: { original: Proxy } }) => (
        <div className="flex gap-0.5 justify-end">
          {row.original.quarantined && (
            <button
              onClick={() => unquarantineMutation.mutate(row.original.id)}
              className="p-1 rounded text-warning hover:bg-warning-soft"
              title="Release from quarantine"
            >
              <ShieldOff className="w-4 h-4" />
            </button>
          )}
          <button
            onClick={() => setPendingDelete([row.original])}
            className="p-1 rounded text-fg-subtle hover:text-danger hover:bg-danger-soft"
            title="Delete"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    }] as ColumnDef<Proxy>[] : []),
  ], [canMutate, unquarantineMutation])

  // With the panel open there is less room. Connector and traffic are shown in the
  // panel itself, so the table keeps host, status and the per-proxy numbers.
  const columnVisibility = useMemo<VisibilityState>(
    () => (panel
      ? { protocol: false, connector_name: false, traffic: false, actions: false }
      : { protocol: true, connector_name: true, traffic: true, actions: true }),
    [panel]
  )

  const bulkActions = useCallback((selected: Proxy[], clear: () => void) => (
    <>
      <button
        onClick={() => setPendingDelete(selected)}
        className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-danger hover:bg-danger-soft font-medium"
      >
        <Trash2 className="w-3.5 h-3.5" /> Delete selected
      </button>
      <button onClick={clear} className="text-primary hover:brightness-110">Clear</button>
    </>
  ), [])

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'proxy' && openProxy) {
    panelNode = (
      <ProxyPanel
        key={openProxy.id}
        proxy={openProxy}
        connectorType={connectorsData?.connectors.find((c) => c.id === openProxy.connector_id)?.credential_type ?? null}
        canMutate={canMutate}
        onClose={() => setPanel(null)}
        onRelease={() => unquarantineMutation.mutate(openProxy.id)}
        onDelete={() => setPendingDelete([openProxy])}
        onSaved={() => { invalidate(); toast.show('Proxy saved') }}
      />
    )
  } else if (panel?.kind === 'new') {
    panelNode = (
      <NewProxyPanel
        connectors={staticConnectors}
        onClose={() => setPanel(null)}
        onCreated={() => { invalidate(); setPanel(null); toast.show('Proxy added — health check running') }}
      />
    )
  } else if (panel?.kind === 'upload') {
    panelNode = (
      <UploadPanel
        connectors={staticConnectors}
        onClose={() => setPanel(null)}
        onUploaded={() => invalidate()}
      />
    )
  }

  return (
    <Page
      title="Proxies"
      count={data?.total}
      actions={canMutate ? (
        <>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setPanel({ kind: 'upload' })}
            disabled={staticConnectors.length === 0}
            title={staticConnectors.length === 0 ? 'Create a Static Proxy Provider connector first' : 'Upload proxies from CSV'}
          >
            <Upload className="w-3.5 h-3.5" /> Upload CSV
          </Button>
          <Button size="sm" onClick={() => setPanel({ kind: 'new' })} disabled={staticConnectors.length === 0} title={staticConnectors.length === 0 ? 'Create a Static Proxy Provider connector first' : undefined}>
            <Plus className="w-3.5 h-3.5" /> Add proxy
          </Button>
        </>
      ) : undefined}
      panel={panelNode}
    >
      {isLoading ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : (
        <DataTable
          columns={columns}
          data={proxies}
          defaultPageSize={20}
          emptyMessage="No proxies yet. Connectors provision cloud and provider proxies automatically; static proxies can be added or uploaded here."
          enableRowSelection={canMutate}
          enableColumnFilters
          getRowId={(row) => row.id}
          onRowClick={(row) => setPanel({ kind: 'proxy', id: row.id })}
          activeRowId={panel?.kind === 'proxy' ? panel.id : null}
          columnVisibility={columnVisibility}
          bulkActions={canMutate ? bulkActions : undefined}
        />
      )}

      {pendingDelete && (
        <ConfirmDialog
          title={pendingDelete.length === 1 ? 'Delete proxy?' : `Delete ${pendingDelete.length} proxies?`}
          message={pendingDelete.length === 1
            ? <>Remove <span className="font-mono text-fg">{pendingDelete[0].display_host}:{pendingDelete[0].port}</span> from the pool. Traffic in flight will fail over to other proxies.</>
            : 'The selected proxies are removed from the pool. Traffic in flight fails over to the remaining proxies.'}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => deleteMutation.mutate(pendingDelete)}
          isLoading={deleteMutation.isPending}
        />
      )}
    </Page>
  )
}

// ---------------------------------------------------------------------------
// Panels

function ProxyPanel({ proxy, connectorType, canMutate, onClose, onRelease, onDelete, onSaved }: {
  proxy: Proxy
  connectorType: CredentialType | null
  canMutate: boolean
  onClose: () => void
  onRelease: () => void
  onDelete: () => void
  onSaved: () => void
}) {
  const { selectedProjectId } = useProject()
  const toast = useToast()
  const [form, setForm] = useState({ host: proxy.host, port: String(proxy.port), protocol: proxy.protocol, username: proxy.username || '', password: proxy.password || '' })
  const dirty = form.host !== proxy.host || form.port !== String(proxy.port) || form.protocol !== proxy.protocol || form.username !== (proxy.username || '') || form.password !== (proxy.password || '')

  const updateMutation = useMutation({
    mutationFn: (d: ProxyUpdate) => updateProjectProxy(selectedProjectId!, proxy.id, d),
    onSuccess: onSaved,
    onError: (e: Error) => toast.show(e.message || 'Failed to save proxy', 'error'),
  })

  // Only static-provider proxies are hand-edited; cloud and provider proxies are managed by their connector.
  const isStatic = connectorType === 'static_proxy_provider'

  return (
    <Inspector
      title={<span className="font-mono text-[14px]">{proxy.display_host}:{proxy.port}</span>}
      subtitle={proxy.connector_name || 'No connector'}
      onClose={onClose}
      footer={canMutate ? (
        <>
          <Button type="button" variant="danger-ghost" size="sm" onClick={onDelete}><Trash2 className="w-3.5 h-3.5" /> Delete</Button>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Close</Button>
          <Button type="submit" form="proxy-form" size="sm" disabled={!dirty || updateMutation.isPending}>{updateMutation.isPending ? 'Saving…' : 'Save changes'}</Button>
        </>
      ) : undefined}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <ProxyStatusBadge proxy={proxy} />
        <span className="text-xs text-fg-muted">
          {proxy.quarantined
            ? `releases automatically in ${formatQuarantineRemaining(proxy.quarantine_remaining_seconds)}`
            : !proxy.connector_enabled
              ? 'connector is disabled'
              : proxy.status === 'degraded'
                ? 'success rate is below the healthy threshold'
                : proxy.tags.length > 0 ? proxy.tags.join(', ') : `added ${formatDate(proxy.created_at)}`}
        </span>
      </div>
      {proxy.quarantined && canMutate && (
        <Button type="button" variant="outline" size="sm" onClick={onRelease} className="text-warning border-warning-soft bg-warning-soft hover:bg-warning-soft">
          <ShieldOff className="w-3.5 h-3.5" /> Release now
        </Button>
      )}

      <InspectorSection title="Traffic">
        <StatGrid items={[
          { label: 'Requests', value: proxy.request_count.toLocaleString() },
          { label: 'Success', value: proxy.request_count > 0 ? `${proxy.success_rate.toFixed(1)}%` : '—', className: proxy.request_count > 0 && proxy.success_rate < 90 ? 'text-danger' : undefined },
          { label: 'Latency', value: proxy.request_count > 0 ? `${Math.round(proxy.avg_latency_ms)} ms` : '—' },
          { label: 'Sent ↑', value: formatBytes(proxy.bytes_sent) },
          { label: 'Received ↓', value: formatBytes(proxy.bytes_received) },
          { label: 'Failures', value: proxy.failure_count.toLocaleString(), className: proxy.failure_count > 0 ? 'text-danger' : undefined },
        ]} />
      </InspectorSection>

      <InspectorSection title="Configuration">
        <form id="proxy-form" onSubmit={(e) => { e.preventDefault(); updateMutation.mutate({ host: form.host, port: parseInt(form.port), protocol: form.protocol, username: form.username || undefined, password: form.password || undefined }) }} className="space-y-3">
          <div>
            <Label className="text-xs">Connector</Label>
            <div className="h-9 px-3 rounded-lg bg-surface-raised text-fg-muted text-sm flex items-center gap-2">
              {connectorType && <ProviderLogo type={connectorType} className="w-4 h-4 text-[16px]" />}
              <span className="truncate flex-1">{proxy.connector_name || '—'}</span>
              <Lock className="w-3.5 h-3.5 text-fg-subtle" />
            </div>
          </div>
          <div className="grid grid-cols-[minmax(0,1fr)_110px] gap-3">
            <div>
              <Label className="text-xs">Host</Label>
              <Input className="font-mono text-sm" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} disabled={!canMutate || !isStatic} required />
            </div>
            <div>
              <Label className="text-xs">Port</Label>
              <Input className="font-mono text-sm" type="number" value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} disabled={!canMutate || !isStatic} required />
            </div>
          </div>
          <div>
            <Label className="text-xs">Protocol</Label>
            <Select value={form.protocol} onChange={(e) => setForm({ ...form, protocol: e.target.value })} disabled={!canMutate || !isStatic}>
              {PROTOCOLS.map((p) => <option key={p} value={p}>{p.toUpperCase()}</option>)}
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="text-xs">Username</Label>
              <Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="Optional" disabled={!canMutate || !isStatic} autoComplete="off" />
            </div>
            <div>
              <Label className="text-xs">Password</Label>
              <Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Optional" disabled={!canMutate || !isStatic} autoComplete="new-password" />
            </div>
          </div>
          {!isStatic && <p className="text-xs text-fg-subtle">Provisioned by its connector; edit the connector to change how these proxies are created.</p>}
        </form>
      </InspectorSection>

      <InspectorSection title="Details">
        <KeyValue label="Upstream host" value={proxy.host} mono />
        <KeyValue label="Successes" value={proxy.success_count.toLocaleString()} />
        <KeyValue label="Added" value={formatDateTime(proxy.created_at)} />
      </InspectorSection>
    </Inspector>
  )
}

function NewProxyPanel({ connectors, onClose, onCreated }: { connectors: { id: string; name: string }[]; onClose: () => void; onCreated: () => void }) {
  const { selectedProjectId } = useProject()
  const [form, setForm] = useState({ host: '', port: '', protocol: 'http', connector_id: connectors[0]?.id ?? '', username: '', password: '' })
  const [error, setError] = useState<string | null>(null)
  const createMutation = useMutation({
    mutationFn: (d: ProxyCreate) => createProjectProxy(selectedProjectId!, d),
    onSuccess: onCreated,
    onError: (e: Error) => setError(e.message || 'Failed to create proxy'),
  })
  return (
    <Inspector
      title="Add proxy"
      subtitle="Manually managed proxy server"
      onClose={onClose}
      footer={
        <>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="new-proxy-form" size="sm" disabled={!form.connector_id || createMutation.isPending}>{createMutation.isPending ? 'Adding…' : 'Add proxy'}</Button>
        </>
      }
    >
      <form
        id="new-proxy-form"
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault()
          setError(null)
          createMutation.mutate({ host: form.host, port: parseInt(form.port), protocol: form.protocol, connector_id: form.connector_id, username: form.username || undefined, password: form.password || undefined })
        }}
      >
        {error && <Alert>{error}</Alert>}
        <div>
          <Label className="text-xs">Connector</Label>
          <Select value={form.connector_id} onChange={(e) => setForm({ ...form, connector_id: e.target.value })} required>
            {connectors.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </Select>
          <p className="text-xs text-fg-subtle mt-1">Only Static Proxy Provider connectors accept manually added proxies.</p>
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_110px] gap-3">
          <div>
            <Label className="text-xs">Host</Label>
            <Input className="font-mono text-sm" placeholder="203.0.113.10" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} required autoFocus />
          </div>
          <div>
            <Label className="text-xs">Port</Label>
            <Input className="font-mono text-sm" type="number" placeholder="8080" value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} required />
          </div>
        </div>
        <div>
          <Label className="text-xs">Protocol</Label>
          <Select value={form.protocol} onChange={(e) => setForm({ ...form, protocol: e.target.value })}>
            {PROTOCOLS.map((p) => <option key={p} value={p}>{p.toUpperCase()}</option>)}
          </Select>
        </div>
        <InspectorSection title="Authentication">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="text-xs">Username</Label>
              <Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="Optional" autoComplete="off" />
            </div>
            <div>
              <Label className="text-xs">Password</Label>
              <Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Optional" autoComplete="new-password" />
            </div>
          </div>
        </InspectorSection>
      </form>
    </Inspector>
  )
}

function UploadPanel({ connectors, onClose, onUploaded }: { connectors: { id: string; name: string }[]; onClose: () => void; onUploaded: () => void }) {
  const { selectedProjectId } = useProject()
  const [connectorId, setConnectorId] = useState(connectors[0]?.id ?? '')
  const [fileName, setFileName] = useState<string | null>(null)
  const [result, setResult] = useState<ProxyUploadResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const uploadMutation = useMutation({
    mutationFn: ({ file, connectorId }: { file: File; connectorId: string }) => uploadProjectProxies(selectedProjectId!, file, connectorId),
    onSuccess: (r) => { setResult(r); setError(null); onUploaded() },
    onError: (e: Error) => { setError(e.message || 'Upload failed'); setResult(null) },
  })

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const file = fileInputRef.current?.files?.[0]
    if (file && connectorId) uploadMutation.mutate({ file, connectorId })
  }

  return (
    <Inspector
      title="Upload proxies"
      subtitle="CSV or TXT, one proxy per line"
      onClose={onClose}
      footer={result ? (
        <>
          <span className="flex-1" />
          <Button size="sm" onClick={onClose}>Done</Button>
        </>
      ) : (
        <>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="upload-form" size="sm" disabled={uploadMutation.isPending || !fileName}>{uploadMutation.isPending ? 'Uploading…' : 'Upload'}</Button>
        </>
      )}
    >
      {!result ? (
        <form id="upload-form" onSubmit={submit} className="space-y-4">
          {error && <Alert>{error}</Alert>}
          <div>
            <Label className="text-xs">Connector</Label>
            <Select value={connectorId} onChange={(e) => setConnectorId(e.target.value)} required>
              {connectors.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </div>
          <label className="block border border-dashed border-line-strong rounded-[10px] p-5 text-center cursor-pointer hover:border-primary hover:bg-primary-soft/40 transition-colors">
            <Upload className="w-5 h-5 mx-auto text-fg-subtle mb-1.5" />
            <span className="block text-sm">{fileName ? <span className="font-medium text-fg">{fileName}</span> : <><span className="font-medium text-fg">Choose a file</span> <span className="text-fg-muted">or drop it here</span></>}</span>
            <span className="block text-xs text-fg-muted mt-1">One proxy per line: <code className="bg-surface-raised px-1 rounded font-mono">protocol://[user:pass@]host:port</code></span>
            <input ref={fileInputRef} type="file" accept=".csv,.txt" className="sr-only" onChange={(e) => setFileName(e.target.files?.[0]?.name ?? null)} required />
          </label>
        </form>
      ) : (
        <div className="space-y-4">
          <StatGrid items={[
            { label: 'Lines', value: result.total_lines },
            { label: 'Added', value: result.successful, className: 'text-success' },
            { label: 'Failed', value: result.failed, className: result.failed > 0 ? 'text-danger' : undefined },
          ]} />
          {result.errors.length > 0 && (
            <InspectorSection title="Errors">
              <div className="max-h-64 overflow-y-auto rounded-lg bg-danger-soft p-3 space-y-2">
                {result.errors.map((err, idx) => (
                  <div key={idx} className="text-xs text-danger">
                    <span className="font-medium">Line {err.line_number}:</span> {err.error}
                    <code className="block mt-0.5 text-[11px] font-mono opacity-80 break-all">{err.line}</code>
                  </div>
                ))}
              </div>
            </InspectorSection>
          )}
        </div>
      )}
    </Inspector>
  )
}
