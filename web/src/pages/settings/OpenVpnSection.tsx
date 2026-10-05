// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, ShieldCheck } from 'lucide-react'
import {
  fetchOpenVpnSettings, rotateOpenVpnIdentity, updateOpenVpnSettings,
  OpenVpnProtocol, OpenVpnServerSettings, OpenVpnServerSettingsDoc, OpenVpnStatus,
} from '../../api/client'
import { useToast } from '../../contexts/ToastContext'
import { Page } from '../../components/layout/Page'
import { formatDateTime } from '../../utils/format'
import { Alert, Badge, Button, Card, CardHeader, ConfirmDialog, InfoTip, Input, Label, Select } from '../../components/ui'

/**
 * Admin page for the OpenVPN endpoint: the install's CA and what every device
 * profile says about reaching it, plus what the answering instance is doing
 * about the daemon.
 */
export default function OpenVpnSection() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const { data, isLoading } = useQuery({ queryKey: ['openvpn-settings'], queryFn: fetchOpenVpnSettings, refetchInterval: 15_000 })
  const [confirmRotate, setConfirmRotate] = useState(false)

  const rotate = useMutation({
    mutationFn: rotateOpenVpnIdentity,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['openvpn-settings'] })
      queryClient.invalidateQueries({ queryKey: ['tunnel-devices', 'openvpn'] })
      queryClient.invalidateQueries({ queryKey: ['tunnel-device-config', 'openvpn'] })
      setConfirmRotate(false)
      toast.show('Identity rotated; every OpenVPN device needs its profile again')
    },
    onError: (e: Error) => { setConfirmRotate(false); toast.show(e.message || 'Failed to rotate the identity', 'error') },
  })

  return (
    <Page
      title="OpenVPN"
      subtitle="Devices without proxy support join a project's pool through an OpenVPN tunnel terminated by Octoprox."
      toolbar={data && <StateBadge status={data.status} />}
    >
      {isLoading || !data ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : (
        <div className="grid gap-4 @4xl:grid-cols-[minmax(0,1fr)_380px]">
          <div className="space-y-4">
            <EndpointForm settings={data} />
            <Card className="p-5">
              <CardHeader
                title="Server identity"
                action={<Button size="sm" variant="outline" onClick={() => setConfirmRotate(true)}><KeyRound className="w-3.5 h-3.5" /> Rotate identity</Button>}
              />
              <p className="text-sm text-fg-muted mt-1">A private certificate authority for the whole install, shared by every instance. It signs the server certificate devices check and the certificate each device presents. Rotating it reissues every device.</p>
              <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
                <dt className="text-fg-muted">CA fingerprint</dt>
                <dd className="font-mono text-xs break-all select-all">{data.ca_fingerprint}</dd>
                <dt className="text-fg-muted">CA expires</dt>
                <dd>{formatDateTime(data.ca_expires_at)}</dd>
              </dl>
              <div className="text-xs text-fg-subtle mt-2">Last changed {formatDateTime(data.updated_at)}</div>
            </Card>
          </div>
          <StatusCard status={data.status} />
        </div>
      )}
      {confirmRotate && (
        <ConfirmDialog
          title="Rotate the OpenVPN identity?"
          message="A new CA, server certificate and tls-crypt key replace the current ones on every instance, and every device is reissued a certificate under the new CA. Every OpenVPN profile in every project stops working until it is downloaded again."
          confirmLabel="Rotate identity"
          onCancel={() => setConfirmRotate(false)}
          onConfirm={() => rotate.mutate()}
          isLoading={rotate.isPending}
        />
      )}
    </Page>
  )
}

function StateBadge({ status }: { status: OpenVpnStatus }) {
  if (!status.enabled) return <Badge color="gray">Not running here</Badge>
  const colors: Record<OpenVpnStatus['state'], 'green' | 'yellow' | 'red' | 'gray'> = {
    running: 'green', starting: 'yellow', failed: 'red', disabled: 'gray', stopped: 'gray',
  }
  return <Badge color={colors[status.state]}>{status.state}</Badge>
}

function EndpointForm({ settings }: { settings: OpenVpnServerSettings }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const toDoc = (s: OpenVpnServerSettings): OpenVpnServerSettingsDoc => ({
    endpoint_host: s.endpoint_host, endpoint_port: s.endpoint_port, protocol: s.protocol, subnet: s.subnet,
    keepalive_interval: s.keepalive_interval, keepalive_timeout: s.keepalive_timeout, client_mtu: s.client_mtu,
  })
  const [doc, setDoc] = useState<OpenVpnServerSettingsDoc>(toDoc(settings))
  // The page polls the status; only a change to the saved values resets the form, not every tick.
  const saved = JSON.stringify(toDoc(settings))
  useEffect(() => setDoc(JSON.parse(saved) as OpenVpnServerSettingsDoc), [saved])
  const dirty = JSON.stringify(doc) !== saved

  const save = useMutation({
    mutationFn: () => updateOpenVpnSettings(doc),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['openvpn-settings'] })
      queryClient.invalidateQueries({ queryKey: ['tunnel-devices', 'openvpn'] })
      queryClient.invalidateQueries({ queryKey: ['tunnel-device-config', 'openvpn'] })
      toast.show('OpenVPN settings saved')
    },
    onError: (e: Error) => toast.show(e.message || 'Failed to save settings', 'error'),
  })

  return (
    <Card className="p-5">
      <CardHeader title="Endpoint and addressing" />
      {!settings.configured && (
        <Alert variant="warning" className="text-sm mt-2">Set the public endpoint before handing out device profiles; without it a profile has nowhere to connect.</Alert>
      )}
      <form className="mt-4 grid grid-cols-1 @lg:grid-cols-2 gap-4" onSubmit={(e) => { e.preventDefault(); save.mutate() }}>
        <div>
          <Label htmlFor="ovpn-host" className="inline-flex items-center gap-1">Public endpoint host <InfoTip>The hostname or IP devices reach this install at over the internet, as written into every device profile. Point it at the load balancer in a cluster, or at the instance itself otherwise.</InfoTip></Label>
          <Input id="ovpn-host" value={doc.endpoint_host} onChange={(e) => setDoc({ ...doc, endpoint_host: e.target.value })} placeholder="vpn.example.com" />
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_110px] gap-3">
          <div>
            <Label htmlFor="ovpn-port" className="inline-flex items-center gap-1">Port <InfoTip>The port in device profiles. Every instance running the daemon listens on it too unless its openvpn.listen_port overrides that (port-mapping NAT).</InfoTip></Label>
            <Input id="ovpn-port" type="number" min={1} max={65535} value={doc.endpoint_port} onChange={(e) => setDoc({ ...doc, endpoint_port: Number(e.target.value) })} />
          </div>
          <div>
            <Label htmlFor="ovpn-proto" className="inline-flex items-center gap-1">Transport <InfoTip>UDP is faster and the usual choice. TCP (typically on port 443) gets through networks that block UDP; the daemon listens on one of the two, so changing it restarts the daemon on every instance.</InfoTip></Label>
            <Select id="ovpn-proto" value={doc.protocol} onChange={(e) => setDoc({ ...doc, protocol: e.target.value as OpenVpnProtocol })}>
              <option value="udp">UDP</option>
              <option value="tcp">TCP</option>
            </Select>
          </div>
        </div>
        <div>
          <Label htmlFor="ovpn-subnet" className="inline-flex items-center gap-1">Tunnel subnet <InfoTip>Devices get addresses from this network; its first host is the gateway devices use as DNS. It must not overlap the WireGuard subnet. Changing it needs every device removed first and restarts the daemon on every instance.</InfoTip></Label>
          <Input id="ovpn-subnet" value={doc.subnet} onChange={(e) => setDoc({ ...doc, subnet: e.target.value })} placeholder="10.67.0.0/16" className="font-mono text-xs" />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label htmlFor="ovpn-ka-int" className="inline-flex items-center gap-1">Keepalive <InfoTip>Seconds between pings the daemon and the devices exchange, so NATs keep the mapping open.</InfoTip></Label>
            <Input id="ovpn-ka-int" type="number" min={1} max={600} value={doc.keepalive_interval} onChange={(e) => setDoc({ ...doc, keepalive_interval: Number(e.target.value) })} />
          </div>
          <div>
            <Label htmlFor="ovpn-ka-to" className="inline-flex items-center gap-1">Timeout <InfoTip>Seconds without a ping before a side considers the other gone. Must be longer than the keepalive.</InfoTip></Label>
            <Input id="ovpn-ka-to" type="number" min={2} max={3600} value={doc.keepalive_timeout} onChange={(e) => setDoc({ ...doc, keepalive_timeout: Number(e.target.value) })} />
          </div>
        </div>
        <div>
          <Label htmlFor="ovpn-mtu" className="inline-flex items-center gap-1">Device MTU <InfoTip>Written into device profiles as tun-mtu when set. Leave empty for OpenVPN's default (1500); lower it if devices behind PPPoE or another tunnel see stalls.</InfoTip></Label>
          <Input id="ovpn-mtu" type="number" min={1280} max={1500} value={doc.client_mtu ?? ''} onChange={(e) => setDoc({ ...doc, client_mtu: e.target.value === '' ? null : Number(e.target.value) })} placeholder="default" />
        </div>
        <div className="@lg:col-span-2 flex items-center justify-between gap-3 pt-1">
          <div className="text-xs text-fg-muted">Gateway and tunnel DNS: <span className="font-mono text-fg">{settings.gateway}</span></div>
          <Button type="submit" size="sm" disabled={!dirty || save.isPending}>Save</Button>
        </div>
      </form>
    </Card>
  )
}

function StatusCard({ status }: { status: OpenVpnStatus }) {
  return (
    <Card className="p-5 h-fit">
      <CardHeader title="This instance" action={<StateBadge status={status} />} />
      {!status.enabled ? (
        <p className="text-sm text-fg-muted mt-2">
          This instance is not running the OpenVPN daemon. Enable it with <span className="font-mono text-fg">OCTOPROX_OPENVPN_ENABLED=true</span> (or <span className="font-mono text-fg">openvpn.enabled</span> in the config file) on every instance the public endpoint reaches; the bundled cluster compose does so on all replicas. Each needs CAP_NET_ADMIN, /dev/net/tun and the port published.
        </p>
      ) : status.state === 'failed' ? (
        <Alert variant="error" className="text-sm mt-2 break-words whitespace-pre-wrap">{status.error}</Alert>
      ) : (
        <p className="text-sm text-fg-muted mt-2">Tunnel traffic from devices is redirected into this instance's proxy path.</p>
      )}
      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <Row label="Instance" value={status.instance_id} mono />
        <Row label="Interface" value={status.interface} mono />
        <Row label="Daemon" value={status.daemon_version ?? '-'} />
        <Row label="Transport" value={`${status.protocol.toUpperCase()}${status.listen_port ? ` ${status.listen_port}` : ''}`} />
        <Row label="Restarts" value={status.restarts} title="Times the daemon exited on its own and was started again since this process started." />
        <Row label="Refused" value={status.denied} title="Connections refused at the daemon since this process started: unknown, disabled or rotated devices." />
        <Row label="Fake IP range" value={status.fake_ip_range} mono />
        <Row label="Devices" value={`${status.peers_enabled} enabled of ${status.peers_total}`} />
        <Row label="Online now" value={status.peers_online} />
        <Row label="Open tunnels" value={status.active_connections} title="Connections the shared tunnel listener is relaying right now, every tunnel protocol included." />
        <Row label="Routed by address" value={status.connections_by_address} title="Connections of every OpenVPN device, all time, relayed by address because no destination name could be recovered." />
        <Row label="Encrypted DNS" value={status.block_encrypted_dns ? `blocked (${status.encrypted_dns_blocked})` : 'allowed'} title="Encrypted-DNS connections closed so devices keep the tunnel resolver: every OpenVPN device, all time." />
      </dl>
      <div className="mt-4 flex items-start gap-2 text-xs text-fg-muted">
        <ShieldCheck className="w-4 h-4 flex-none mt-0.5" />
        <span>Devices are added per project under Devices, picking OpenVPN as the tunnel. Each gets a .ovpn profile.</span>
      </div>
    </Card>
  )
}

function Row({ label, value, mono, title }: { label: string; value: string | number; mono?: boolean; title?: string }) {
  return (
    <>
      <dt className="text-fg-muted" title={title}>{label}</dt>
      <dd className={mono ? 'font-mono text-xs truncate text-right' : 'text-right tabular-nums'} title={title ?? String(value)}>{value}</dd>
    </>
  )
}
