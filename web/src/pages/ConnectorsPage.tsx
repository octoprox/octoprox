// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Link2, Trash2, Plus, AlertTriangle } from 'lucide-react'
import {
  fetchProjectConnectors, fetchProjectCredentials, fetchProjectCredential, fetchConnectorOptions,
  createProjectConnector, updateProjectConnector, deleteProjectConnector, fetchBrightDataZones,
  Connector, CredentialType, ConnectorCreate, ConnectorUpdate, ConnectorOptions, OxylabsProxyType, BrightDataProxyType,
  RoutingConfig, RateLimitConfig, Credential, CredentialDetail,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useTheme } from '../contexts/ThemeContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { DataTable } from '../components/DataTable'
import { Page, EmptyState } from '../components/layout/Page'
import { NewCredentialPanel, TypePicker, TypeCard } from '../components/CredentialForm'
import { CREDENTIAL_TYPES } from '../utils/credentials'
import { relativeTime, formatDateTime, formatDate } from '../utils/format'
import { RichSelect, RichSelectOption } from '../components/RichSelect'
import { Button, Input, Label, Badge, Alert, ChipInput, Inspector, Tabs, ConfirmDialog, KeyValue, InspectorSection } from '../components/ui'

type DomainFilterMode = 'none' | 'whitelist' | 'blacklist'
type ConfigTab = 'infrastructure' | 'scaling' | 'advanced' | 'routing' | 'rate_limiting'

interface ConnectorFormData {
  name: string
  credential_id: string
  config: Record<string, string>
  routing_config: RoutingConfig
  rate_limit_config: RateLimitConfig
  enabled: boolean
}

const EMPTY_FORM: ConnectorFormData = { name: '', credential_id: '', config: {}, routing_config: {}, rate_limit_config: {}, enabled: true }

// ---------------------------------------------------------------------------
// Helpers shared by the table and the editor

const getConfiguredProxies = (connector: Connector): number | null => {
  const config = connector.config
  const credType = connector.credential_type
  if (!credType || credType === 'static_proxy_provider') return null
  if (credType === 'oxylabs' || credType === 'brightdata') return typeof config.num_proxies === 'number' ? config.num_proxies : null
  if (credType === 'aws' || credType === 'gcp' || credType === 'azure') return typeof config.max_proxies === 'number' ? config.max_proxies : null
  return null
}

const getCredentialTypeLabel = (type: string | null) => {
  if (!type) return 'Unknown'
  return CREDENTIAL_TYPES.find((ct) => ct.value === type)?.label || type
}

const getDefaultConfig = (type: CredentialType | null, options?: ConnectorOptions): Record<string, string> => {
  switch (type) {
    case 'static_proxy_provider':
      return {}
    case 'aws':
      return { instance_name: '', region: options?.aws_regions[0]?.code || 'us-east-1', instance_type: options?.aws_instance_types[0]?.code || 't3.micro', key_pair_name: '', security_group: '', tags: '{}', min_proxies: '1', max_proxies: '10', min_rotation_period_minutes: '60', max_rotation_period_minutes: '1440' }
    case 'gcp':
      return { project_id: '', instance_name: '', zone: options?.gcp_zones[0]?.code || 'us-central1-a', machine_type: options?.gcp_machine_types[0]?.code || 'e2-micro', network: 'default', tags: '{}', min_proxies: '1', max_proxies: '10', min_rotation_period_minutes: '60', max_rotation_period_minutes: '1440' }
    case 'azure':
      return { subscription_id: '', resource_group: '', instance_name: '', location: options?.azure_locations[0]?.code || 'eastus', vm_size: options?.azure_vm_sizes[0]?.code || 'Standard_B1s', vnet_name: '', subnet_name: '', ssh_public_key: '', tags: '{}', min_proxies: '1', max_proxies: '10', min_rotation_period_minutes: '60', max_rotation_period_minutes: '1440' }
    case 'oxylabs':
      return { num_proxies: '1', country_code: '', session_duration_minutes: '10' }
    default:
      return {}
  }
}

// ---------------------------------------------------------------------------
// Page

type PanelState = { kind: 'edit'; id: string } | { kind: 'new' } | null

export default function ConnectorsPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { isDark } = useTheme()
  const { canMutate } = useAuth()
  const toast = useToast()
  const [searchParams, setSearchParams] = useSearchParams()
  const [panel, setPanel] = useState<PanelState>(null)
  const [pendingDelete, setPendingDelete] = useState<Connector | null>(null)

  const { data: connectorsData, isLoading } = useQuery({
    queryKey: ['connectors', selectedProjectId],
    queryFn: () => fetchProjectConnectors(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: 15000, // proxy counts and provisioning errors
  })

  // Deep link from the Overview connector list: ?open=<id>
  useEffect(() => {
    const open = searchParams.get('open')
    if (open) {
      setPanel({ kind: 'edit', id: open })
      searchParams.delete('open')
      setSearchParams(searchParams, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProjectConnector(selectedProjectId!, id),
    onSuccess: (_r, id) => {
      queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['proxies', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
      if (panel?.kind === 'edit' && panel.id === id) setPanel(null)
      setPendingDelete(null)
      toast.show('Connector deleted')
    },
    onError: (e: Error) => { setPendingDelete(null); toast.show(e.message || 'Failed to delete connector', 'error') },
  })

  const connectors = connectorsData?.connectors ?? []
  const openConnector = panel?.kind === 'edit' ? connectors.find((c) => c.id === panel.id) ?? null : null

  const columns: ColumnDef<Connector>[] = useMemo(() => [
    {
      accessorKey: 'name',
      header: 'Connector',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => {
        const ct = CREDENTIAL_TYPES.find((t) => t.value === row.original.credential_type)
        return (
          <span className="inline-flex items-center gap-2.5 max-w-full">
            {ct && <img src={isDark ? ct.logoDark : ct.logo} alt="" className="w-5 h-5 object-contain flex-none" />}
            <span className="font-medium truncate">{row.original.name}</span>
          </span>
        )
      },
    },
    {
      id: 'credential',
      accessorFn: (row: Connector) => row.credential_name || 'Unknown',
      header: 'Credential',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="text-fg-muted truncate block">
          {row.original.credential_name || 'Unknown'} <span className="text-fg-subtle">· {getCredentialTypeLabel(row.original.credential_type)}</span>
        </span>
      ),
    },
    {
      accessorKey: 'proxy_count',
      header: 'Proxies',
      size: 150,
      cell: ({ row }) => {
        const max = getConfiguredProxies(row.original)
        const n = row.original.proxy_count
        return (
          <span className="inline-flex items-center gap-2.5">
            <span className="tabular-nums font-medium">{n}{max != null && <span className="text-fg-subtle font-normal"> / {max}</span>}</span>
            {max != null && max > 0 && (
              <span className="w-16 h-1 rounded-full bg-primary-soft overflow-hidden inline-block">
                <span className="block h-full bg-primary rounded-full" style={{ width: `${Math.min(100, (n / max) * 100)}%` }} />
              </span>
            )}
          </span>
        )
      },
    },
    {
      id: 'status',
      accessorFn: (row: Connector) => (row.last_error ? 'Error' : row.enabled ? 'Enabled' : 'Disabled'),
      header: 'Status',
      size: 170,
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-1.5">
          {row.original.last_error && (
            <Badge color="red" className="inline-flex items-center gap-1" title={row.original.last_error}>
              <AlertTriangle className="w-3 h-3" /> Error
            </Badge>
          )}
          <Badge color={row.original.enabled ? 'green' : 'gray'}>{row.original.enabled ? 'Enabled' : 'Disabled'}</Badge>
        </span>
      ),
    },
    {
      accessorKey: 'updated_at',
      header: 'Updated',
      size: 120,
      cell: ({ getValue }) => <span className="text-fg-muted text-xs" title={formatDateTime(getValue<string>())}>{relativeTime(getValue<string>())}</span>,
    },
    ...(canMutate ? [{
      id: 'actions',
      header: '',
      size: 56,
      enableSorting: false,
      cell: ({ row }: { row: { original: Connector } }) => (
        <div className="flex justify-end">
          <button onClick={() => setPendingDelete(row.original)} className="p-1 rounded text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Delete">
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    }] as ColumnDef<Connector>[] : []),
  ], [canMutate, isDark])

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'edit' && openConnector) {
    panelNode = (
      <ConnectorEditor
        key={openConnector.id}
        connector={openConnector}
        canMutate={canMutate}
        onClose={() => setPanel(null)}
        onDelete={() => setPendingDelete(openConnector)}
        onSaved={() => toast.show('Connector saved')}
      />
    )
  } else if (panel?.kind === 'new') {
    panelNode = (
      <ConnectorEditor
        key="new"
        canMutate={canMutate}
        onClose={() => setPanel(null)}
        onSaved={(c) => { setPanel(null); toast.show(`Connector "${c.name}" created`) }}
      />
    )
  }

  const errored = connectors.filter((c) => c.last_error)

  return (
    <Page
      title="Connectors"
      count={connectorsData?.total}
      subtitle={connectors.length > 0 ? `${connectors.reduce((a, c) => a + c.proxy_count, 0)} proxies provisioned` : 'Where this project\'s proxies come from'}
      actions={canMutate ? <Button size="sm" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> Add connector</Button> : undefined}
      panel={panelNode}
    >
      {isLoading ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : connectors.length === 0 ? (
        <EmptyState
          icon={<Link2 />}
          title="No connectors yet"
          description="A connector turns a credential into proxies: cloud instances, provider sessions or a static list you manage yourself."
          action={canMutate ? <Button size="sm" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> Add connector</Button> : undefined}
        />
      ) : (
        <>
          <DataTable
            columns={columns}
            data={connectors}
            getRowId={(row) => row.id}
            onRowClick={(row) => setPanel({ kind: 'edit', id: row.id })}
            activeRowId={panel?.kind === 'edit' ? panel.id : null}
            columnVisibility={panel ? { updated_at: false, actions: false } : {}}
          />
          {errored.map((c) => (
            <Alert key={c.id} className="flex items-start gap-2.5 text-[12.5px]">
              <AlertTriangle className="w-4 h-4 flex-none mt-0.5" />
              <span>
                <b className="font-semibold">{c.name}</b> failed {c.consecutive_errors} consecutive provisioning attempt{c.consecutive_errors === 1 ? '' : 's'}:{' '}
                <span className="font-mono text-xs">{c.last_error}</span>{' '}
                <button onClick={() => setPanel({ kind: 'edit', id: c.id })} className="underline font-medium">Open connector</button>
              </span>
            </Alert>
          ))}
        </>
      )}
      {pendingDelete && (
        <ConfirmDialog
          title="Delete connector?"
          message={<>Remove <b className="text-fg">{pendingDelete.name}</b> and its {pendingDelete.proxy_count} prox{pendingDelete.proxy_count === 1 ? 'y' : 'ies'} from the pool. Cloud instances it manages are terminated.</>}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => deleteMutation.mutate(pendingDelete.id)}
          isLoading={deleteMutation.isPending}
        />
      )}
    </Page>
  )
}

// ---------------------------------------------------------------------------
// Editor: create (type → configure) or edit, with nested credential creation.
// Stays mounted across the nested step so the form is not lost.

function ConnectorEditor({ connector, canMutate, onClose, onDelete, onSaved }: {
  connector?: Connector
  canMutate: boolean
  onClose: () => void
  onDelete?: () => void
  onSaved: (c: Connector) => void
}) {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const isEdit = !!connector

  const [type, setType] = useState<CredentialType | null>((connector?.credential_type as CredentialType) ?? null)
  const [formData, setFormData] = useState<ConnectorFormData>(() => {
    if (!connector) return EMPTY_FORM
    const configForForm: Record<string, string> = {}
    for (const [key, value] of Object.entries(connector.config || {})) {
      configForForm[key] = key === 'tags' && typeof value === 'object' ? JSON.stringify(value) : String(value ?? '')
    }
    return {
      name: connector.name,
      credential_id: connector.credential_id,
      config: configForForm,
      routing_config: connector.routing_config || {},
      rate_limit_config: connector.rate_limit_config || {},
      enabled: connector.enabled,
    }
  })
  const [activeTab, setActiveTab] = useState<ConfigTab>(connector?.credential_type === 'static_proxy_provider' ? 'routing' : 'infrastructure')
  const [error, setError] = useState<string | null>(null)
  const [creatingCredential, setCreatingCredential] = useState(false)

  const { data: credentialsData } = useQuery({
    queryKey: ['credentials', selectedProjectId],
    queryFn: () => fetchProjectCredentials(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: false,
  })
  const { data: optionsData } = useQuery({
    queryKey: ['connector-options'],
    queryFn: fetchConnectorOptions,
    refetchInterval: false,
    staleTime: Infinity,
  })
  const { data: selectedCredentialData } = useQuery({
    queryKey: ['credential', selectedProjectId, formData.credential_id],
    queryFn: () => fetchProjectCredential(selectedProjectId!, formData.credential_id),
    enabled: !!selectedProjectId && !!formData.credential_id && type === 'oxylabs',
    refetchInterval: false,
  })
  const { data: brightDataZones } = useQuery({
    queryKey: ['brightdata-zones', formData.credential_id],
    queryFn: () => fetchBrightDataZones(formData.credential_id),
    enabled: !!formData.credential_id && type === 'brightdata',
    refetchInterval: false,
    staleTime: 5 * 60 * 1000,
  })

  const credentialsForType: Credential[] = useMemo(
    () => credentialsData?.credentials.filter((c) => c.type === type) || [],
    [credentialsData, type]
  )

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
    queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
  }
  const createMutation = useMutation({
    mutationFn: (data: ConnectorCreate) => createProjectConnector(selectedProjectId!, data),
    onSuccess: (c) => { invalidate(); onSaved(c) },
    onError: (e: Error) => setError(e.message || 'Failed to create connector'),
  })
  const updateMutation = useMutation({
    mutationFn: (data: ConnectorUpdate) => updateProjectConnector(selectedProjectId!, connector!.id, data),
    onSuccess: (c) => { invalidate(); onSaved(c) },
    onError: (e: Error) => setError(e.message || 'Failed to update connector'),
  })

  const handleTypeSelect = (t: CredentialType) => {
    setType(t)
    const creds = credentialsData?.credentials.filter((c) => c.type === t) || []
    setFormData({ ...EMPTY_FORM, credential_id: creds.length === 1 ? creds[0].id : '', config: getDefaultConfig(t, optionsData) })
    setActiveTab(t === 'static_proxy_provider' ? 'routing' : 'infrastructure')
  }

  const handleConfigChange = (key: string, value: string) => {
    setFormData((prev) => ({ ...prev, config: { ...prev.config, [key]: value } }))
  }

  const prepareConfigForSubmit = (config: Record<string, string>): Record<string, unknown> => {
    const result: Record<string, unknown> = { ...config }
    if (config.tags) {
      try { result.tags = JSON.parse(config.tags) } catch { result.tags = {} }
    }
    return result
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (!formData.credential_id) { setError('Select or create a credential first.'); return }
    const preparedConfig = prepareConfigForSubmit(formData.config)
    if (isEdit) {
      updateMutation.mutate({ name: formData.name, credential_id: formData.credential_id, enabled: formData.enabled, config: preparedConfig, routing_config: formData.routing_config, rate_limit_config: formData.rate_limit_config })
    } else {
      createMutation.mutate({ name: formData.name, credential_id: formData.credential_id, config: preparedConfig, routing_config: formData.routing_config, rate_limit_config: formData.rate_limit_config, enabled: formData.enabled })
    }
  }

  // --- Nested step: create a credential of this type, then come back with it selected.
  if (creatingCredential && type) {
    return (
      <NewCredentialPanel
        fixedType={type}
        crumb={isEdit ? 'Edit connector' : 'New connector'}
        width={600}
        onBack={() => setCreatingCredential(false)}
        onClose={onClose}
        onCreated={(cred: CredentialDetail) => {
          setFormData((prev) => ({ ...prev, credential_id: cred.id }))
          setCreatingCredential(false)
        }}
      />
    )
  }

  // --- Step 1 (create only): choose the type
  if (!type) {
    return (
      <Inspector title="Add connector" subtitle="Step 1 of 2 · Type" onClose={onClose} width={600}>
        <TypePicker onPick={handleTypeSelect} hint="Choose where the proxies come from." />
      </Inspector>
    )
  }

  // --- Oxylabs / BrightData derived state
  const getOxylabsProxyType = (): OxylabsProxyType | null => {
    if (type !== 'oxylabs' || !selectedCredentialData) return null
    return (selectedCredentialData.config as { proxy_type?: OxylabsProxyType })?.proxy_type || null
  }
  const isSessionBasedOxylabs = () => { const t = getOxylabsProxyType(); return t === 'residential' || t === 'mobile' }
  const getBrightDataProxyType = (): BrightDataProxyType | null => {
    if (type !== 'brightdata' || !formData.config.zone_name || !brightDataZones) return null
    const zone = brightDataZones.find((z) => z.name === formData.config.zone_name)
    return (zone?.proxy_type as BrightDataProxyType) || null
  }
  const isSessionBasedBrightData = () => { const t = getBrightDataProxyType(); return t === 'residential' || t === 'mobile' }

  // --- Routing tab
  const getRoutingMode = (): DomainFilterMode => {
    const rc = formData.routing_config
    if ('domain_whitelist' in rc) return 'whitelist'
    if ('domain_blacklist' in rc) return 'blacklist'
    return 'none'
  }
  const getRoutingDomainsArray = (): string[] => {
    const rc = formData.routing_config
    if (rc.domain_whitelist && rc.domain_whitelist.length > 0) return rc.domain_whitelist
    if (rc.domain_blacklist && rc.domain_blacklist.length > 0) return rc.domain_blacklist
    return []
  }
  const handleRoutingModeChange = (mode: DomainFilterMode) => {
    if (mode === 'none') { setFormData({ ...formData, routing_config: {} }); return }
    const domains = getRoutingDomainsArray()
    setFormData({ ...formData, routing_config: mode === 'whitelist' ? { domain_whitelist: domains } : { domain_blacklist: domains } })
  }
  const setRoutingDomains = (domains: string[]) => {
    const mode = getRoutingMode()
    setFormData({ ...formData, routing_config: mode === 'whitelist' ? { domain_whitelist: domains } : { domain_blacklist: domains } })
  }
  const addRoutingDomain = (input: string) => {
    const newDomains = input.split(/[\n,]+/).map((d) => d.trim().toLowerCase()).filter((d) => d)
    if (newDomains.length === 0) return
    const existing = getRoutingDomainsArray()
    const unique = newDomains.filter((d) => !existing.includes(d))
    if (unique.length > 0) setRoutingDomains([...existing, ...unique])
  }
  const removeRoutingDomain = (index: number) => setRoutingDomains(getRoutingDomainsArray().filter((_, i) => i !== index))

  const renderRoutingTab = () => (
    <div className="space-y-4">
      <div>
        <Label className="text-xs">Domain filter mode</Label>
        <div className="flex gap-1.5 mt-1 flex-wrap">
          {([{ value: 'none', label: 'No restriction' }, { value: 'whitelist', label: 'Whitelist' }, { value: 'blacklist', label: 'Blacklist' }] as const).map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => handleRoutingModeChange(option.value)}
              className={`px-2.5 py-1 text-[12.5px] font-medium rounded-full border transition-colors ${getRoutingMode() === option.value ? 'border-primary bg-primary-soft text-primary-soft-fg' : 'border-line text-fg-muted hover:border-line-strong hover:text-fg'}`}
            >
              {option.label}
            </button>
          ))}
        </div>
        <p className="text-xs text-fg-muted mt-1.5">
          {getRoutingMode() === 'none' && 'All domains can be routed through this connector\'s proxies.'}
          {getRoutingMode() === 'whitelist' && 'Only the listed domains can be routed through this connector\'s proxies.'}
          {getRoutingMode() === 'blacklist' && 'All domains except the listed ones can be routed through this connector\'s proxies.'}
        </p>
      </div>
      {getRoutingMode() !== 'none' && (
        <div>
          <ChipInput
            label={getRoutingMode() === 'whitelist' ? 'Allowed domains' : 'Blocked domains'}
            values={getRoutingDomainsArray()}
            onAdd={addRoutingDomain}
            onRemove={removeRoutingDomain}
            placeholder="Type a domain and press Enter…"
          />
          <p className="text-xs text-fg-muted mt-1">Subdomains are included automatically (<code className="bg-surface-raised px-1 rounded">bing.com</code> also matches <code className="bg-surface-raised px-1 rounded">www.bing.com</code>).</p>
        </div>
      )}
    </div>
  )

  const Toggle = ({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) => (
    <label className="relative inline-flex items-center cursor-pointer shrink-0">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="sr-only peer" />
      <div className="w-9 h-5 bg-surface-raised peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-ring rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:border-line-strong after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary" />
    </label>
  )

  const renderRateLimitingTab = () => (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <Label className="text-xs mb-0">Enable rate limiting</Label>
        <Toggle
          checked={!!formData.rate_limit_config.max_requests}
          onChange={(on) => setFormData({ ...formData, rate_limit_config: on ? { max_requests: 100, window_seconds: 60, quarantine_seconds_min: 60, quarantine_seconds_max: 300 } : {} })}
        />
      </div>
      {formData.rate_limit_config.max_requests ? (
        <>
          <div className="grid grid-cols-2 gap-3">
            {([
              ['max_requests', 'Max requests'], ['window_seconds', 'Window (seconds)'],
              ['quarantine_seconds_min', 'Min quarantine (s)'], ['quarantine_seconds_max', 'Max quarantine (s)'],
            ] as const).map(([key, label]) => (
              <div key={key}>
                <Label className="text-xs">{label}</Label>
                <Input
                  type="number"
                  min={1}
                  className="px-3 py-1.5 text-sm"
                  value={formData.rate_limit_config[key] || ''}
                  onChange={(e) => setFormData({ ...formData, rate_limit_config: { ...formData.rate_limit_config, [key]: parseInt(e.target.value) || 1 } })}
                />
              </div>
            ))}
          </div>
          <div className="flex items-center justify-between gap-4 border-t border-line pt-3">
            <Label className="text-xs mb-0">Sticky session quarantine <span className="font-normal text-fg-subtle">— block fallback to other proxies for sticky sessions</span></Label>
            <Toggle
              checked={!!formData.rate_limit_config.sticky_quarantine}
              onChange={(v) => setFormData({ ...formData, rate_limit_config: { ...formData.rate_limit_config, sticky_quarantine: v } })}
            />
          </div>
        </>
      ) : (
        <p className="text-xs text-fg-muted">No rate limiting applied. Proxies handle requests without throttling.</p>
      )}
    </div>
  )

  // --- Provider-specific tabs and fields
  const toInstanceTypeOptions = (opts: ConnectorOptions['aws_instance_types'] | undefined): RichSelectOption[] =>
    (opts || []).map((opt) => ({ value: opt.code, label: opt.code, description: opt.description, badge: opt.architecture }))
  const toRegionOptions = (opts: ConnectorOptions['aws_regions'] | undefined): RichSelectOption[] =>
    (opts || []).map((opt) => ({ value: opt.code, label: opt.name, description: opt.code }))

  const getFieldLabel = (key: string): string => {
    const labels: Record<string, string> = {
      instance_name: 'Instance name', region: 'Region', instance_type: 'Instance type', key_pair_name: 'Key pair', security_group: 'Security group',
      project_id: 'Project ID', zone: 'Zone', machine_type: 'Machine type', network: 'Network', subnetwork: 'Subnetwork', ssh_key: 'SSH key',
      ssh_public_key: 'SSH public key', subscription_id: 'Subscription ID', num_proxies: 'Number of proxies', country_code: 'Country',
      session_duration_minutes: 'Session duration', location: 'Location', vm_size: 'VM size', resource_group: 'Resource group',
      vnet_name: 'Virtual network', subnet_name: 'Subnet', min_proxies: 'Min proxies', max_proxies: 'Max proxies',
      min_rotation_period_minutes: 'Min rotation (min)', max_rotation_period_minutes: 'Max rotation (min)',
    }
    return labels[key] || key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())
  }

  const tabsFor = (t: CredentialType): { id: ConfigTab; label: string }[] => {
    if (t === 'static_proxy_provider') return [{ id: 'routing', label: 'Routing' }, { id: 'rate_limiting', label: 'Rate limiting' }]
    if (t === 'oxylabs') return [{ id: 'infrastructure', label: 'General' }, { id: 'routing', label: 'Routing' }, { id: 'rate_limiting', label: 'Rate limiting' }]
    if (t === 'brightdata') return [{ id: 'infrastructure', label: 'General' }, { id: 'advanced', label: 'Advanced' }, { id: 'routing', label: 'Routing' }, { id: 'rate_limiting', label: 'Rate limiting' }]
    return [{ id: 'infrastructure', label: 'Infrastructure' }, { id: 'scaling', label: 'Scaling' }, { id: 'advanced', label: 'Advanced' }, { id: 'routing', label: 'Routing' }, { id: 'rate_limiting', label: 'Rate limiting' }]
  }

  const renderOxylabsGeneral = () => {
    const proxyType = getOxylabsProxyType()
    const isSessionBased = isSessionBasedOxylabs()
    const countryOptions: RichSelectOption[] = [
      { value: '', label: 'All countries', description: 'No geo-targeting' },
      ...(optionsData?.oxylabs_countries || []).map((c) => ({ value: c.code, label: c.name, description: c.code })),
    ]
    return (
      <div className="space-y-4">
        <div>
          <Label className="text-xs">Number of proxies <span className="text-danger">*</span></Label>
          <Input type="number" min="1" value={formData.config.num_proxies || '1'} onChange={(e) => handleConfigChange('num_proxies', e.target.value)} className="px-3 py-1.5 text-sm" required />
        </div>
        {isSessionBased && (
          <>
            <div>
              <Label className="text-xs">Country</Label>
              <RichSelect options={countryOptions} value={formData.config.country_code || ''} onChange={(val) => handleConfigChange('country_code', val)} placeholder="Select country (optional)" />
              <p className="text-xs text-fg-muted mt-1">Leave as “All countries” for no geo-targeting.</p>
            </div>
            <div>
              <Label className="text-xs">Session duration (minutes) <span className="text-danger">*</span></Label>
              <Input type="number" min="1" max="30" value={formData.config.session_duration_minutes || '10'} onChange={(e) => handleConfigChange('session_duration_minutes', e.target.value)} className="px-3 py-1.5 text-sm" required />
              <p className="text-xs text-fg-muted mt-1">1–30 minutes (default 10).</p>
            </div>
          </>
        )}
        {proxyType && !isSessionBased && (
          <p className="text-xs text-fg-muted bg-surface-raised p-3 rounded-lg">Port-based proxy type ({proxyType}). IPs are discovered from Oxylabs ports and refreshed every 24 hours.</p>
        )}
        {!proxyType && formData.credential_id && (
          <p className="text-xs text-warning bg-warning-soft p-3 rounded-lg">Loading credential configuration…</p>
        )}
      </div>
    )
  }

  const renderBrightDataGeneral = () => {
    const proxyType = getBrightDataProxyType()
    const isSessionBased = isSessionBasedBrightData()
    const selectedZone = brightDataZones?.find((z) => z.name === formData.config.zone_name)
    const selectedZoneIsPortBased = selectedZone && !isSessionBased
    const countryOptions: RichSelectOption[] = selectedZoneIsPortBased && selectedZone?.country_counts
      ? [
        { value: '', label: 'All countries', description: `${selectedZone.total_ips ?? 0} IPs total` },
        ...Object.entries(selectedZone.country_counts).sort(([, a], [, b]) => b - a).map(([cc, count]) => ({ value: cc.toUpperCase(), label: cc.toUpperCase(), description: `${count} IPs` })),
      ]
      : [
        { value: '', label: 'All countries', description: 'No geo-targeting' },
        ...(optionsData?.oxylabs_countries || []).map((c) => ({ value: c.code, label: c.name, description: c.code })),
      ]
    const zoneOptions: RichSelectOption[] = (brightDataZones || []).map((zone) => ({
      value: zone.name, label: zone.name,
      description: zone.total_ips != null ? `${zone.proxy_type} (${zone.type}) — ${zone.total_ips} IPs` : `${zone.proxy_type} (${zone.type})`,
    }))
    const cc = formData.config.country_code?.toLowerCase()
    const maxIps = selectedZoneIsPortBased && selectedZone?.total_ips != null
      ? (cc && selectedZone.country_counts?.[cc] ? selectedZone.country_counts[cc] : selectedZone.total_ips)
      : undefined
    return (
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-x-3 gap-y-3">
          <div>
            <Label className="text-xs">Zone <span className="text-danger">*</span></Label>
            <RichSelect
              options={zoneOptions}
              value={formData.config.zone_name || ''}
              onChange={(val) => {
                const zone = brightDataZones?.find((z) => z.name === val)
                if (zone) setFormData((prev) => ({ ...prev, config: { ...prev.config, zone_name: val, zone_password: zone.password, proxy_type: zone.proxy_type } }))
              }}
              placeholder={formData.credential_id ? 'Select zone' : 'Select a credential first'}
              disabled={!formData.credential_id}
            />
            <p className="text-xs text-fg-muted mt-1">Proxy type follows the zone.</p>
          </div>
          <div>
            <Label className="text-xs">Zone password <span className="text-danger">*</span></Label>
            <Input type="text" value={formData.config.zone_password || ''} onChange={(e) => handleConfigChange('zone_password', e.target.value)} className="px-3 py-1.5 text-sm" placeholder="Auto-filled from zone" required />
          </div>
          <div>
            <Label className="text-xs">Number of proxies <span className="text-danger">*</span></Label>
            <Input type="number" min="1" max={maxIps} value={formData.config.num_proxies || '1'} onChange={(e) => handleConfigChange('num_proxies', e.target.value)} className="px-3 py-1.5 text-sm" required />
            {maxIps != null && <p className="text-xs text-fg-muted mt-1">Max {maxIps}{cc ? ` in ${cc.toUpperCase()}` : ''}</p>}
          </div>
          <div>
            <Label className="text-xs">Country</Label>
            <RichSelect
              options={countryOptions}
              value={formData.config.country_code || ''}
              onChange={(val) => {
                handleConfigChange('country_code', val)
                if (selectedZoneIsPortBased && selectedZone?.total_ips != null) {
                  const newMax = val && selectedZone.country_counts?.[val.toLowerCase()] ? selectedZone.country_counts[val.toLowerCase()] : selectedZone.total_ips
                  if (parseInt(formData.config.num_proxies || '1', 10) > newMax) handleConfigChange('num_proxies', String(newMax))
                }
              }}
              placeholder="Select country (optional)"
            />
          </div>
        </div>
        {selectedZoneIsPortBased && selectedZone?.total_ips != null && (
          <div className="text-xs bg-primary-soft/40 p-3 rounded-lg">
            <p className="font-medium text-primary-soft-fg">{selectedZone.total_ips} IPs available in this zone</p>
            {selectedZone.country_counts && Object.keys(selectedZone.country_counts).length > 0 && (
              <p className="text-primary mt-1">{Object.entries(selectedZone.country_counts).sort(([, a], [, b]) => b - a).map(([c, n]) => `${c.toUpperCase()} (${n})`).join(', ')}</p>
            )}
          </div>
        )}
        {proxyType && isSessionBased && <p className="text-xs text-fg-muted bg-surface-raised p-3 rounded-lg">Session-based proxy type ({proxyType}). Global session IDs are used for routing.</p>}
        {!formData.config.zone_name && formData.credential_id && <p className="text-xs text-warning bg-warning-soft p-3 rounded-lg">Select a zone to continue.</p>}
      </div>
    )
  }

  const renderCloudFields = () => {
    const scalingFields = ['min_proxies', 'max_proxies', 'min_rotation_period_minutes', 'max_rotation_period_minutes']
    const infraFields: Record<string, string[]> = {
      aws: ['instance_name', 'region', 'instance_type', 'key_pair_name', 'security_group'],
      gcp: ['project_id', 'instance_name', 'zone', 'machine_type', 'network'],
      azure: ['subscription_id', 'resource_group', 'instance_name', 'location', 'vm_size', 'vnet_name', 'subnet_name', 'ssh_public_key'],
    }
    const requiredFields: Record<string, string[]> = {
      aws: ['instance_name', 'region', 'instance_type', 'key_pair_name', 'security_group'],
      gcp: ['project_id', 'instance_name', 'zone', 'machine_type'],
      azure: ['subscription_id', 'resource_group', 'instance_name', 'vm_size', 'ssh_public_key'],
    }
    const placeholders: Record<string, string> = {
      security_group: 'sg-xxxxxxxx', key_pair_name: 'my-key-pair', instance_name: 'proxy-instance', project_id: 'my-gcp-project',
      subscription_id: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', resource_group: 'my-resource-group', vnet_name: 'my-vnet', subnet_name: 'default', ssh_public_key: 'ssh-rsa AAAA… user@host',
    }
    const regionDropdownFields: Record<string, RichSelectOption[]> = {
      region: toRegionOptions(optionsData?.aws_regions), zone: toRegionOptions(optionsData?.gcp_zones), location: toRegionOptions(optionsData?.azure_locations),
    }
    const instanceTypeDropdownFields: Record<string, RichSelectOption[]> = {
      instance_type: toInstanceTypeOptions(optionsData?.aws_instance_types), machine_type: toInstanceTypeOptions(optionsData?.gcp_machine_types), vm_size: toInstanceTypeOptions(optionsData?.azure_vm_sizes),
    }
    const fields = activeTab === 'scaling' ? scalingFields : (infraFields[type] || [])
    const isNumber = (key: string) => scalingFields.includes(key)
    return (
      <div className="grid grid-cols-2 gap-x-3 gap-y-3">
        {fields.map((key) => {
          const regionOptions = regionDropdownFields[key]
          const instanceTypeOptions = instanceTypeDropdownFields[key]
          const required = requiredFields[type]?.includes(key) || false
          const wide = key === 'ssh_public_key'
          return (
            <div key={key} className={wide ? 'col-span-2' : ''}>
              <Label className="text-xs">{getFieldLabel(key)}{required && <span className="text-danger ml-1">*</span>}</Label>
              {regionOptions && regionOptions.length > 0 ? (
                <RichSelect options={regionOptions} value={formData.config[key] || ''} onChange={(val) => handleConfigChange(key, val)} placeholder={`Select ${getFieldLabel(key).toLowerCase()}`} required={required} />
              ) : instanceTypeOptions && instanceTypeOptions.length > 0 ? (
                <RichSelect options={instanceTypeOptions} value={formData.config[key] || ''} onChange={(val) => handleConfigChange(key, val)} placeholder={`Select ${getFieldLabel(key).toLowerCase()}`} required={required} />
              ) : (
                <Input type={isNumber(key) ? 'number' : 'text'} value={formData.config[key] || ''} onChange={(e) => handleConfigChange(key, e.target.value)} className="px-3 py-1.5 text-sm" placeholder={placeholders[key] || getFieldLabel(key)} required={required} />
              )}
            </div>
          )
        })}
      </div>
    )
  }

  const renderAdvanced = () => {
    if (type === 'brightdata') {
      return (
        <div>
          <Label className="text-xs">Healthcheck URL</Label>
          <Input type="url" value={formData.config.healthcheck_url || ''} onChange={(e) => handleConfigChange('healthcheck_url', e.target.value)} className="px-3 py-1.5 text-sm" placeholder="https://httpbin.org/ip (default)" />
          <p className="text-xs text-fg-muted mt-1">Use a custom URL if your BrightData zone is restricted to certain targets.</p>
        </div>
      )
    }
    return (
      <div>
        <Label className="text-xs">Instance tags</Label>
        <p className="text-xs text-fg-muted mb-2">Key/value tags applied to created instances.</p>
        <KeyValueTagsEditor value={formData.config.tags || '{}'} onChange={(val) => handleConfigChange('tags', val)} />
      </div>
    )
  }

  const renderTabContent = () => {
    if (activeTab === 'routing') return renderRoutingTab()
    if (activeTab === 'rate_limiting') return renderRateLimitingTab()
    if (activeTab === 'advanced') return renderAdvanced()
    if (type === 'oxylabs') return renderOxylabsGeneral()
    if (type === 'brightdata') return renderBrightDataGeneral()
    return renderCloudFields()
  }

  const credentialOptions: RichSelectOption[] = credentialsForType.map((c) => ({ value: c.id, label: c.name, description: `Created ${formatDate(c.created_at)}` }))
  const readOnly = !canMutate
  const saving = createMutation.isPending || updateMutation.isPending
  const typeLabel = getCredentialTypeLabel(type)

  return (
    <Inspector
      title={isEdit ? 'Edit connector' : `New ${typeLabel} connector`}
      subtitle={isEdit ? connector!.name : 'Step 2 of 2 · Configure'}
      onClose={onClose}
      onBack={!isEdit ? () => setType(null) : undefined}
      width={600}
      footer={canMutate ? (
        <>
          {isEdit && onDelete && <Button type="button" variant="danger-ghost" size="sm" onClick={onDelete}><Trash2 className="w-3.5 h-3.5" /> Delete</Button>}
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="connector-form" size="sm" disabled={saving}>{saving ? 'Saving…' : isEdit ? 'Save changes' : 'Create connector'}</Button>
        </>
      ) : undefined}
    >
      <form id="connector-form" onSubmit={handleSubmit} className="space-y-4">
        {error && <Alert>{error}</Alert>}
        {isEdit && connector?.last_error && (
          <Alert className="flex items-start gap-2 text-xs">
            <AlertTriangle className="w-3.5 h-3.5 flex-none mt-0.5" />
            <span>{connector.last_error}{connector.last_error_at && <span className="text-fg-subtle"> · {relativeTime(connector.last_error_at)}</span>}</span>
          </Alert>
        )}
        {!isEdit && <TypeCard type={type} onChange={() => setType(null)} />}

        <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 items-end">
          <div>
            <Label className="text-xs">Connector name <span className="text-danger">*</span></Label>
            <Input type="text" value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })} placeholder={`e.g. ${typeLabel} eu-west-1 fleet`} required disabled={readOnly} />
          </div>
          <label className="flex items-center gap-2 h-9 text-[13px] cursor-pointer select-none">
            <input type="checkbox" checked={formData.enabled} onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })} className="w-4 h-4" disabled={readOnly} /> Enabled
          </label>
        </div>

        <div>
          <div className="flex items-center justify-between">
            <Label className="text-xs">Credential <span className="text-danger">*</span></Label>
            {canMutate && (
              <button type="button" onClick={() => setCreatingCredential(true)} className="text-xs text-primary hover:brightness-110 inline-flex items-center gap-1 mb-1">
                <Plus className="w-3 h-3" /> New credential
              </button>
            )}
          </div>
          {credentialsForType.length === 0 ? (
            <div className="text-xs text-fg-muted border border-dashed border-line-strong rounded-lg px-3 py-2.5">
              No {typeLabel} credential yet. {canMutate ? <button type="button" onClick={() => setCreatingCredential(true)} className="text-primary font-medium">Create one</button> : 'Ask an editor to add one.'}
            </div>
          ) : (
            <RichSelect options={credentialOptions} value={formData.credential_id} onChange={(v) => setFormData({ ...formData, credential_id: v })} placeholder="Select credential" required disabled={readOnly} />
          )}
        </div>

        <Tabs<ConfigTab> tabs={tabsFor(type)} active={activeTab} onChange={setActiveTab} size="sm" />
        <fieldset disabled={readOnly} className="min-h-[160px]">{renderTabContent()}</fieldset>
      </form>

      {isEdit && connector && (
        <InspectorSection title="Details">
          <KeyValue label="Proxies" value={<>{connector.proxy_count}{getConfiguredProxies(connector) != null && <span className="text-fg-subtle font-normal"> / {getConfiguredProxies(connector)}</span>}</>} />
          <KeyValue label="Credential type" value={typeLabel} />
          <KeyValue label="Created" value={formatDateTime(connector.created_at)} />
          <KeyValue label="Updated" value={formatDateTime(connector.updated_at)} />
        </InspectorSection>
      )}
    </Inspector>
  )
}

// Key/value tag editor (AWS, Azure instance tags)
function KeyValueTagsEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const parseTags = (val: string): Array<{ key: string; value: string }> => {
    try {
      const parsed = JSON.parse(val || '{}')
      return Object.entries(parsed).map(([k, v]) => ({ key: k, value: String(v) }))
    } catch {
      return []
    }
  }
  const [tags, setTags] = useState<Array<{ key: string; value: string }>>(parseTags(value))
  const updateTags = (newTags: Array<{ key: string; value: string }>) => {
    setTags(newTags)
    const obj: Record<string, string> = {}
    newTags.forEach((t) => { if (t.key.trim()) obj[t.key.trim()] = t.value })
    onChange(JSON.stringify(obj))
  }
  return (
    <div className="space-y-2">
      {tags.map((tag, index) => (
        <div key={index} className="flex gap-2 items-center">
          <Input type="text" value={tag.key} onChange={(e) => { const n = [...tags]; n[index] = { ...n[index], key: e.target.value }; updateTags(n) }} placeholder="Key" className="flex-1 px-3 py-1.5 text-sm" />
          <Input type="text" value={tag.value} onChange={(e) => { const n = [...tags]; n[index] = { ...n[index], value: e.target.value }; updateTags(n) }} placeholder="Value" className="flex-1 px-3 py-1.5 text-sm" />
          <button type="button" onClick={() => updateTags(tags.filter((_, i) => i !== index))} className="p-1.5 text-fg-subtle hover:text-danger"><Trash2 className="w-4 h-4" /></button>
        </div>
      ))}
      <button type="button" onClick={() => updateTags([...tags, { key: '', value: '' }])} className="flex items-center gap-1 text-xs text-primary hover:brightness-110">
        <Plus className="w-3.5 h-3.5" /> Add tag
      </button>
    </div>
  )
}

