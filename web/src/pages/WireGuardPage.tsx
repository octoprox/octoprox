// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Check, Copy, Download, KeyRound, Plus, Router, Trash2 } from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import {
  createWireGuardPeer, deleteWireGuardPeer, fetchWireGuardPeerConfig, fetchWireGuardPeers,
  rotateWireGuardPeerKeys, updateWireGuardPeer,
  WireGuardPeer, WireGuardPeerCreate, WireGuardPeerUpdate,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { DataTable } from '../components/DataTable'
import { Page, EmptyState } from '../components/layout/Page'
import { formatBytes, formatDateTime, parseApiDate } from '../utils/format'
import {
  Alert, Badge, Button, ConfirmDialog, InfoTip, Input, Inspector, InspectorSection, KeyValue, Label,
} from '../components/ui'

type PanelState = { kind: 'edit'; id: string } | { kind: 'new' } | null

/**
 * The project's WireGuard devices: anything that cannot speak to a proxy
 * (TVs, consoles, routers, phones) joins the pool by connecting to the tunnel.
 * Each device is a peer with its own address and the routing a proxy client
 * would put in its username.
 */
export default function WireGuardPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { canMutate, isAdmin } = useAuth()
  const toast = useToast()
  const [panel, setPanel] = useState<PanelState>(null)
  const [pendingDelete, setPendingDelete] = useState<WireGuardPeer | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['wireguard-peers', selectedProjectId],
    queryFn: () => fetchWireGuardPeers(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: 10_000, // handshakes and counters move
  })
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['wireguard-peers', selectedProjectId] })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteWireGuardPeer(selectedProjectId!, id),
    onSuccess: (_r, id) => {
      invalidate()
      if (panel?.kind === 'edit' && panel.id === id) setPanel(null)
      setPendingDelete(null)
      toast.show('Device removed')
    },
    onError: (e: Error) => { setPendingDelete(null); toast.show(e.message || 'Failed to remove device', 'error') },
  })

  const columns: ColumnDef<WireGuardPeer>[] = useMemo(() => [
    {
      accessorKey: 'name',
      header: 'Device',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-2.5 max-w-full">
          <Router className="w-4 h-4 text-fg-subtle flex-none" />
          <span className={row.original.enabled ? 'font-medium truncate' : 'font-medium truncate text-fg-muted line-through'}>{row.original.name}</span>
        </span>
      ),
    },
    {
      accessorKey: 'address',
      header: 'Tunnel address',
      size: 140,
      cell: ({ getValue }) => <span className="font-mono text-xs text-fg-muted">{getValue<string>()}</span>,
    },
    {
      id: 'status',
      header: 'Status',
      size: 150,
      accessorFn: (row: WireGuardPeer) => statusLabel(row),
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => <StatusBadge peer={row.original} />,
    },
    {
      id: 'routing',
      header: 'Routing',
      enableSorting: false,
      accessorFn: (row: WireGuardPeer) => routingLabel(row),
      cell: ({ getValue }) => <span className="text-fg-muted truncate block">{getValue<string>() || 'project default'}</span>,
    },
    {
      id: 'traffic',
      header: 'Traffic',
      size: 150,
      enableSorting: false,
      accessorFn: (row: WireGuardPeer) => (row.status ? row.status.rx_bytes + row.status.tx_bytes : 0),
      cell: ({ row }) => row.original.status
        ? <span className="text-fg-muted tabular-nums text-xs">{formatBytes(row.original.status.rx_bytes)} in, {formatBytes(row.original.status.tx_bytes)} out</span>
        : <span className="text-fg-subtle">-</span>,
    },
    ...(canMutate ? [{
      id: 'actions',
      header: '',
      size: 56,
      enableSorting: false,
      cell: ({ row }: { row: { original: WireGuardPeer } }) => (
        <div className="flex justify-end">
          <button onClick={() => setPendingDelete(row.original)} className="p-1 rounded text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Remove">
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    }] as ColumnDef<WireGuardPeer>[] : []),
  ], [canMutate])

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'edit') {
    const peer = data?.peers.find((p) => p.id === panel.id)
    panelNode = peer ? (
      <PeerPanel
        key={peer.id}
        peer={peer}
        serverConfigured={data?.server_configured ?? false}
        canMutate={canMutate}
        onClose={() => setPanel(null)}
        onDelete={() => setPendingDelete(peer)}
        onChanged={invalidate}
      />
    ) : null
  } else if (panel?.kind === 'new') {
    panelNode = (
      <NewPeerPanel
        onClose={() => setPanel(null)}
        onCreated={(peer) => { invalidate(); setPanel({ kind: 'edit', id: peer.id }); toast.show(`Device "${peer.name}" added`) }}
      />
    )
  }

  const peers = data?.peers ?? []
  const online = peers.filter((p) => p.status?.online).length

  return (
    <Page
      title="WireGuard devices"
      count={data?.total}
      subtitle="Devices that cannot use a proxy join this project through a WireGuard tunnel; everything they send is routed through the pool"
      toolbar={data && data.total > 0 ? (
        <Badge color={online > 0 ? 'green' : 'gray'}>{online} online</Badge>
      ) : undefined}
      actions={canMutate ? <Button size="sm" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> Add device</Button> : undefined}
      panel={panelNode}
    >
      {data && !data.server_configured && (
        <Alert variant="warning" className="text-sm">
          The tunnel's public endpoint is not set, so device configs cannot be completed yet.{' '}
          {isAdmin
            ? <Link to={`/projects/${selectedProjectId}/settings/wireguard`} className="underline">Set it in Settings, WireGuard.</Link>
            : 'Ask an administrator to set it under Settings, WireGuard.'}
        </Alert>
      )}
      {isLoading ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : peers.length === 0 ? (
        <EmptyState
          icon={<Router />}
          title="No devices yet"
          description="Add a device to get a WireGuard config and QR code. Import it on the device and its traffic exits through this project's proxies, no proxy settings needed."
          action={canMutate ? <Button size="sm" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> Add device</Button> : undefined}
        />
      ) : (
        <DataTable
          columns={columns}
          data={peers}
          getRowId={(row) => row.id}
          onRowClick={(row) => setPanel({ kind: 'edit', id: row.id })}
          activeRowId={panel?.kind === 'edit' ? panel.id : null}
          columnVisibility={panel ? { traffic: false, routing: false, actions: false } : {}}
        />
      )}
      {pendingDelete && (
        <ConfirmDialog
          title="Remove device?"
          message={<>Remove <b className="text-fg">{pendingDelete.name}</b>. Its config stops working immediately and its tunnel address is freed.</>}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => deleteMutation.mutate(pendingDelete.id)}
          isLoading={deleteMutation.isPending}
          confirmLabel="Remove"
        />
      )}
    </Page>
  )
}

function statusLabel(peer: WireGuardPeer): string {
  if (!peer.enabled) return 'Disabled'
  if (!peer.status) return 'Unknown'
  if (peer.status.online) return 'Online'
  return peer.status.last_handshake_at ? 'Offline' : 'Never connected'
}

function StatusBadge({ peer }: { peer: WireGuardPeer }) {
  const label = statusLabel(peer)
  const color = label === 'Online' ? 'green' : label === 'Disabled' ? 'gray' : label === 'Offline' ? 'yellow' : 'slate'
  const seen = peer.status?.last_handshake_at ? parseApiDate(peer.status.last_handshake_at) : null
  return (
    <span className="inline-flex items-center gap-2" title={seen ? `Last handshake ${seen.toLocaleString()}` : undefined}>
      <Badge color={color}>{label}</Badge>
    </span>
  )
}

function routingLabel(peer: WireGuardPeer): string {
  const parts: string[] = []
  if (peer.country) parts.push([peer.country, peer.state, peer.city?.replace(/_/g, ' ')].filter(Boolean).join(' / '))
  if (peer.session_id) parts.push(`session ${peer.session_id}`)
  return parts.join(', ')
}

// --- forms --------------------------------------------------------------------------------

interface RoutingFields { session_id: string; country: string; state: string; city: string }

function RoutingFieldset({ value, onChange, disabled }: { value: RoutingFields; onChange: (v: RoutingFields) => void; disabled?: boolean }) {
  const set = (patch: Partial<RoutingFields>) => onChange({ ...value, ...patch })
  return (
    <InspectorSection title="Routing">
      <p className="text-xs text-fg-muted">What a proxy client would put in its username. Leave empty to route with the project's defaults.</p>
      <div className="grid grid-cols-2 gap-3">
        <div className="col-span-2">
          <Label htmlFor="peer-session" className="inline-flex items-center gap-1">Sticky session <InfoTip>A fixed -sessid- for this device: with a sticky or dynamic-sessions connector, all of its traffic keeps one exit.</InfoTip></Label>
          <Input id="peer-session" value={value.session_id} onChange={(e) => set({ session_id: e.target.value })} placeholder="e.g. living-room" disabled={disabled} />
        </div>
        <div>
          <Label htmlFor="peer-country">Exit country</Label>
          <Input
            id="peer-country"
            value={value.country}
            onChange={(e) => {
              const country = e.target.value.toUpperCase()
              // State and city need a country: clearing it clears them too, or the save is refused.
              set(country ? { country } : { country, state: '', city: '' })
            }}
            placeholder="US"
            maxLength={2}
            disabled={disabled}
          />
        </div>
        <div>
          <Label htmlFor="peer-state">State</Label>
          <Input id="peer-state" value={value.state} onChange={(e) => set({ state: e.target.value.toUpperCase() })} placeholder="NY" maxLength={3} disabled={disabled || !value.country} />
        </div>
        <div className="col-span-2">
          <Label htmlFor="peer-city">City</Label>
          <Input id="peer-city" value={value.city} onChange={(e) => set({ city: e.target.value })} placeholder="New York" disabled={disabled || !value.country} />
        </div>
      </div>
    </InspectorSection>
  )
}

function NewPeerPanel({ onClose, onCreated }: { onClose: () => void; onCreated: (peer: WireGuardPeer) => void }) {
  const { selectedProjectId } = useProject()
  const toast = useToast()
  const [name, setName] = useState('')
  const [byo, setByo] = useState(false)
  const [publicKey, setPublicKey] = useState('')
  const [preshared, setPreshared] = useState(true)
  const [routing, setRouting] = useState<RoutingFields>({ session_id: '', country: '', state: '', city: '' })

  const mutation = useMutation({
    mutationFn: (data: WireGuardPeerCreate) => createWireGuardPeer(selectedProjectId!, data),
    onSuccess: onCreated,
    onError: (e: Error) => toast.show(e.message || 'Failed to add device', 'error'),
  })

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    mutation.mutate({
      name: name.trim(),
      public_key: byo ? publicKey.trim() : null,
      preshared,
      session_id: routing.session_id || null,
      country: routing.country || null,
      state: routing.state || null,
      city: routing.city || null,
    })
  }

  return (
    <Inspector
      title="Add device"
      subtitle="A new peer with its own tunnel address"
      onClose={onClose}
      footer={
        <>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="new-peer-form" size="sm" disabled={!name.trim() || (byo && !publicKey.trim()) || mutation.isPending}>Add device</Button>
        </>
      }
    >
      <form id="new-peer-form" onSubmit={submit} className="space-y-4">
        <div>
          <Label htmlFor="peer-name">Name</Label>
          <Input id="peer-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Living room TV" autoFocus />
        </div>
        <InspectorSection title="Keys">
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={!byo} onChange={(e) => setByo(!e.target.checked)} />
            <span>
              <span className="font-medium">Generate keys for me</span>
              <span className="block text-xs text-fg-muted">Octoprox keeps the device's private key so the config and QR code can be shown again. Untick to register a public key the device already has.</span>
            </span>
          </label>
          {byo && (
            <div>
              <Label htmlFor="peer-pubkey">Device public key</Label>
              <Input id="peer-pubkey" value={publicKey} onChange={(e) => setPublicKey(e.target.value)} placeholder="44 base64 characters" className="font-mono text-xs" />
            </div>
          )}
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={preshared} onChange={(e) => setPreshared(e.target.checked)} />
            <span>
              <span className="font-medium">Preshared key</span>
              <span className="block text-xs text-fg-muted">An extra symmetric key on top of the key pair. Recommended; every WireGuard client supports it.</span>
            </span>
          </label>
        </InspectorSection>
        <RoutingFieldset value={routing} onChange={setRouting} />
      </form>
    </Inspector>
  )
}

function PeerPanel({ peer, serverConfigured, canMutate, onClose, onDelete, onChanged }: {
  peer: WireGuardPeer
  serverConfigured: boolean
  canMutate: boolean
  onClose: () => void
  onDelete: () => void
  onChanged: () => void
}) {
  const { selectedProjectId } = useProject()
  const queryClient = useQueryClient()
  const toast = useToast()
  const [name, setName] = useState(peer.name)
  const [routing, setRouting] = useState<RoutingFields>({
    session_id: peer.session_id ?? '', country: peer.country ?? '', state: peer.state ?? '', city: peer.city ?? '',
  })
  const [confirmRotate, setConfirmRotate] = useState(false)
  useEffect(() => {
    setName(peer.name)
    setRouting({ session_id: peer.session_id ?? '', country: peer.country ?? '', state: peer.state ?? '', city: peer.city ?? '' })
  }, [peer])

  const dirty = name.trim() !== peer.name
    || routing.session_id !== (peer.session_id ?? '')
    || routing.country !== (peer.country ?? '')
    || routing.state !== (peer.state ?? '')
    || routing.city.replace(/\s+/g, '_').toLowerCase() !== (peer.city ?? '')

  const { data: config } = useQuery({
    queryKey: ['wireguard-peer-config', selectedProjectId, peer.id, peer.public_key, serverConfigured],
    queryFn: () => fetchWireGuardPeerConfig(selectedProjectId!, peer.id),
    enabled: !!selectedProjectId,
    refetchInterval: false,
  })

  const update = useMutation({
    mutationFn: (data: WireGuardPeerUpdate) => updateWireGuardPeer(selectedProjectId!, peer.id, data),
    onSuccess: () => { onChanged(); toast.show('Device saved') },
    onError: (e: Error) => toast.show(e.message || 'Failed to save device', 'error'),
  })
  const rotate = useMutation({
    mutationFn: () => rotateWireGuardPeerKeys(selectedProjectId!, peer.id),
    onSuccess: () => {
      onChanged()
      queryClient.invalidateQueries({ queryKey: ['wireguard-peer-config', selectedProjectId, peer.id] })
      setConfirmRotate(false)
      toast.show('New keys issued; load the new config on the device')
    },
    onError: (e: Error) => { setConfirmRotate(false); toast.show(e.message || 'Failed to rotate keys', 'error') },
  })

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    update.mutate({
      name: name.trim(),
      session_id: routing.session_id,
      country: routing.country,
      state: routing.state,
      city: routing.city,
    })
  }

  const seen = peer.status?.last_handshake_at ? formatDateTime(peer.status.last_handshake_at) : 'never'

  return (
    <Inspector
      title={peer.name}
      subtitle={`${peer.address} · ${statusLabel(peer)}`}
      onClose={onClose}
      footer={canMutate ? (
        <>
          <Button type="button" variant="danger-ghost" size="sm" onClick={onDelete}><Trash2 className="w-3.5 h-3.5" /> Remove</Button>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Close</Button>
          <Button type="submit" form="peer-form" size="sm" disabled={!dirty || !name.trim() || update.isPending}>Save changes</Button>
        </>
      ) : undefined}
    >
      {canMutate && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-line px-3 py-2">
          <div className="text-sm">
            <div className="font-medium">{peer.enabled ? 'Enabled' : 'Disabled'}</div>
            <div className="text-xs text-fg-muted">{peer.enabled ? 'The device can connect and its traffic is routed.' : 'The device is refused at the tunnel; its config stays valid.'}</div>
          </div>
          <Button type="button" size="sm" variant="outline" onClick={() => update.mutate({ enabled: !peer.enabled })} disabled={update.isPending}>
            {peer.enabled ? 'Disable' : 'Enable'}
          </Button>
        </div>
      )}

      <ConfigSection config={config ?? null} peer={peer} serverConfigured={serverConfigured} />

      <form id="peer-form" onSubmit={save} className="space-y-4">
        <div>
          <Label htmlFor="peer-name">Name</Label>
          <Input id="peer-name" value={name} onChange={(e) => setName(e.target.value)} disabled={!canMutate} />
        </div>
        <RoutingFieldset value={routing} onChange={setRouting} disabled={!canMutate} />
      </form>

      <InspectorSection
        title="Keys"
        action={canMutate ? (
          <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmRotate(true)}><KeyRound className="w-3.5 h-3.5" /> Rotate</Button>
        ) : undefined}
      >
        <KeyValue label="Public key" value={peer.public_key} mono />
        <KeyValue label="Private key" value={peer.has_private_key ? 'kept by Octoprox' : 'on the device only'} />
        <KeyValue label="Preshared key" value={peer.has_preshared_key ? 'yes' : 'no'} />
      </InspectorSection>

      <InspectorSection title="Connection">
        <KeyValue label="Last handshake" value={seen} />
        <KeyValue label="From" value={peer.status?.endpoint ?? '-'} mono />
        <KeyValue label="Received" value={peer.status ? formatBytes(peer.status.rx_bytes) : '-'} />
        <KeyValue label="Sent" value={peer.status ? formatBytes(peer.status.tx_bytes) : '-'} />
        <KeyValue label="Added" value={formatDateTime(peer.created_at)} />
      </InspectorSection>

      <InspectorSection title="Name resolution">
        <p className="text-xs text-fg-muted">
          Connections whose destination name could not be recovered are relayed by address: domain filters see an address and the exit may differ from the one that resolved it. A device resolving names outside the tunnel (encrypted DNS) is the usual cause.
        </p>
        <KeyValue
          label="Routed by address"
          value={peer.status ? <span className={peer.status.connections_by_address > 0 ? 'text-warning' : undefined}>{peer.status.connections_by_address}</span> : '-'}
        />
        <KeyValue label="Encrypted DNS blocked" value={peer.status ? peer.status.encrypted_dns_blocked : '-'} />
      </InspectorSection>

      {confirmRotate && (
        <ConfirmDialog
          title="Rotate this device's keys?"
          message={<>A new key pair{peer.has_preshared_key ? ' and preshared key' : ''} replace the current ones. The config loaded on <b className="text-fg">{peer.name}</b> stops working until the new one is imported.</>}
          confirmLabel="Rotate keys"
          onCancel={() => setConfirmRotate(false)}
          onConfirm={() => rotate.mutate()}
          isLoading={rotate.isPending}
        />
      )}
    </Inspector>
  )
}

function ConfigSection({ config, peer, serverConfigured }: { config: { filename: string; config: string; complete: boolean } | null; peer: WireGuardPeer; serverConfigured: boolean }) {
  const [copied, setCopied] = useState(false)
  const [showText, setShowText] = useState(false)
  if (!config) return null

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(config.config)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable in insecure contexts */ }
  }
  const download = () => {
    const blob = new Blob([config.config], { type: 'application/octet-stream' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = config.filename
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <InspectorSection
      title="Device configuration"
      action={
        <div className="flex items-center gap-1">
          <Button type="button" size="sm" variant="ghost" onClick={copy}>{copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />} {copied ? 'Copied' : 'Copy'}</Button>
          <Button type="button" size="sm" variant="ghost" onClick={download}><Download className="w-3.5 h-3.5" /> .conf</Button>
        </div>
      }
    >
      {!serverConfigured && (
        <Alert variant="warning" className="text-xs">The public endpoint is not set yet; this config has no Endpoint line a device could reach.</Alert>
      )}
      {!peer.has_private_key && (
        <Alert variant="info" className="text-xs">This device holds its own private key. Paste it into the config where marked; the QR code cannot include it.</Alert>
      )}
      <div className="flex gap-4 items-start">
        {peer.has_private_key && (
          <div className="flex-none rounded-lg bg-white p-2 border border-line" title="Scan with the WireGuard app">
            <QRCodeSVG value={config.config} size={168} level="M" />
          </div>
        )}
        <div className="min-w-0 flex-1 text-xs text-fg-muted space-y-2">
          <p>Phones and tablets: open the WireGuard app, add a tunnel from QR code.</p>
          <p>Routers, TVs and computers: import <span className="font-mono text-fg">{config.filename}</span>, or paste the text.</p>
          <p>Everything the device sends goes through the tunnel; names resolve at the exit.</p>
          <button type="button" className="underline" onClick={() => setShowText((v) => !v)}>{showText ? 'Hide text' : 'Show text'}</button>
        </div>
      </div>
      {showText && (
        <pre className="text-[11px] font-mono bg-surface-raised rounded-lg p-3 overflow-x-auto whitespace-pre">{config.config}</pre>
      )}
    </InspectorSection>
  )
}
