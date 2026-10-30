// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, ShieldCheck } from 'lucide-react'
import {
  fetchOpenVpnSettings, rotateOpenVpnIdentity, updateOpenVpnSettings,
  OpenVpnProtocol, OpenVpnServerSettings, OpenVpnServerSettingsDoc, TunnelStatus,
} from '../../api/client'
import { useToast } from '../../contexts/ToastContext'
import { Page } from '../../components/layout/Page'
import { TunnelGatewaysCard, TunnelStateBadge, relayedLabel } from '../../components/TunnelGateways'
import { formatDateTime, plural } from '../../utils/format'
import { Alert, Button, Card, CardHeader, ConfirmDialog, InfoTip, Input, Label, Select } from '../../components/ui'

/**
 * Admin page for the OpenVPN endpoint: the install's CA and what every device
 * profile says about reaching it, plus the daemon on every gateway running
 * it, from the heartbeats the gateways publish.
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
      subtitle="Devices without proxy support join a project's pool through an OpenVPN tunnel terminated by Octoprox. This server is shared by every project; each project adds its devices on its Devices page."
      toolbar={data && <TunnelStateBadge status={data.status} />}
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
              <p className="text-sm text-fg-muted mt-1">A private certificate authority for the whole install, shared by every gateway. It signs the server certificate devices check and the certificate each device presents. Rotating it reissues every device.</p>
              <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
                <dt className="text-fg-muted">CA fingerprint</dt>
                <dd className="font-mono text-xs break-all select-all">{data.ca_fingerprint}</dd>
                <dt className="text-fg-muted">CA expires</dt>
                <dd>{formatDateTime(data.ca_expires_at)}</dd>
              </dl>
              <div className="text-xs text-fg-subtle mt-2">Last changed {formatDateTime(data.updated_at)}</div>
            </Card>
          </div>
          <StatusCard status={data.status} transport={data.protocol} />
        </div>
      )}
      {confirmRotate && (
        <ConfirmDialog
          title="Rotate the OpenVPN identity?"
          message="A new CA, server certificate and tls-crypt key replace the current ones, every gateway restarts its daemon with them within a few seconds, and every device is reissued a certificate under the new CA. Every OpenVPN profile in every project stops working until it is downloaded again."
          confirmLabel="Rotate identity"
          onCancel={() => setConfirmRotate(false)}
          onConfirm={() => rotate.mutate()}
          isLoading={rotate.isPending}
        />
      )}
    </Page>
  )
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
          <Label htmlFor="ovpn-host" className="inline-flex items-center gap-1">Public endpoint host <InfoTip>The hostname or IP devices reach this install at over the internet, as written into every device profile. Point it at the load balancer in front of the gateways, or at the gateway's own address when there is only one.</InfoTip></Label>
          <Input id="ovpn-host" value={doc.endpoint_host} onChange={(e) => setDoc({ ...doc, endpoint_host: e.target.value })} placeholder="vpn.example.com" />
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_110px] gap-3">
          <div>
            <Label htmlFor="ovpn-port" className="inline-flex items-center gap-1">Port <InfoTip>The port in device profiles. Every gateway running the daemon listens on it too unless its openvpn.listen_port overrides that (port-mapping NAT).</InfoTip></Label>
            <Input id="ovpn-port" type="number" min={1} max={65535} value={doc.endpoint_port} onChange={(e) => setDoc({ ...doc, endpoint_port: Number(e.target.value) })} />
          </div>
          <div>
            <Label htmlFor="ovpn-proto" className="inline-flex items-center gap-1">Transport <InfoTip>UDP is faster and the usual choice. TCP (typically on port 443) gets through networks that block UDP; the daemon listens on one of the two, so changing it restarts the daemon on every gateway.</InfoTip></Label>
            <Select id="ovpn-proto" value={doc.protocol} onChange={(e) => setDoc({ ...doc, protocol: e.target.value as OpenVpnProtocol })}>
              <option value="udp">UDP</option>
              <option value="tcp">TCP</option>
            </Select>
          </div>
        </div>
        <div>
          <Label htmlFor="ovpn-subnet" className="inline-flex items-center gap-1">Tunnel subnet <InfoTip>Devices get addresses from this network; its first host is the gateway devices use as DNS. It must not overlap the WireGuard subnet. A new subnet must still hold every device's address, so devices outside it are removed first; changing it restarts the daemon on every gateway.</InfoTip></Label>
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

function StatusCard({ status, transport }: { status: TunnelStatus; transport: OpenVpnProtocol }) {
  return (
    <TunnelGatewaysCard
      status={status}
      notRunning={<>
        No gateway is running the OpenVPN daemon. Enable it with <span className="font-mono text-fg">OCTOPROX_OPENVPN_ENABLED=true</span> (or <span className="font-mono text-fg">openvpn.enabled</span> in the config file) on the gateways the public endpoint reaches. Each needs CAP_NET_ADMIN, /dev/net/tun and the port published.
      </>}
      details={(gateway) => [
        gateway.interface,
        gateway.daemon_version,
        `${transport.toUpperCase()}${gateway.listen_port != null ? ` ${gateway.listen_port}` : ''}`,
        relayedLabel(gateway),
        gateway.restarts > 0 ? `${gateway.restarts} ${plural(gateway.restarts, 'restart', 'restarts')}` : null,
        gateway.denied > 0 ? `${gateway.denied} refused` : null,
        gateway.fake_ip_range ? `fake IPs ${gateway.fake_ip_range}` : null,
        gateway.block_encrypted_dns ? null : 'encrypted DNS allowed',
      ]}
    >
      {status.gateways.some((g) => g.restarts > 0 || g.denied > 0) && (
        <p className="text-[11px] text-fg-subtle mt-3">
          Restarts count the times a gateway's daemon exited on its own and was started again, and refused the connections
          it turned away at admission (unknown, disabled or rotated devices), each since that gateway started.
        </p>
      )}
      <div className="mt-3 flex items-start gap-2 text-xs text-fg-muted">
        <ShieldCheck className="w-4 h-4 flex-none mt-0.5" />
        <span>Devices are added per project under Devices, picking OpenVPN as the tunnel. Each gets a .ovpn profile.</span>
      </div>
    </TunnelGatewaysCard>
  )
}
