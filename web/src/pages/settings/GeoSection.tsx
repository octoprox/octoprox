// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Database, Globe, RefreshCw, Search, Trash2, Upload } from 'lucide-react'
import {
  addGeoDatabaseFromUrl, deleteGeoDatabase, fetchGeoDatabases, fetchGeoSettings, fetchGeoStatus, lookupGeoIp,
  reattributeProxies, refreshGeoDatabase, updateGeoDatabase, updateGeoSettings, uploadGeoDatabase,
  GeoDatabase, GeoLookupResponse, GeoSettingsDoc, GeoSourceKind,
} from '../../api/client'
import { useToast } from '../../contexts/ToastContext'
import { DataTable } from '../../components/DataTable'
import { Page, EmptyState } from '../../components/layout/Page'
import { ProviderAccuracyPanel } from '../../components/geo/ProviderAccuracyPanel'
import { ObservationsPanel } from '../../components/geo/ObservationsPanel'
import { ExitIpsPanel } from '../../components/geo/ExitIpsPanel'
import { formatBytes, formatDateTime } from '../../utils/format'
import { Alert, Badge, Button, Card, ConfirmDialog, Input, Label, Select, Tabs } from '../../components/ui'
import { cn } from '../../utils/cn'

type Tab = 'databases' | 'policy' | 'accuracy' | 'exits' | 'observations'

const SOURCE_LABELS: Record<GeoSourceKind, string> = {
  database: 'Local IP databases',
  vendor: 'Vendor claim',
  endpoint: 'Echo endpoint (only when it reports a country)',
}

const VENDOR_LABELS: Record<string, string> = {
  maxmind: 'MaxMind',
  dbip: 'DB-IP',
  ipinfo: 'IPinfo',
  ip2location: 'IP2Location',
  other: 'Other',
}

/**
 * Admin page for IP attribution: the local IP databases proxies are attributed
 * with, the source policy that combines databases, vendor claims and the echo
 * endpoint, and what the observations say about each provider's honesty.
 */
export default function GeoSection() {
  const [tab, setTab] = useState<Tab>('databases')
  const { data: status } = useQuery({ queryKey: ['geo-status'], queryFn: fetchGeoStatus, refetchInterval: 30_000 })

  return (
    <Page
      title="IP attribution"
      subtitle="Where exit IPs really are: local IP databases, the vendor's word, and an echo endpoint, combined under one policy."
      toolbar={status && (
        <div className="flex items-center gap-2 text-xs text-fg-muted">
          <Badge color={status.databases_loaded > 0 ? 'green' : 'gray'}>{status.databases_loaded} database{status.databases_loaded === 1 ? '' : 's'} loaded</Badge>
          {Object.keys(status.load_errors).length > 0 && <Badge color="red">{Object.keys(status.load_errors).length} failed to load</Badge>}
          <span className="tabular-nums">{status.stored_observations.toLocaleString()} observations</span>
        </div>
      )}
    >
      <Tabs<Tab>
        tabs={[
          { id: 'databases', label: 'Databases' },
          { id: 'policy', label: 'Policy & echo' },
          { id: 'accuracy', label: 'Provider accuracy' },
          { id: 'exits', label: 'Exit IPs' },
          { id: 'observations', label: 'Observation log' },
        ]}
        active={tab}
        onChange={setTab}
      />
      {tab === 'databases' && <DatabasesTab />}
      {tab === 'policy' && <PolicyTab />}
      {tab === 'accuracy' && <ProviderAccuracyPanel />}
      {tab === 'exits' && <ExitIpsPanel />}
      {tab === 'observations' && <ObservationsPanel />}
    </Page>
  )
}

// --- databases -------------------------------------------------------------------------

function DatabasesTab() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const { data: databases, isLoading } = useQuery({ queryKey: ['geo-databases'], queryFn: fetchGeoDatabases, refetchInterval: 30_000 })
  const [pendingDelete, setPendingDelete] = useState<GeoDatabase | null>(null)
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['geo-databases'] })
    queryClient.invalidateQueries({ queryKey: ['geo-status'] })
    queryClient.invalidateQueries({ queryKey: ['geo-settings'] })
  }

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteGeoDatabase(id),
    onSuccess: () => { invalidate(); setPendingDelete(null); toast.show('Database removed. Proxies are being re-attributed.') },
    onError: (e: Error) => { setPendingDelete(null); toast.show(e.message || 'Failed to remove database', 'error') },
  })
  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => updateGeoDatabase(id, { enabled }),
    onSuccess: invalidate,
    onError: (e: Error) => toast.show(e.message || 'Failed to update database', 'error'),
  })
  const priorityMutation = useMutation({
    mutationFn: ({ id, priority }: { id: string; priority: number }) => updateGeoDatabase(id, { priority }),
    onSuccess: invalidate,
    onError: (e: Error) => toast.show(e.message || 'Failed to update database', 'error'),
  })
  const refreshMutation = useMutation({
    mutationFn: (id: string) => refreshGeoDatabase(id),
    onSuccess: () => { invalidate(); toast.show('Database refreshed') },
    onError: (e: Error) => { invalidate(); toast.show(e.message || 'Refresh failed', 'error') },
  })
  const reattributeMutation = useMutation({
    mutationFn: () => reattributeProxies(),
    onSuccess: (r) => toast.show(`Re-attributed ${r.scanned} prox${r.scanned === 1 ? 'y' : 'ies'}, ${r.updated} changed`),
    onError: (e: Error) => toast.show(e.message || 'Re-attribution failed', 'error'),
  })

  const columns: ColumnDef<GeoDatabase>[] = useMemo(() => [
    {
      accessorKey: 'name',
      header: 'Database',
      cell: ({ row }) => (
        <div className="min-w-0">
          <div className="font-medium truncate flex items-center gap-2">
            {row.original.name}
            {!row.original.enabled && <Badge color="gray" className="py-0 text-[10px]">disabled</Badge>}
          </div>
          <div className="text-xs text-fg-muted truncate">{row.original.database_type || row.original.format}{row.original.description ? ` · ${row.original.description}` : ''}</div>
        </div>
      ),
    },
    { accessorKey: 'vendor', header: 'Vendor', size: 110, cell: ({ getValue }) => VENDOR_LABELS[getValue<string>()] ?? getValue<string>() },
    { accessorKey: 'kind', header: 'Answers', size: 100, cell: ({ getValue }) => <span className="capitalize">{getValue<string>()}</span> },
    {
      accessorKey: 'source', header: 'Source', size: 110,
      cell: ({ row }) => row.original.source === 'path' ? <span title={row.original.path ?? ''}>Config file</span> : row.original.source === 'url' ? 'Scheduled download' : 'Uploaded',
    },
    {
      accessorKey: 'priority', header: 'Priority', size: 90, meta: { align: 'right' as const },
      cell: ({ row }) => row.original.source === 'path' ? <span className="tabular-nums">{row.original.priority}</span> : (
        <Input type="number" min={0} className="w-20 h-7 px-2 py-0 text-xs text-right" defaultValue={row.original.priority}
          onBlur={(e) => { const v = parseInt(e.target.value); if (!Number.isNaN(v) && v !== row.original.priority) priorityMutation.mutate({ id: row.original.id, priority: v }) }} />
      ),
    },
    { accessorKey: 'build_epoch', header: 'Built', size: 120, cell: ({ getValue }) => getValue<string | null>() ? formatDateTime(getValue<string>()).split(',')[0] : '-' },
    { accessorKey: 'size_bytes', header: 'Size', size: 90, meta: { align: 'right' as const }, cell: ({ getValue }) => formatBytes(getValue<number>()) },
    {
      id: 'state', header: 'Status', size: 150,
      cell: ({ row }) => row.original.load_error
        ? <Badge color="red" title={row.original.load_error}>Not loaded</Badge>
        : row.original.last_update_error
          ? <Badge color="yellow" title={row.original.last_update_error}>Update failed</Badge>
          : row.original.loaded_here ? <Badge color="green">Loaded</Badge> : <Badge color="gray">{row.original.enabled ? 'Not loaded here' : 'Off'}</Badge>,
    },
    {
      id: 'actions', header: '', size: 150,
      cell: ({ row }) => row.original.source === 'path' ? null : (
        <div className="flex items-center justify-end gap-1">
          {row.original.update_url && (
            <Button variant="ghost" size="sm" title={`Download now (every ${row.original.update_interval_hours}h)`} onClick={() => refreshMutation.mutate(row.original.id)} disabled={refreshMutation.isPending}>
              <RefreshCw className={cn('w-3.5 h-3.5', refreshMutation.isPending && 'animate-spin')} />
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={() => toggleMutation.mutate({ id: row.original.id, enabled: !row.original.enabled })}>
            {row.original.enabled ? 'Disable' : 'Enable'}
          </Button>
          <Button variant="ghost" size="sm" className="text-danger" title="Remove" onClick={() => setPendingDelete(row.original)}>
            <Trash2 className="w-3.5 h-3.5" />
          </Button>
        </div>
      ),
    },
  ], [priorityMutation, refreshMutation, toggleMutation])

  const attributions = useMemo(() => Array.from(new Set((databases ?? []).filter((d) => d.enabled && d.attribution).map((d) => d.attribution))), [databases])

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 @3xl:grid-cols-2 gap-4">
        <UploadCard onDone={invalidate} />
        <FromUrlCard onDone={invalidate} />
      </div>

      <Card className="p-0 overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="flex items-center gap-2">
            <Database className="w-4 h-4 text-fg-muted" />
            <h3 className="text-sm font-semibold text-fg">Installed databases</h3>
            <span className="text-xs text-fg-subtle">Consulted lowest priority first. The first database with a country answers.</span>
          </div>
          <Button variant="outline" size="sm" onClick={() => reattributeMutation.mutate()} disabled={reattributeMutation.isPending} title="Re-run attribution for every proxy with a known exit IP, offline">
            <RefreshCw className={cn('w-3.5 h-3.5', reattributeMutation.isPending && 'animate-spin')} /> Re-attribute proxies
          </Button>
        </div>
        {!isLoading && (databases ?? []).length === 0 ? (
          <EmptyState icon={<Database className="w-5 h-5" />} title="No IP databases" description="Upload a MaxMind, DB-IP, IPinfo or IP2Location file, or register a vendor download URL. Until then attribution uses vendor claims and the echo endpoint only." />
        ) : (
          <DataTable columns={columns} data={databases ?? []} getRowId={(d) => d.id} enableColumnFilters={false} defaultPageSize={20} />
        )}
      </Card>

      {attributions.length > 0 && (
        <div className="text-[11px] text-fg-subtle space-y-0.5">
          {attributions.map((text) => <p key={text}>{text}</p>)}
        </div>
      )}

      {pendingDelete && (
        <ConfirmDialog
          title="Remove database?"
          message={<>Remove <b className="text-fg">{pendingDelete.name}</b> from every instance. Proxies will be re-attributed with the remaining sources.</>}
          confirmLabel="Remove"
          danger
          isLoading={deleteMutation.isPending}
          onConfirm={() => deleteMutation.mutate(pendingDelete.id)}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  )
}

function UploadCard({ onDone }: { onDone: () => void }) {
  const toast = useToast()
  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const [priority, setPriority] = useState(100)
  const [error, setError] = useState<string | null>(null)
  const mutation = useMutation({
    mutationFn: () => uploadGeoDatabase(file!, { name: name || undefined, priority }),
    onSuccess: (db) => {
      setError(null); setFile(null); setName(''); if (fileRef.current) fileRef.current.value = ''
      onDone(); toast.show(`${db.name} loaded (${VENDOR_LABELS[db.vendor] ?? db.vendor}, ${db.kind}). Proxies are being re-attributed.`)
    },
    onError: (e: Error) => setError(e.message || 'Upload failed'),
  })
  return (
    <Card className="p-5 flex flex-col">
      <div className="flex items-center gap-2 mb-1">
        <Upload className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold text-fg">Upload a database file</h3>
      </div>
      <p className="text-xs text-fg-muted mb-4">
        Any mmdb file (MaxMind GeoIP2 and GeoLite2, DB-IP, IPinfo, IP2Location DB1 and DB9) or an IP2Location BIN file. The file is validated, stored, and distributed to every instance.
      </p>
      <form onSubmit={(e) => { e.preventDefault(); if (file) mutation.mutate() }} className="flex-1 flex flex-col gap-3">
        {error && <Alert variant="error">{error}</Alert>}
        <div>
          <Label htmlFor="geo-upload-file">File</Label>
          <input id="geo-upload-file" ref={fileRef} type="file" accept=".mmdb,.BIN,.bin" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="block w-full text-sm text-fg-muted file:mr-3 file:rounded-md file:border-0 file:bg-surface-raised file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-fg" />
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_100px] gap-3">
          <div>
            <Label htmlFor="geo-upload-name">Name</Label>
            <Input id="geo-upload-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={file ? file.name.replace(/\.[^.]+$/, '') : 'Defaults to the file name'} />
          </div>
          <div>
            <Label htmlFor="geo-upload-priority">Priority</Label>
            <Input id="geo-upload-priority" type="number" min={0} value={priority} onChange={(e) => setPriority(parseInt(e.target.value) || 0)} />
          </div>
        </div>
        <div className="flex justify-end mt-auto pt-1">
          <Button type="submit" size="sm" disabled={!file || mutation.isPending}>{mutation.isPending ? 'Uploading…' : 'Upload'}</Button>
        </div>
      </form>
    </Card>
  )
}

const URL_PRESETS: { label: string; url: string; hint: string; auth: 'basic' | 'token' | 'none'; interval: number }[] = [
  { label: 'MaxMind GeoLite2 City', url: 'https://download.maxmind.com/geoip/databases/GeoLite2-City/download?suffix=tar.gz', hint: 'Account ID as username, license key as password', auth: 'basic', interval: 24 },
  { label: 'MaxMind GeoLite2 Country', url: 'https://download.maxmind.com/geoip/databases/GeoLite2-Country/download?suffix=tar.gz', hint: 'Account ID as username, license key as password', auth: 'basic', interval: 24 },
  { label: 'MaxMind GeoLite2 ASN', url: 'https://download.maxmind.com/geoip/databases/GeoLite2-ASN/download?suffix=tar.gz', hint: 'Account ID as username, license key as password', auth: 'basic', interval: 24 },
  { label: 'DB-IP Country Lite', url: 'https://download.db-ip.com/free/dbip-country-lite-YYYY-MM.mmdb.gz', hint: 'Replace YYYY-MM with the current month; no credentials', auth: 'none', interval: 720 },
  { label: 'IPinfo country + ASN', url: 'https://ipinfo.io/data/free/country_asn.mmdb?token=', hint: 'Append your IPinfo token to the URL', auth: 'none', interval: 24 },
  { label: 'Custom URL', url: '', hint: 'Any HTTPS URL serving an mmdb, .mmdb.gz or .tar.gz containing one', auth: 'none', interval: 24 },
]

function FromUrlCard({ onDone }: { onDone: () => void }) {
  const toast = useToast()
  const [preset, setPreset] = useState(0)
  const [name, setName] = useState(URL_PRESETS[0].label)
  const [url, setUrl] = useState(URL_PRESETS[0].url)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [interval, setInterval] = useState(24)
  const [priority, setPriority] = useState(100)
  const [error, setError] = useState<string | null>(null)
  const current = URL_PRESETS[preset]

  const choosePreset = (index: number) => {
    const p = URL_PRESETS[index]
    setPreset(index); setName(p.label === 'Custom URL' ? '' : p.label); setUrl(p.url); setInterval(p.interval)
  }
  const mutation = useMutation({
    mutationFn: () => addGeoDatabaseFromUrl({
      name: name || current.label, update_url: url, update_interval_hours: interval, priority, enabled: true,
      update_auth: username || password ? { username, password } : {},
    }),
    onSuccess: (db) => { setError(null); onDone(); toast.show(`${db.name} downloaded and loaded`) },
    onError: (e: Error) => { onDone(); setError(e.message || 'Download failed') },
  })
  return (
    <Card className="p-5 flex flex-col">
      <div className="flex items-center gap-2 mb-1">
        <Globe className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold text-fg">Download from the vendor</h3>
      </div>
      <p className="text-xs text-fg-muted mb-4">
        Register a download URL with your own vendor credentials. The leader instance fetches it now and again on the schedule, and every instance picks up the new file.
      </p>
      <form onSubmit={(e) => { e.preventDefault(); mutation.mutate() }} className="flex-1 flex flex-col gap-3">
        {error && <Alert variant="error">{error}</Alert>}
        <div>
          <Label htmlFor="geo-url-preset">Vendor</Label>
          <Select id="geo-url-preset" value={preset} onChange={(e) => choosePreset(parseInt(e.target.value))}>
            {URL_PRESETS.map((p, i) => <option key={p.label} value={i}>{p.label}</option>)}
          </Select>
        </div>
        <div>
          <Label htmlFor="geo-url">Download URL</Label>
          <Input id="geo-url" className="font-mono text-xs" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" required />
          <p className="text-[11px] text-fg-subtle mt-1">{current.hint}</p>
        </div>
        {current.auth === 'basic' && (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="geo-url-user">Account ID</Label>
              <Input id="geo-url-user" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
            </div>
            <div>
              <Label htmlFor="geo-url-pass">License key</Label>
              <Input id="geo-url-pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
            </div>
          </div>
        )}
        <div className="grid grid-cols-[minmax(0,1fr)_110px_100px] gap-3">
          <div>
            <Label htmlFor="geo-url-name">Name</Label>
            <Input id="geo-url-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={current.label} />
          </div>
          <div>
            <Label htmlFor="geo-url-interval">Every (hours)</Label>
            <Input id="geo-url-interval" type="number" min={0} value={interval} onChange={(e) => setInterval(parseInt(e.target.value) || 0)} />
          </div>
          <div>
            <Label htmlFor="geo-url-priority">Priority</Label>
            <Input id="geo-url-priority" type="number" min={0} value={priority} onChange={(e) => setPriority(parseInt(e.target.value) || 0)} />
          </div>
        </div>
        <div className="flex justify-end mt-auto pt-1">
          <Button type="submit" size="sm" disabled={!url || mutation.isPending}>{mutation.isPending ? 'Downloading…' : 'Download and add'}</Button>
        </div>
      </form>
    </Card>
  )
}

// --- policy ----------------------------------------------------------------------------

function PolicyTab() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const { data, isLoading } = useQuery({ queryKey: ['geo-settings'], queryFn: fetchGeoSettings })
  const [draft, setDraft] = useState<GeoSettingsDoc | null>(null)
  const policy = draft ?? data?.settings ?? null
  const mutation = useMutation({
    mutationFn: (doc: GeoSettingsDoc) => updateGeoSettings(doc),
    onSuccess: () => { setDraft(null); queryClient.invalidateQueries({ queryKey: ['geo-settings'] }); toast.show('Settings saved on every instance') },
    onError: (e: Error) => toast.show(e.message || 'Failed to save settings', 'error'),
  })
  if (isLoading || !policy) return <p className="text-sm text-fg-muted">Loading…</p>

  const update = (patch: Partial<GeoSettingsDoc>) => setDraft({ ...policy, ...patch })
  const move = (kind: GeoSourceKind, direction: -1 | 1) => {
    const sources = [...policy.default_sources]
    const index = sources.indexOf(kind)
    const target = index + direction
    if (index < 0 || target < 0 || target >= sources.length) return
    sources.splice(index, 1); sources.splice(target, 0, kind)
    update({ default_sources: sources })
  }
  const toggle = (kind: GeoSourceKind) => {
    const sources = policy.default_sources.includes(kind) ? policy.default_sources.filter((s) => s !== kind) : [...policy.default_sources, kind]
    if (sources.length > 0) update({ default_sources: sources })
  }
  const allKinds: GeoSourceKind[] = ['database', 'vendor', 'endpoint']
  const excluded = allKinds.filter((k) => !policy.default_sources.includes(k))

  return (
    <form onSubmit={(e) => { e.preventDefault(); mutation.mutate(policy) }} className="space-y-4 max-w-4xl">
      <div className="grid grid-cols-1 @3xl:grid-cols-2 gap-4">
        <Card className="p-5">
          <h3 className="text-sm font-semibold text-fg mb-1">Default source precedence</h3>
          <p className="text-xs text-fg-muted mb-3">The first source with an answer decides a proxy's country. Sources left out never decide, but still count as evidence when judging the vendor. Projects can override this on their Location tab.</p>
          <ol className="space-y-1.5">
            {policy.default_sources.map((kind, i) => (
              <li key={kind} className="flex items-center gap-2 rounded-lg border border-line px-3 py-2 text-sm">
                <span className="w-5 text-xs text-fg-subtle tabular-nums">{i + 1}.</span>
                <span className="flex-1">{SOURCE_LABELS[kind]}</span>
                <Button type="button" variant="ghost" size="sm" onClick={() => move(kind, -1)} disabled={i === 0}>↑</Button>
                <Button type="button" variant="ghost" size="sm" onClick={() => move(kind, 1)} disabled={i === policy.default_sources.length - 1}>↓</Button>
                <Button type="button" variant="ghost" size="sm" onClick={() => toggle(kind)} disabled={policy.default_sources.length === 1}>Exclude</Button>
              </li>
            ))}
            {excluded.map((kind) => (
              <li key={kind} className="flex items-center gap-2 rounded-lg border border-dashed border-line px-3 py-2 text-sm text-fg-muted">
                <span className="w-5" />
                <span className="flex-1">{SOURCE_LABELS[kind]} <span className="text-xs text-fg-subtle">(excluded)</span></span>
                <Button type="button" variant="ghost" size="sm" onClick={() => toggle(kind)}>Include</Button>
              </li>
            ))}
          </ol>
          <div className="mt-4">
            <Label htmlFor="geo-conflict">When is a vendor claim contradicted? (default)</Label>
            <Select id="geo-conflict" value={policy.default_conflict_rule} onChange={(e) => update({ default_conflict_rule: e.target.value as GeoSettingsDoc['default_conflict_rule'] })}>
              <option value="consensus">Consensus: every independent source agrees, and disagrees with the vendor</option>
              <option value="first">First: the top-ranked independent source disagrees with the vendor</option>
            </Select>
            <p className="text-[11px] text-fg-subtle mt-1">Consensus is safer: IP databases lag on residential ranges, so one dissenting database alone should not count against a vendor.</p>
          </div>
          <p className="text-xs text-fg-muted mt-4 border-t border-line pt-3">
            What a contradicted claim does (ignore, warn, strict) and whether sessions are verified before their first request (off, report, retry, reject) are decided per project, on the project's Location tab, because different projects tolerate different risk.
          </p>
        </Card>

        <Card className="p-5">
          <h3 className="text-sm font-semibold text-fg mb-1">Echo endpoint</h3>
          <p className="text-xs text-fg-muted mb-3">
            Requested through a proxy to learn its exit IP. Used by health checks, discovery, exit lookups and preflight. Every Octoprox instance serves <span className="font-mono">/echo</span>; point this at a publicly reachable one, or at the standalone echo service.
            {data && !data.echo_enabled && <span className="text-warning"> The local /echo endpoint is disabled in this install's config.</span>}
          </p>
          <div className="space-y-3">
            <div>
              <Label htmlFor="geo-echo-url">URL</Label>
              <Input id="geo-echo-url" className="font-mono text-xs" value={policy.echo_url} onChange={(e) => update({ echo_url: e.target.value })} required />
            </div>
            <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] gap-3">
              <div className="min-w-0">
                <Label htmlFor="geo-echo-ip">IP path</Label>
                <Input id="geo-echo-ip" className="font-mono text-xs" value={policy.echo_ip_path} onChange={(e) => update({ echo_ip_path: e.target.value })} placeholder="ip" required />
              </div>
              <div className="min-w-0">
                <Label htmlFor="geo-echo-country">Country path</Label>
                <Input id="geo-echo-country" className="font-mono text-xs" value={policy.echo_country_path ?? ''} onChange={(e) => update({ echo_country_path: e.target.value || null })} placeholder="none" />
              </div>
              <div className="min-w-0">
                <Label htmlFor="geo-echo-timeout">Timeout (s)</Label>
                <Input id="geo-echo-timeout" type="number" min={1} step={1} value={policy.echo_timeout_seconds} onChange={(e) => update({ echo_timeout_seconds: parseFloat(e.target.value) || 15 })} />
              </div>
            </div>
            <label className="flex items-center gap-2 text-[13px] text-fg">
              <input type="checkbox" checked={policy.health_check_attribution} onChange={(e) => update({ health_check_attribution: e.target.checked })} />
              Attribute the IP each health check sees when the check URL is the echo URL
            </label>
          </div>

          <h3 className="text-sm font-semibold text-fg mt-5 mb-1">Preflight</h3>
          <div className="grid grid-cols-2 gap-3 items-start">
            <div>
              <Label htmlFor="geo-preflight-ttl" className="whitespace-nowrap">Verdict cache (seconds)</Label>
              <Input id="geo-preflight-ttl" type="number" min={10} value={policy.preflight_session_ttl_seconds} onChange={(e) => update({ preflight_session_ttl_seconds: parseInt(e.target.value) || 600 })} />
              <p className="text-[11px] text-fg-subtle mt-1">One echo request per proxy per this window.</p>
            </div>
            <div>
              <Label htmlFor="geo-preflight-attempts" className="whitespace-nowrap">Retry attempts</Label>
              <Input id="geo-preflight-attempts" type="number" min={1} max={10} value={policy.preflight_max_attempts} onChange={(e) => update({ preflight_max_attempts: parseInt(e.target.value) || 1 })} />
              <p className="text-[11px] text-fg-subtle mt-1">Proxies tried in retry mode before a 502.</p>
            </div>
          </div>

          <h3 className="text-sm font-semibold text-fg mt-5 mb-1">History</h3>
          <div className="grid grid-cols-2 gap-3 items-start">
            <div>
              <Label htmlFor="geo-retention" className="whitespace-nowrap">Raw observations (days)</Label>
              <Input id="geo-retention" type="number" min={0} value={policy.observation_retention_days} onChange={(e) => update({ observation_retention_days: parseInt(e.target.value) || 0 })} />
              <p className="text-[11px] text-fg-subtle mt-1">Accuracy totals are kept forever.</p>
            </div>
            <div>
              <Label htmlFor="geo-exit-retention" className="whitespace-nowrap">Exit IPs not seen for (days)</Label>
              <Input id="geo-exit-retention" type="number" min={0} value={policy.exit_ip_retention_days} onChange={(e) => update({ exit_ip_retention_days: parseInt(e.target.value) || 0 })} />
              <p className="text-[11px] text-fg-subtle mt-1">0 keeps every exit ever handed out.</p>
            </div>
          </div>
        </Card>
      </div>

      <div className="flex items-center justify-between gap-3">
        <span className="text-xs text-fg-subtle">{data?.from_database ? 'Saved in the database; applies to every instance.' : 'Using the defaults from the config file until saved.'}</span>
        <div className="flex gap-2">
          {draft && <Button type="button" variant="outline" size="sm" onClick={() => setDraft(null)}>Discard</Button>}
          <Button type="submit" size="sm" disabled={!draft || mutation.isPending}>{mutation.isPending ? 'Saving…' : 'Save settings'}</Button>
        </div>
      </div>

      <LookupCard />
    </form>
  )
}

function LookupCard() {
  const [ip, setIp] = useState('')
  const [claimed, setClaimed] = useState('')
  const [result, setResult] = useState<GeoLookupResponse | null>(null)
  const mutation = useMutation({
    mutationFn: () => lookupGeoIp(ip.trim(), claimed.trim() || undefined),
    onSuccess: setResult,
  })
  return (
    <Card className="p-5">
      <div className="flex items-center gap-2 mb-1">
        <Search className="w-4 h-4 text-fg-muted" />
        <h3 className="text-sm font-semibold text-fg">Test an IP</h3>
      </div>
      <p className="text-xs text-fg-muted mb-3">See what every loaded database says and how the default source policy resolves it, optionally against a vendor claim. A project's own policy is tested from the API with its project id.</p>
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-[200px]">
          <Label htmlFor="geo-lookup-ip">IP address</Label>
          <Input id="geo-lookup-ip" className="font-mono text-sm" value={ip} onChange={(e) => setIp(e.target.value)} placeholder="203.0.113.7" />
        </div>
        <div className="w-56">
          <Label htmlFor="geo-lookup-claim">Vendor claims</Label>
          <Input id="geo-lookup-claim" className="font-mono text-sm" value={claimed} onChange={(e) => setClaimed(e.target.value.toUpperCase())} placeholder="Optional, e.g. GB" maxLength={2} />
        </div>
        <Button type="button" size="sm" onClick={() => mutation.mutate()} disabled={!ip.trim() || mutation.isPending}>{mutation.isPending ? 'Looking up…' : 'Look up'}</Button>
      </div>
      {mutation.error && <Alert variant="error" className="mt-3">{(mutation.error as Error).message}</Alert>}
      {result && (
        <div className="mt-4 space-y-2 text-sm">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono">{result.ip}</span>
            <span className="text-fg-muted">resolves to</span>
            {result.resolution.country ? <Badge color="blue">{result.resolution.country}</Badge> : <Badge color="gray">unknown</Badge>}
            {result.resolution.source && <span className="text-xs text-fg-subtle">from {SOURCE_LABELS[result.resolution.source]}{result.resolution.origin && result.resolution.source === 'database' ? '' : ''}</span>}
            {result.resolution.conflict && <Badge color="red">vendor claim contradicted</Badge>}
            {result.resolution.disagreement && <Badge color="yellow">sources disagree</Badge>}
            {result.databases_loaded === 0 && <span className="text-xs text-warning">No databases loaded.</span>}
          </div>
          {result.candidates.length > 0 && (
            <table className="w-full text-xs">
              <thead className="text-fg-subtle text-left"><tr><th className="py-1 pr-3 font-medium">Source</th><th className="py-1 pr-3 font-medium">Country</th><th className="py-1 font-medium">Details</th></tr></thead>
              <tbody>
                {result.candidates.map((c, i) => (
                  <tr key={i} className="border-t border-line">
                    <td className="py-1.5 pr-3">{SOURCE_LABELS[c.source]}{c.source === 'database' && <span className="text-fg-subtle"> · {c.origin.slice(0, 8)}</span>}</td>
                    <td className="py-1.5 pr-3 font-mono">{c.country ?? '-'}</td>
                    <td className="py-1.5 text-fg-muted">{describeLocation(c.location)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </Card>
  )
}

export function describeLocation(location: GeoLookupResponse['resolution']['location'] | null | undefined): string {
  if (!location) return ''
  const parts = [location.city, location.region].filter(Boolean)
  const asn = location.asn ? `AS${location.asn}${location.organization ? ` ${location.organization}` : ''}` : location.organization
  const flags = [location.is_hosting && 'hosting', location.is_vpn && 'VPN', location.is_public_proxy && 'public proxy', location.is_tor && 'Tor', location.is_residential_proxy && 'residential proxy'].filter(Boolean)
  return [parts.join(', '), asn, flags.length ? flags.join(', ') : ''].filter(Boolean).join(' · ')
}
