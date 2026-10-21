// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Router } from 'lucide-react'
import {
  fetchWireGuardSettings, rotateWireGuardServerKey, updateWireGuardSettings,
  TunnelStatus, WireGuardServerSettings, WireGuardServerSettingsDoc,
} from '../../api/client'
import { useToast } from '../../contexts/ToastContext'
import { Page } from '../../components/layout/Page'
import { TunnelGatewaysCard, TunnelStateBadge, relayedLabel } from '../../components/TunnelGateways'
import { formatDateTime } from '../../utils/format'
import { Alert, Button, Card, CardHeader, ConfirmDialog, InfoTip, Input, Label } from '../../components/ui'

/**
 * Admin page for the WireGuard endpoint: the install's identity and what every
 * device config says about reaching it, plus the tunnel on every gateway
 * terminating it, from the heartbeats the gateways publish.
 */
export default function WireGuardSection() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const { data, isLoading } = useQuery({ queryKey: ['wireguard-settings'], queryFn: fetchWireGuardSettings, refetchInterval: 15_000 })
  const [confirmRotate, setConfirmRotate] = useState(false)

  const rotate = useMutation({
    mutationFn: rotateWireGuardServerKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['wireguard-settings'] })
      queryClient.invalidateQueries({ queryKey: ['tunnel-device-config', 'wireguard'] })
      setConfirmRotate(false)
      toast.show('Server key rotated; every device needs its config again')
    },
    onError: (e: Error) => { setConfirmRotate(false); toast.show(e.message || 'Failed to rotate key', 'error') },
  })

  return (
    <Page
      title="WireGuard"
      subtitle="Devices without proxy support join a project's pool through a WireGuard tunnel terminated by Octoprox."
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
                action={<Button size="sm" variant="outline" onClick={() => setConfirmRotate(true)}><KeyRound className="w-3.5 h-3.5" /> Rotate key</Button>}
              />
              <p className="text-sm text-fg-muted mt-1">One key pair for the whole install, shared by every gateway. Devices pin this public key in their config.</p>
              <div className="mt-3 font-mono text-xs bg-surface-raised rounded-lg p-3 break-all select-all">{data.public_key}</div>
              <div className="text-xs text-fg-subtle mt-2">Last changed {formatDateTime(data.updated_at)}</div>
            </Card>
          </div>
          <StatusCard status={data.status} />
        </div>
      )}
      {confirmRotate && (
        <ConfirmDialog
          title="Rotate the server key?"
          message="A new key pair replaces the current one on every gateway within a few seconds. Every device config in every project stops working until it is downloaded or scanned again."
          confirmLabel="Rotate key"
          onCancel={() => setConfirmRotate(false)}
          onConfirm={() => rotate.mutate()}
          isLoading={rotate.isPending}
        />
      )}
    </Page>
  )
}

function EndpointForm({ settings }: { settings: WireGuardServerSettings }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const toDoc = (s: WireGuardServerSettings): WireGuardServerSettingsDoc => ({
    endpoint_host: s.endpoint_host, endpoint_port: s.endpoint_port, subnet: s.subnet,
    persistent_keepalive: s.persistent_keepalive, client_mtu: s.client_mtu,
  })
  const [doc, setDoc] = useState<WireGuardServerSettingsDoc>(toDoc(settings))
  // The page polls the status; only a change to the saved values resets the form, not every tick.
  const saved = JSON.stringify(toDoc(settings))
  useEffect(() => setDoc(JSON.parse(saved) as WireGuardServerSettingsDoc), [saved])
  const dirty = JSON.stringify(doc) !== saved

  const save = useMutation({
    mutationFn: () => updateWireGuardSettings(doc),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['wireguard-settings'] })
      queryClient.invalidateQueries({ queryKey: ['tunnel-device-config', 'wireguard'] })
      queryClient.invalidateQueries({ queryKey: ['tunnel-devices', 'wireguard'] })
      toast.show('WireGuard settings saved')
    },
    onError: (e: Error) => toast.show(e.message || 'Failed to save settings', 'error'),
  })

  return (
    <Card className="p-5">
      <CardHeader title="Endpoint and addressing" />
      {!settings.configured && (
        <Alert variant="warning" className="text-sm mt-2">Set the public endpoint before handing out device configs; without it a config has nowhere to connect.</Alert>
      )}
      <form className="mt-4 grid grid-cols-1 @lg:grid-cols-2 gap-4" onSubmit={(e) => { e.preventDefault(); save.mutate() }}>
        <div>
          <Label htmlFor="wg-host" className="inline-flex items-center gap-1">Public endpoint host <InfoTip>The hostname or IP devices reach this install at over the internet, as written into every device config. Point it at the load balancer in front of the gateways, or at the gateway's own address when there is only one.</InfoTip></Label>
          <Input id="wg-host" value={doc.endpoint_host} onChange={(e) => setDoc({ ...doc, endpoint_host: e.target.value })} placeholder="vpn.example.com" />
        </div>
        <div>
          <Label htmlFor="wg-port" className="inline-flex items-center gap-1">UDP port <InfoTip>The port in device configs. Every gateway terminating the tunnel listens on it too unless its wireguard.listen_port overrides that (port-mapping NAT).</InfoTip></Label>
          <Input id="wg-port" type="number" min={1} max={65535} value={doc.endpoint_port} onChange={(e) => setDoc({ ...doc, endpoint_port: Number(e.target.value) })} />
        </div>
        <div>
          <Label htmlFor="wg-subnet" className="inline-flex items-center gap-1">Tunnel subnet <InfoTip>Devices get addresses from this network; its first host is the gateway devices use as DNS. It must not overlap the OpenVPN subnet. A new subnet must still hold every device's address, so devices outside it are removed first; the gateways move to it without a restart.</InfoTip></Label>
          <Input id="wg-subnet" value={doc.subnet} onChange={(e) => setDoc({ ...doc, subnet: e.target.value })} placeholder="10.66.0.0/16" className="font-mono text-xs" />
        </div>
        <div>
          <Label htmlFor="wg-keepalive" className="inline-flex items-center gap-1">Persistent keepalive <InfoTip>Seconds between keepalives a device sends, so a NAT in front of it keeps the mapping open. 25 is the usual value; 0 disables it.</InfoTip></Label>
          <Input id="wg-keepalive" type="number" min={0} max={3600} value={doc.persistent_keepalive} onChange={(e) => setDoc({ ...doc, persistent_keepalive: Number(e.target.value) })} />
        </div>
        <div>
          <Label htmlFor="wg-mtu" className="inline-flex items-center gap-1">Device MTU <InfoTip>Written into device configs when set. Leave empty for WireGuard's default (1420); lower it if devices behind PPPoE or another tunnel see stalls.</InfoTip></Label>
          <Input id="wg-mtu" type="number" min={1280} max={1500} value={doc.client_mtu ?? ''} onChange={(e) => setDoc({ ...doc, client_mtu: e.target.value === '' ? null : Number(e.target.value) })} placeholder="default" />
        </div>
        <div className="@lg:col-span-2 flex items-center justify-between gap-3 pt-1">
          <div className="text-xs text-fg-muted">Gateway and tunnel DNS: <span className="font-mono text-fg">{settings.gateway}</span></div>
          <Button type="submit" size="sm" disabled={!dirty || save.isPending}>Save</Button>
        </div>
      </form>
    </Card>
  )
}

function StatusCard({ status }: { status: TunnelStatus }) {
  return (
    <TunnelGatewaysCard
      status={status}
      notRunning={<>
        No gateway is terminating the tunnel. Enable it with <span className="font-mono text-fg">OCTOPROX_WIREGUARD_ENABLED=true</span> (or <span className="font-mono text-fg">wireguard.enabled</span> in the config file) on the gateways the public endpoint reaches. Each needs CAP_NET_ADMIN and the UDP port published.
      </>}
      details={(gateway) => [
        gateway.interface,
        gateway.backend ? (gateway.backend === 'kernel' ? 'kernel module' : 'wireguard-go') : null,
        gateway.listen_port != null ? `UDP ${gateway.listen_port}` : null,
        relayedLabel(gateway),
        gateway.fake_ip_range ? `fake IPs ${gateway.fake_ip_range}` : null,
        gateway.block_encrypted_dns ? null : 'encrypted DNS allowed',
      ]}
    >
      <div className="mt-4 flex items-start gap-2 text-xs text-fg-muted">
        <Router className="w-4 h-4 flex-none mt-0.5" />
        <span>Devices are added per project under Devices, picking WireGuard as the tunnel. Each gets a config and a QR code.</span>
      </div>
    </TunnelGatewaysCard>
  )
}
