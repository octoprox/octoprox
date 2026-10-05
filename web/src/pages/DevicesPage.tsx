// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Check, Copy, Download, KeyRound, Plus, Router, ShieldCheck, Trash2 } from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import {
  createOpenVpnPeer, createWireGuardPeer, deleteOpenVpnPeer, deleteWireGuardPeer,
  fetchOpenVpnPeerConfig, fetchOpenVpnPeerMetricsHistory, fetchOpenVpnPeers,
  fetchWireGuardPeerConfig, fetchWireGuardPeerMetricsHistory, fetchWireGuardPeers,
  rotateOpenVpnPeerCertificate, rotateWireGuardPeerKeys, updateOpenVpnPeer, updateWireGuardPeer,
  OpenVpnPeer, TunnelPeerMetrics, TunnelPeerMetricsHistoryResponse, TunnelPeerMetricsSnapshot, WireGuardPeer, WireGuardPeerUpdate,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useTheme } from '../contexts/ThemeContext'
import { useToast } from '../contexts/ToastContext'
import { DataTable } from '../components/DataTable'
import { Page, EmptyState } from '../components/layout/Page'
import { formatBytes, formatDateTime, parseApiDate, plural } from '../utils/format'
import {
  Alert, Badge, Button, ConfirmDialog, InfoTip, Input, Inspector, InspectorSection, KeyValue, Label, Segmented,
} from '../components/ui'

// --- one shape for every tunnel protocol's devices -------------------------------------------

export type Protocol = 'wireguard' | 'openvpn'

/** A device as the page shows it, whichever tunnel it connects through. */
interface Device {
  protocol: Protocol
  id: string
  project_id: string
  name: string
  address: string
  enabled: boolean
  session_id: string | null
  country: string | null
  state: string | null
  city: string | null
  created_at: string
  metrics: TunnelPeerMetrics
  status: {
    online: boolean
    live: boolean
    last_seen_at: string | null
    endpoint: string | null
    rx_bytes: number
    tx_bytes: number
  }
  credential:
    | { kind: 'keys'; public_key: string; has_private_key: boolean; has_preshared_key: boolean }
    | { kind: 'certificate'; serial: string; expires_at: string }
}

interface DeviceConfig { filename: string; config: string; complete: boolean }

interface CreateInput {
  name: string
  session_id: string | null
  country: string | null
  state: string | null
  city: string | null
  /** WireGuard only. */
  public_key?: string | null
  preshared?: boolean
}

/** What differs per protocol, behind one interface the page talks to. */
interface Adapter {
  label: string
  icon: React.ReactNode
  settingsSegment: string
  /** The config file's extension, as the download button names it. */
  fileLabel: string
  /** Whether the config is small enough for a QR code and the apps scan one. */
  qr: boolean
  list(projectId: string): Promise<{ devices: Device[]; serverConfigured: boolean }>
  create(projectId: string, input: CreateInput): Promise<Device>
  update(projectId: string, id: string, data: WireGuardPeerUpdate): Promise<Device>
  remove(projectId: string, id: string): Promise<void>
  rotate(projectId: string, id: string): Promise<Device>
  config(projectId: string, id: string): Promise<DeviceConfig>
  history(projectId: string, id: string, range: string): Promise<TunnelPeerMetricsHistoryResponse>
}

function fromWireGuard(p: WireGuardPeer): Device {
  return {
    protocol: 'wireguard', id: p.id, project_id: p.project_id, name: p.name, address: p.address, enabled: p.enabled,
    session_id: p.session_id, country: p.country, state: p.state, city: p.city, created_at: p.created_at, metrics: p.metrics,
    status: {
      online: p.status.online, live: p.status.live, last_seen_at: p.status.last_handshake_at, endpoint: p.status.endpoint,
      rx_bytes: p.status.rx_bytes, tx_bytes: p.status.tx_bytes,
    },
    credential: { kind: 'keys', public_key: p.public_key, has_private_key: p.has_private_key, has_preshared_key: p.has_preshared_key },
  }
}

function fromOpenVpn(p: OpenVpnPeer): Device {
  return {
    protocol: 'openvpn', id: p.id, project_id: p.project_id, name: p.name, address: p.address, enabled: p.enabled,
    session_id: p.session_id, country: p.country, state: p.state, city: p.city, created_at: p.created_at, metrics: p.metrics,
    status: {
      online: p.status.online, live: p.status.live, last_seen_at: p.status.last_seen_at, endpoint: p.status.endpoint,
      rx_bytes: p.status.rx_bytes, tx_bytes: p.status.tx_bytes,
    },
    credential: { kind: 'certificate', serial: p.serial, expires_at: p.certificate_expires_at },
  }
}

const ADAPTERS: Record<Protocol, Adapter> = {
  wireguard: {
    label: 'WireGuard',
    icon: <Router className="w-4 h-4" />,
    settingsSegment: 'wireguard',
    fileLabel: '.conf',
    qr: true,
    list: async (projectId) => {
      const r = await fetchWireGuardPeers(projectId)
      return { devices: r.peers.map(fromWireGuard), serverConfigured: r.server_configured }
    },
    create: async (projectId, input) => fromWireGuard(await createWireGuardPeer(projectId, {
      name: input.name, public_key: input.public_key ?? null, preshared: input.preshared ?? true,
      session_id: input.session_id, country: input.country, state: input.state, city: input.city,
    })),
    update: async (projectId, id, data) => fromWireGuard(await updateWireGuardPeer(projectId, id, data)),
    remove: deleteWireGuardPeer,
    rotate: async (projectId, id) => fromWireGuard(await rotateWireGuardPeerKeys(projectId, id)),
    config: fetchWireGuardPeerConfig,
    history: fetchWireGuardPeerMetricsHistory,
  },
  openvpn: {
    label: 'OpenVPN',
    icon: <ShieldCheck className="w-4 h-4" />,
    settingsSegment: 'openvpn',
    fileLabel: '.ovpn',
    qr: false,
    list: async (projectId) => {
      const r = await fetchOpenVpnPeers(projectId)
      return { devices: r.peers.map(fromOpenVpn), serverConfigured: r.server_configured }
    },
    create: async (projectId, input) => fromOpenVpn(await createOpenVpnPeer(projectId, {
      name: input.name, session_id: input.session_id, country: input.country, state: input.state, city: input.city,
    })),
    update: async (projectId, id, data) => fromOpenVpn(await updateOpenVpnPeer(projectId, id, data)),
    remove: deleteOpenVpnPeer,
    rotate: async (projectId, id) => fromOpenVpn(await rotateOpenVpnPeerCertificate(projectId, id)),
    config: fetchOpenVpnPeerConfig,
    history: fetchOpenVpnPeerMetricsHistory,
  },
}
const PROTOCOLS: Protocol[] = ['wireguard', 'openvpn']

type PanelState = { kind: 'edit'; protocol: Protocol; id: string } | { kind: 'new' } | null

/**
 * The project's tunnel devices: anything that cannot speak to a proxy (TVs,
 * consoles, routers, phones) joins the pool by connecting to a tunnel, over
 * WireGuard or OpenVPN. Each device has its own address and the routing a
 * proxy client would put in its username; the protocol is picked per device.
 */
export default function DevicesPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { canMutate, isAdmin } = useAuth()
  const toast = useToast()
  const [panel, setPanel] = useState<PanelState>(null)
  const [pendingDelete, setPendingDelete] = useState<Device | null>(null)

  // One list per protocol, merged below; sessions and counters move, so both poll.
  const wireguard = useQuery({
    queryKey: ['tunnel-devices', 'wireguard', selectedProjectId],
    queryFn: () => ADAPTERS.wireguard.list(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: 10_000,
  })
  const openvpn = useQuery({
    queryKey: ['tunnel-devices', 'openvpn', selectedProjectId],
    queryFn: () => ADAPTERS.openvpn.list(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: 10_000,
  })
  const lists = [{ protocol: 'wireguard' as Protocol, query: wireguard }, { protocol: 'openvpn' as Protocol, query: openvpn }]
  const isLoading = wireguard.isLoading || openvpn.isLoading
  const devices = useMemo(
    () => [...(wireguard.data?.devices ?? []), ...(openvpn.data?.devices ?? [])]
      .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id)),
    [wireguard.data, openvpn.data],
  )
  const unconfigured = lists.filter((l) => l.query.data && !l.query.data.serverConfigured).map((l) => l.protocol)
  const invalidate = (protocol?: Protocol) =>
    queryClient.invalidateQueries({ queryKey: protocol ? ['tunnel-devices', protocol, selectedProjectId] : ['tunnel-devices'] })

  const deleteMutation = useMutation({
    mutationFn: (device: Device) => ADAPTERS[device.protocol].remove(selectedProjectId!, device.id),
    onSuccess: (_r, device) => {
      invalidate(device.protocol)
      if (panel?.kind === 'edit' && panel.id === device.id) setPanel(null)
      setPendingDelete(null)
      toast.show('Device removed')
    },
    onError: (e: Error) => { setPendingDelete(null); toast.show(e.message || 'Failed to remove device', 'error') },
  })

  const columns: ColumnDef<Device>[] = useMemo(() => [
    {
      accessorKey: 'name',
      header: 'Device',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-2.5 max-w-full">
          <span className="text-fg-subtle flex-none">{ADAPTERS[row.original.protocol].icon}</span>
          <span className={row.original.enabled ? 'font-medium truncate' : 'font-medium truncate text-fg-muted line-through'}>{row.original.name}</span>
        </span>
      ),
    },
    {
      id: 'protocol',
      header: 'Tunnel',
      size: 110,
      accessorFn: (row: Device) => ADAPTERS[row.protocol].label,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <Badge color="slate">{getValue<string>()}</Badge>,
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
      accessorFn: (row: Device) => statusLabel(row),
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => <StatusBadge device={row.original} />,
    },
    {
      id: 'routing',
      header: 'Routing',
      enableSorting: false,
      accessorFn: (row: Device) => routingLabel(row),
      cell: ({ getValue }) => <span className="text-fg-muted truncate block">{getValue<string>() || 'project default'}</span>,
    },
    {
      id: 'traffic',
      header: 'Traffic',
      size: 170,
      accessorFn: (row: Device) => row.metrics.bytes_sent + row.metrics.bytes_received,
      cell: ({ row }) => {
        const m = row.original.metrics
        const total = m.bytes_sent + m.bytes_received
        return total > 0 || m.request_count > 0
          ? <span className="text-fg-muted tabular-nums text-xs" title="What the pool relayed for this device, all time">{formatBytes(total)} · {m.request_count} {plural(m.request_count, 'connection', 'connections')}</span>
          : <span className="text-fg-subtle">-</span>
      },
    },
    ...(canMutate ? [{
      id: 'actions',
      header: '',
      size: 56,
      enableSorting: false,
      cell: ({ row }: { row: { original: Device } }) => (
        <div className="flex justify-end">
          <button onClick={() => setPendingDelete(row.original)} className="p-1 rounded text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Remove">
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    }] as ColumnDef<Device>[] : []),
  ], [canMutate])

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'edit') {
    const device = devices.find((d) => d.protocol === panel.protocol && d.id === panel.id)
    panelNode = device ? (
      <DevicePanel
        key={`${device.protocol}:${device.id}`}
        device={device}
        serverConfigured={!unconfigured.includes(device.protocol)}
        canMutate={canMutate}
        onClose={() => setPanel(null)}
        onDelete={() => setPendingDelete(device)}
        onChanged={() => invalidate(device.protocol)}
      />
    ) : null
  } else if (panel?.kind === 'new') {
    panelNode = (
      <NewDevicePanel
        unconfigured={unconfigured}
        onClose={() => setPanel(null)}
        onCreated={(device) => { invalidate(device.protocol); setPanel({ kind: 'edit', protocol: device.protocol, id: device.id }); toast.show(`Device "${device.name}" added`) }}
      />
    )
  }

  const online = devices.filter((d) => d.status.online).length
  const total = wireguard.data && openvpn.data ? devices.length : undefined

  return (
    <Page
      title="Devices"
      count={total}
      subtitle="Devices that cannot use a proxy join this project through a WireGuard or OpenVPN tunnel; everything they send is routed through the pool"
      toolbar={total ? <Badge color={online > 0 ? 'green' : 'gray'}>{online} online</Badge> : undefined}
      actions={canMutate ? <Button size="sm" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> Add device</Button> : undefined}
      panel={panelNode}
    >
      {unconfigured.filter((p) => devices.some((d) => d.protocol === p)).map((protocol) => (
        <Alert key={protocol} variant="warning" className="text-sm">
          The {ADAPTERS[protocol].label} public endpoint is not set, so its device configs cannot be completed yet.{' '}
          {isAdmin
            ? <Link to={`/projects/${selectedProjectId}/settings/${ADAPTERS[protocol].settingsSegment}`} className="underline">Set it in Settings, {ADAPTERS[protocol].label}.</Link>
            : `Ask an administrator to set it under Settings, ${ADAPTERS[protocol].label}.`}
        </Alert>
      ))}
      {isLoading ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : devices.length === 0 ? (
        <EmptyState
          icon={<Router />}
          title="No devices yet"
          description="Add a device to get a WireGuard config and QR code, or an OpenVPN profile. Import it on the device and its traffic exits through this project's proxies, no proxy settings needed."
          action={canMutate ? <Button size="sm" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> Add device</Button> : undefined}
        />
      ) : (
        <DataTable
          columns={columns}
          data={devices}
          getRowId={(row) => `${row.protocol}:${row.id}`}
          onRowClick={(row) => setPanel({ kind: 'edit', protocol: row.protocol, id: row.id })}
          activeRowId={panel?.kind === 'edit' ? `${panel.protocol}:${panel.id}` : null}
          columnVisibility={panel ? { traffic: false, routing: false, actions: false } : {}}
        />
      )}
      {pendingDelete && (
        <ConfirmDialog
          title="Remove device?"
          message={<>Remove <b className="text-fg">{pendingDelete.name}</b>. Its config stops working immediately and its tunnel address is freed.</>}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => deleteMutation.mutate(pendingDelete)}
          isLoading={deleteMutation.isPending}
          confirmLabel="Remove"
        />
      )}
    </Page>
  )
}

function statusLabel(device: Device): string {
  if (!device.enabled) return 'Disabled'
  if (device.status.online) return 'Online'
  return device.status.last_seen_at ? 'Offline' : 'Never connected'
}

function StatusBadge({ device }: { device: Device }) {
  const label = statusLabel(device)
  const color = label === 'Online' ? 'green' : label === 'Disabled' ? 'gray' : label === 'Offline' ? 'yellow' : 'slate'
  const seen = device.status.last_seen_at ? parseApiDate(device.status.last_seen_at) : null
  return (
    <span className="inline-flex items-center gap-2" title={seen ? `Last seen ${seen.toLocaleString()}` : undefined}>
      <Badge color={color}>{label}</Badge>
    </span>
  )
}

function routingLabel(device: Device): string {
  const parts: string[] = []
  if (device.country) parts.push([device.country, device.state, device.city?.replace(/_/g, ' ')].filter(Boolean).join(' / '))
  if (device.session_id) parts.push(`session ${device.session_id}`)
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

function NewDevicePanel({ unconfigured, onClose, onCreated }: { unconfigured: Protocol[]; onClose: () => void; onCreated: (device: Device) => void }) {
  const { selectedProjectId } = useProject()
  const toast = useToast()
  const [protocol, setProtocol] = useState<Protocol>('wireguard')
  const [name, setName] = useState('')
  const [byo, setByo] = useState(false)
  const [publicKey, setPublicKey] = useState('')
  const [preshared, setPreshared] = useState(true)
  const [routing, setRouting] = useState<RoutingFields>({ session_id: '', country: '', state: '', city: '' })

  const mutation = useMutation({
    mutationFn: (input: CreateInput) => ADAPTERS[protocol].create(selectedProjectId!, input),
    onSuccess: onCreated,
    onError: (e: Error) => toast.show(e.message || 'Failed to add device', 'error'),
  })

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    mutation.mutate({
      name: name.trim(),
      public_key: protocol === 'wireguard' && byo ? publicKey.trim() : null,
      preshared,
      session_id: routing.session_id || null,
      country: routing.country || null,
      state: routing.state || null,
      city: routing.city || null,
    })
  }
  const incomplete = !name.trim() || (protocol === 'wireguard' && byo && !publicKey.trim())

  return (
    <Inspector
      title="Add device"
      subtitle="A new device with its own tunnel address"
      onClose={onClose}
      footer={
        <>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="new-peer-form" size="sm" disabled={incomplete || mutation.isPending}>Add device</Button>
        </>
      }
    >
      <form id="new-peer-form" onSubmit={submit} className="space-y-4">
        <div>
          <Label htmlFor="peer-name">Name</Label>
          <Input id="peer-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Living room TV" autoFocus />
        </div>
        <div>
          <Label className="inline-flex items-center gap-1">Tunnel <InfoTip>WireGuard is lighter and has a QR code for phones; pick OpenVPN for a router or an older device without WireGuard, or where only TCP gets through.</InfoTip></Label>
          <Segmented options={PROTOCOLS.map((p) => ({ value: p, label: ADAPTERS[p].label }))} value={protocol} onChange={setProtocol} className="mt-1" />
          {unconfigured.includes(protocol) && (
            <p className="text-xs text-warning mt-2">The {ADAPTERS[protocol].label} public endpoint is not set yet; the device can be added but its config cannot be completed until it is.</p>
          )}
        </div>
        {protocol === 'wireguard' ? (
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
        ) : (
          <InspectorSection title="Certificate">
            <p className="text-xs text-fg-muted">The install's OpenVPN CA issues the device a certificate, kept so the profile can be shown again. The profile carries the CA, the certificate, its key and the tls-crypt key; nothing else needs to be installed on the device.</p>
          </InspectorSection>
        )}
        <RoutingFieldset value={routing} onChange={setRouting} />
      </form>
    </Inspector>
  )
}

function DevicePanel({ device, serverConfigured, canMutate, onClose, onDelete, onChanged }: {
  device: Device
  serverConfigured: boolean
  canMutate: boolean
  onClose: () => void
  onDelete: () => void
  onChanged: () => void
}) {
  const adapter = ADAPTERS[device.protocol]
  const { selectedProjectId } = useProject()
  const queryClient = useQueryClient()
  const toast = useToast()
  const [name, setName] = useState(device.name)
  const [routing, setRouting] = useState<RoutingFields>({
    session_id: device.session_id ?? '', country: device.country ?? '', state: device.state ?? '', city: device.city ?? '',
  })
  const [confirmRotate, setConfirmRotate] = useState(false)
  useEffect(() => {
    setName(device.name)
    setRouting({ session_id: device.session_id ?? '', country: device.country ?? '', state: device.state ?? '', city: device.city ?? '' })
  }, [device])

  const dirty = name.trim() !== device.name
    || routing.session_id !== (device.session_id ?? '')
    || routing.country !== (device.country ?? '')
    || routing.state !== (device.state ?? '')
    || routing.city.replace(/\s+/g, '_').toLowerCase() !== (device.city ?? '')

  const credentialKey = device.credential.kind === 'keys' ? device.credential.public_key : device.credential.serial
  const { data: config } = useQuery({
    queryKey: ['tunnel-device-config', device.protocol, selectedProjectId, device.id, credentialKey, serverConfigured],
    queryFn: () => adapter.config(selectedProjectId!, device.id),
    enabled: !!selectedProjectId,
    refetchInterval: false,
  })

  const update = useMutation({
    mutationFn: (data: WireGuardPeerUpdate) => adapter.update(selectedProjectId!, device.id, data),
    onSuccess: () => { onChanged(); toast.show('Device saved') },
    onError: (e: Error) => toast.show(e.message || 'Failed to save device', 'error'),
  })
  const rotate = useMutation({
    mutationFn: () => adapter.rotate(selectedProjectId!, device.id),
    onSuccess: () => {
      onChanged()
      queryClient.invalidateQueries({ queryKey: ['tunnel-device-config', device.protocol, selectedProjectId, device.id] })
      setConfirmRotate(false)
      toast.show(device.credential.kind === 'keys' ? 'New keys issued; load the new config on the device' : 'New certificate issued; load the new profile on the device')
    },
    onError: (e: Error) => { setConfirmRotate(false); toast.show(e.message || 'Failed to rotate', 'error') },
  })

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    update.mutate({ name: name.trim(), session_id: routing.session_id, country: routing.country, state: routing.state, city: routing.city })
  }

  const seen = device.status.last_seen_at ? formatDateTime(device.status.last_seen_at) : 'never'
  const isWireGuard = device.credential.kind === 'keys'

  return (
    <Inspector
      title={device.name}
      subtitle={`${adapter.label} · ${device.address} · ${statusLabel(device)}`}
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
            <div className="font-medium">{device.enabled ? 'Enabled' : 'Disabled'}</div>
            <div className="text-xs text-fg-muted">{device.enabled ? 'The device can connect and its traffic is routed.' : 'The device is refused at the tunnel; its config stays valid.'}</div>
          </div>
          <Button type="button" size="sm" variant="outline" onClick={() => update.mutate({ enabled: !device.enabled })} disabled={update.isPending}>
            {device.enabled ? 'Disable' : 'Enable'}
          </Button>
        </div>
      )}

      <ConfigSection config={config ?? null} device={device} adapter={adapter} serverConfigured={serverConfigured} />

      <form id="peer-form" onSubmit={save} className="space-y-4">
        <div>
          <Label htmlFor="peer-name">Name</Label>
          <Input id="peer-name" value={name} onChange={(e) => setName(e.target.value)} disabled={!canMutate} />
        </div>
        <RoutingFieldset value={routing} onChange={setRouting} disabled={!canMutate} />
      </form>

      <InspectorSection
        title={isWireGuard ? 'Keys' : 'Certificate'}
        action={canMutate ? (
          <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmRotate(true)}><KeyRound className="w-3.5 h-3.5" /> Rotate</Button>
        ) : undefined}
      >
        {device.credential.kind === 'keys' ? (
          <>
            <KeyValue label="Public key" value={device.credential.public_key} mono />
            <KeyValue label="Private key" value={device.credential.has_private_key ? 'kept by Octoprox' : 'on the device only'} />
            <KeyValue label="Preshared key" value={device.credential.has_preshared_key ? 'yes' : 'no'} />
          </>
        ) : (
          <>
            <KeyValue label="Common name" value={device.id} mono />
            <KeyValue label="Serial" value={device.credential.serial} mono />
            <KeyValue label="Expires" value={formatDateTime(device.credential.expires_at)} />
            <KeyValue label="Private key" value="kept by Octoprox" />
          </>
        )}
      </InspectorSection>

      <InspectorSection title="Connection">
        <KeyValue
          label={isWireGuard ? 'Last handshake' : (device.status.online ? 'Connected since' : 'Last connected')}
          value={<span title={device.status.live ? 'As the instance carrying the tunnel reports it right now' : 'The last sighting on record; no instance is reporting this device right now'}>{seen}{device.status.last_seen_at && !device.status.live ? <span className="text-fg-subtle"> · on record</span> : null}</span>}
        />
        <KeyValue label="From" value={device.status.endpoint ?? '-'} mono />
        <KeyValue
          label={isWireGuard ? 'Tunnel counters' : 'Session counters'}
          value={device.status.live
            ? <span title={isWireGuard ? "The carrying instance's interface counters since it came up: wire bytes, handshakes and DNS included" : "The daemon's counters for the current session: wire bytes, control channel and DNS included"}>{formatBytes(device.status.rx_bytes)} in, {formatBytes(device.status.tx_bytes)} out</span>
            : <span className="text-fg-subtle">not carried right now</span>}
        />
        <KeyValue label="Added" value={formatDateTime(device.created_at)} />
      </InspectorSection>

      <DeviceTrafficSection device={device} adapter={adapter} projectId={selectedProjectId!} />

      <InspectorSection title="Name resolution">
        <p className="text-xs text-fg-muted">
          Connections whose destination name could not be recovered are relayed by address: domain filters see an address and the exit may differ from the one that resolved it. A device resolving names outside the tunnel (encrypted DNS) is the usual cause. Both counts are all time; the chart above has them per interval.
        </p>
        <KeyValue
          label="Routed by address"
          value={<span className={device.metrics.connections_by_address > 0 ? 'text-warning' : undefined}>{device.metrics.connections_by_address}</span>}
        />
        <KeyValue label="Encrypted DNS blocked" value={device.metrics.encrypted_dns_blocked} />
      </InspectorSection>

      {confirmRotate && (
        <ConfirmDialog
          title={isWireGuard ? "Rotate this device's keys?" : "Rotate this device's certificate?"}
          message={isWireGuard
            ? <>A new key pair{device.credential.kind === 'keys' && device.credential.has_preshared_key ? ' and preshared key' : ''} replace the current ones. The config loaded on <b className="text-fg">{device.name}</b> stops working until the new one is imported.</>
            : <>A new certificate and key replace the current ones. The profile loaded on <b className="text-fg">{device.name}</b> stops working, and any open session is dropped, until the new profile is imported.</>}
          confirmLabel={isWireGuard ? 'Rotate keys' : 'Rotate certificate'}
          onCancel={() => setConfirmRotate(false)}
          onConfirm={() => rotate.mutate()}
          isLoading={rotate.isPending}
        />
      )}
    </Inspector>
  )
}

type ChartRange = '24h' | '7d' | '30d'
const CHART_RANGES: ChartRange[] = ['24h', '7d', '30d']
type ChartSeries = 'bytes' | 'connections' | 'by_address'
const SERIES: { value: ChartSeries; label: string }[] = [
  { value: 'bytes', label: 'Bytes' }, { value: 'connections', label: 'Connections' }, { value: 'by_address', label: 'Name resolution' },
]

function tick(epoch: number, range: ChartRange): string {
  const d = new Date(epoch)
  if (range === '24h') return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function toPoints(snapshots: TunnelPeerMetricsSnapshot[]) {
  return snapshots.map((s) => ({
    time: parseApiDate(s.timestamp)?.getTime() ?? 0,
    bytes: s.bytes_sent + s.bytes_received,
    connections: s.request_count,
    // Both name-resolution signals: relayed by address and encrypted DNS closed.
    by_address: s.connections_by_address + s.encrypted_dns_blocked,
  }))
}

/** What the pool relayed for the device: totals and a history chart, from the device's own metrics. */
function DeviceTrafficSection({ device, adapter, projectId }: { device: Device; adapter: Adapter; projectId: string }) {
  const { isDark } = useTheme()
  const [range, setRange] = useState<ChartRange>('24h')
  const [series, setSeries] = useState<ChartSeries>('bytes')
  const { data: history } = useQuery({
    queryKey: ['tunnel-device-history', device.protocol, projectId, device.id, range],
    queryFn: () => adapter.history(projectId, device.id, range),
    refetchInterval: 60_000,
  })
  const points = useMemo(() => toPoints(history?.snapshots ?? []), [history])
  const rangeTotal = useMemo(() => points.reduce((a, p) => a + p[series], 0), [points, series])

  const m = device.metrics
  const successRate = m.request_count > 0 ? Math.round((m.success_count / m.request_count) * 100) : null
  const gridColor = isDark ? '#374151' : '#e5e7eb'
  const tickColor = '#9ca3af'
  const tooltipStyle = isDark
    ? { backgroundColor: '#1f2937', border: '1px solid #374151', color: '#f3f4f6', borderRadius: 8, fontSize: 12 }
    : { borderRadius: 8, fontSize: 12, border: '1px solid #e5e7eb' }
  const lineColor = series === 'by_address' ? '#d97706' : '#2563eb'
  const formatValue = (v: number) => (series === 'bytes' ? formatBytes(v) : String(v))
  const seriesLabel = SERIES.find((s) => s.value === series)?.label ?? ''

  return (
    <InspectorSection title="Traffic">
      <p className="text-xs text-fg-muted">
        What the pool relayed for this device, counted like any proxy request and kept as history. The totals are cluster-wide and survive restarts; the tunnel's own counters are under Connection.
      </p>
      <KeyValue label="Relayed" value={<>{formatBytes(m.bytes_sent + m.bytes_received)} <span className="text-fg-subtle font-normal">· {formatBytes(m.bytes_sent)} up, {formatBytes(m.bytes_received)} down</span></>} />
      <KeyValue
        label="Connections"
        value={<>{m.request_count}{successRate != null && <span className="text-fg-subtle font-normal"> · {successRate}% reached the exit, {Math.round(m.avg_latency_ms)} ms to connect</span>}</>}
      />
      <div className="pt-1">
        <div className="flex items-center justify-between gap-2 mb-1 flex-wrap">
          <span className="text-xs text-fg-muted tabular-nums">{formatValue(rangeTotal)} over {range}</span>
          <div className="flex items-center gap-2">
            <Segmented options={SERIES} value={series} onChange={setSeries} size="sm" />
            <Segmented options={CHART_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} size="sm" />
          </div>
        </div>
        {points.length === 0 ? (
          <p className="text-fg-subtle text-xs text-center h-[90px] flex items-center justify-center">No traffic in this range</p>
        ) : (
          <ResponsiveContainer width="100%" height={90}>
            <AreaChart data={points} margin={{ top: 4, right: 4, left: -8, bottom: 0 }}>
              <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
              <XAxis dataKey="time" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={(v: number) => tick(v, range)} tick={{ fontSize: 10, fill: tickColor }} axisLine={false} tickLine={false} minTickGap={40} />
              <YAxis tick={{ fontSize: 10, fill: tickColor }} axisLine={false} tickLine={false} tickFormatter={(v: number) => (series === 'bytes' ? formatBytes(v) : String(v))} width={52} allowDecimals={false} />
              <Tooltip labelFormatter={(v: number) => new Date(v).toLocaleString()} formatter={(v: number) => [formatValue(v), seriesLabel]} contentStyle={tooltipStyle} />
              <Area type="monotone" dataKey={series} name={seriesLabel} stroke={lineColor} strokeWidth={2} fill={lineColor} fillOpacity={0.08} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </InspectorSection>
  )
}

function ConfigSection({ config, device, adapter, serverConfigured }: { config: DeviceConfig | null; device: Device; adapter: Adapter; serverConfigured: boolean }) {
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
  const keysOnDevice = device.credential.kind === 'keys' && !device.credential.has_private_key
  const showQr = adapter.qr && !keysOnDevice

  return (
    <InspectorSection
      title="Device configuration"
      action={
        <div className="flex items-center gap-1">
          <Button type="button" size="sm" variant="ghost" onClick={copy}>{copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />} {copied ? 'Copied' : 'Copy'}</Button>
          <Button type="button" size="sm" variant="ghost" onClick={download}><Download className="w-3.5 h-3.5" /> {adapter.fileLabel}</Button>
        </div>
      }
    >
      {!serverConfigured && (
        <Alert variant="warning" className="text-xs">The {adapter.label} public endpoint is not set yet; this config has no endpoint a device could reach.</Alert>
      )}
      {keysOnDevice && (
        <Alert variant="info" className="text-xs">This device holds its own private key. Paste it into the config where marked; the QR code cannot include it.</Alert>
      )}
      <div className="flex gap-4 items-start">
        {showQr && (
          <div className="flex-none rounded-lg bg-white p-2 border border-line" title="Scan with the WireGuard app">
            <QRCodeSVG value={config.config} size={168} level="M" />
          </div>
        )}
        <div className="min-w-0 flex-1 text-xs text-fg-muted space-y-2">
          {device.protocol === 'wireguard' ? (
            <>
              <p>Phones and tablets: open the WireGuard app, add a tunnel from QR code.</p>
              <p>Routers, TVs and computers: import <span className="font-mono text-fg">{config.filename}</span>, or paste the text.</p>
            </>
          ) : (
            <>
              <p>Phones, tablets and computers: import <span className="font-mono text-fg">{config.filename}</span> into OpenVPN Connect or any OpenVPN client.</p>
              <p>Routers (OpenWrt, pfSense, ASUS, Synology and others): upload the same file as the client profile. It carries the CA, the device's certificate and key, and the tls-crypt key.</p>
            </>
          )}
          <p>Everything the device sends goes through the tunnel; names resolve at the exit.</p>
          <button type="button" className="underline" onClick={() => setShowText((v) => !v)}>{showText ? 'Hide text' : 'Show text'}</button>
        </div>
      </div>
      {showText && (
        <pre className="text-[11px] font-mono bg-surface-raised rounded-lg p-3 overflow-x-auto whitespace-pre max-h-80 overflow-y-auto">{config.config}</pre>
      )}
    </InspectorSection>
  )
}
