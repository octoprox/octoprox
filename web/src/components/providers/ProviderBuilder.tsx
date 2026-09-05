// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Trash2, Upload } from 'lucide-react'
import {
  createProvider, updateProvider, validateProviderSpec, importProviderYaml, isHostConfirmationError,
  ProviderDetail, ProviderSpec, ProviderValidateResponse,
} from '../../api/client'
import { useProviders } from '../../hooks/useProviders'
import { Alert, Badge, Button, ConfirmDialog, Inspector, Tabs, Textarea } from '../ui'
import { FieldListEditor } from './FieldListEditor'
import { ProxyTypeEditor } from './ProxyTypeEditor'
import { DiscoveryEditor } from './DiscoveryEditor'
import { TestPanel } from './TestPanel'
import { Checkbox, Field, Spec, TextField, inputSm, slugify } from './editors'

type Tab = 'general' | 'credential' | 'connector' | 'proxy_types' | 'discovery' | 'test' | 'yaml'

const TABS: { id: Tab; label: string }[] = [
  { id: 'general', label: 'General' },
  { id: 'credential', label: 'Credential fields' },
  { id: 'connector', label: 'Connector fields' },
  { id: 'proxy_types', label: 'Proxy types' },
  { id: 'discovery', label: 'Discovery' },
  { id: 'test', label: 'Test' },
  { id: 'yaml', label: 'YAML' },
]

export const EMPTY_SPEC: ProviderSpec = {
  id: '',
  name: '',
  description: '',
  version: 1,
  credential_fields: [],
  connector_fields: [{ key: 'num_proxies', label: 'Number of proxies', type: 'number', required: true, default: 1, min: 1 }],
  proxy_types: [],
}

/** Strip the identity so a built-in can be used as a starting point for a custom provider. */
export function duplicateSpec(spec: ProviderSpec): ProviderSpec {
  const copy = JSON.parse(JSON.stringify(spec))
  copy.id = `${copy.id}_copy`
  copy.name = `${copy.name} (copy)`
  copy.version = 1
  delete copy.beta
  return copy
}

/**
 * Admin builder for a provider descriptor: a form over the descriptor document,
 * a dry-run validator, a live test panel and YAML import/export. Saving requires
 * confirming the vendor hosts that will receive credentials.
 */
export function ProviderBuilder({ existing, initialSpec, onClose, onSaved, onDelete }: {
  existing?: ProviderDetail | null
  initialSpec?: ProviderSpec
  onClose: () => void
  onSaved: (p: ProviderDetail) => void
  onDelete?: () => void
}) {
  const queryClient = useQueryClient()
  const { presets } = useProviders()
  const isEdit = !!existing
  const [spec, setSpec] = useState<ProviderSpec>(() => JSON.parse(JSON.stringify(existing?.spec ?? initialSpec ?? EMPTY_SPEC)))
  const [tab, setTab] = useState<Tab>('general')
  const [validation, setValidation] = useState<ProviderValidateResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pendingHosts, setPendingHosts] = useState<string[] | null>(null)
  const [hostsAcknowledged, setHostsAcknowledged] = useState(false)
  const [yamlDraft, setYamlDraft] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)

  const patch = (p: Spec) => { setSpec((prev) => ({ ...prev, ...p })); setDirty(true); setValidation(null) }

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['providers'] })

  const validateMutation = useMutation({
    mutationFn: () => validateProviderSpec(spec),
    onSuccess: (v) => { setValidation(v); setError(null) },
    onError: (e: Error) => setError(e.message || 'Validation failed'),
  })

  const saveMutation = useMutation({
    mutationFn: (confirmedHosts: string[]) => (isEdit ? updateProvider(existing!.id, { spec, confirmed_hosts: confirmedHosts }) : createProvider(spec, confirmedHosts)),
    onSuccess: (p) => { invalidate(); setDirty(false); setPendingHosts(null); onSaved(p) },
    onError: (e: Error) => {
      const conflict = isHostConfirmationError(e)
      if (conflict) { setPendingHosts(conflict.egress_hosts); setHostsAcknowledged(false); return }
      setPendingHosts(null)
      setError(e.message || 'Save failed')
    },
  })

  const importMutation = useMutation({
    // First attempt without confirmed hosts: the 409 tells us which hosts to confirm.
    mutationFn: (yaml: string) => importProviderYaml(yaml, [], isEdit),
    onSuccess: (p) => { invalidate(); onSaved(p) },
    onError: (e: Error) => {
      const conflict = isHostConfirmationError(e)
      if (conflict && yamlDraft != null) {
        importProviderYaml(yamlDraft, conflict.egress_hosts, isEdit).then((p) => { invalidate(); onSaved(p) }).catch((err: Error) => setError(err.message))
        return
      }
      setError(e.message || 'Import failed')
    },
  })

  const egressHosts = useMemo(() => {
    const hosts = new Set<string>()
    const add = (url?: string) => { try { if (url) hosts.add(new URL(url.replace(/\{[^}]*\}/g, 'x')).hostname) } catch { /* template or partial */ } }
    for (const f of Object.values(spec.auth ?? {}) as Spec[]) add(f.call?.url)
    for (const s of Object.values(spec.options ?? {}) as Spec[]) { add(s.call?.url); for (const e of s.enrich ?? []) add(e.call?.url) }
    add(spec.validation?.call?.url)
    for (const t of spec.proxy_types ?? []) { add(t.known_ips?.call?.url); add(t.list?.call?.url) }
    return [...hosts].sort()
  }, [spec])

  const title = isEdit ? `Edit ${existing!.name}` : 'New provider'

  return (
    <>
      <Inspector
        title={title}
        subtitle={isEdit ? <span className="font-mono">{existing!.id}</span> : 'Describe a proxy vendor without code'}
        onClose={onClose}
        width={760}
        footer={(
          <>
            {isEdit && onDelete && <Button type="button" variant="danger-ghost" size="sm" onClick={onDelete}><Trash2 className="w-3.5 h-3.5" /> Delete</Button>}
            <span className="flex-1" />
            <Button type="button" variant="outline" size="sm" onClick={() => validateMutation.mutate()} disabled={validateMutation.isPending}>{validateMutation.isPending ? 'Checking…' : 'Check'}</Button>
            <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
            <Button type="button" size="sm" onClick={() => saveMutation.mutate(egressHosts.length && hostsAcknowledged ? egressHosts : [])} disabled={saveMutation.isPending || (isEdit && !dirty)}>
              {saveMutation.isPending ? 'Saving…' : isEdit ? 'Save changes' : 'Create provider'}
            </Button>
          </>
        )}
      >
        {error && <Alert className="text-xs">{error}</Alert>}
        {validation && (
          <div className="space-y-1.5">
            <Alert variant={validation.valid ? 'success' : 'error'} className="text-xs flex items-start gap-2">
              {validation.valid ? <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 flex-none" /> : <AlertTriangle className="w-3.5 h-3.5 mt-0.5 flex-none" />}
              <span>
                {validation.valid ? 'Descriptor is valid.' : 'Descriptor has errors.'}
                {validation.errors.map((e, i) => <span key={i} className="block">• {e}</span>)}
              </span>
            </Alert>
            {validation.warnings.length > 0 && (
              <Alert variant="warning" className="text-xs">{validation.warnings.map((w, i) => <span key={i} className="block">• {w}</span>)}</Alert>
            )}
          </div>
        )}
        <Tabs<Tab> tabs={TABS} active={tab} onChange={setTab} size="sm" />

        {tab === 'general' && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <TextField label="Name" value={spec.name} onChange={(v) => patch({ name: v, id: isEdit ? spec.id : (spec.id || slugify(v)) })} placeholder="Acme Proxies" required />
              <TextField label="Id" value={spec.id} onChange={(v) => !isEdit && patch({ id: slugify(v) })} mono help={isEdit ? 'Ids cannot be changed after creation.' : 'Becomes the credential type. Lowercase letters, digits, - and _.'} required />
              <TextField label="Description" value={spec.description} onChange={(v) => patch({ description: v })} className="col-span-2" placeholder="Residential and ISP proxies" />
              <TextField label="Documentation URL" value={spec.docs_url} onChange={(v) => patch({ docs_url: v || null })} mono placeholder="https://docs.vendor.com" />
              <Field label="Logo (SVG or PNG, under 200 KB)">
                <div className="flex items-center gap-3">
                  {spec.logo && <img src={spec.logo} alt="" className="w-9 h-9 object-contain rounded-md border border-line" />}
                  <label className="inline-flex items-center gap-1.5 text-xs text-primary cursor-pointer hover:brightness-110">
                    <Upload className="w-3.5 h-3.5" /> {spec.logo ? 'Replace' : 'Upload'}
                    <input type="file" accept="image/svg+xml,image/png" className="hidden" onChange={(e) => {
                      const file = e.target.files?.[0]
                      if (!file) return
                      if (file.size > 200_000) { setError('Logo must be under 200 KB'); return }
                      const reader = new FileReader()
                      reader.onload = () => patch({ logo: String(reader.result) })
                      reader.readAsDataURL(file)
                    }} />
                  </label>
                  {spec.logo && <button type="button" className="text-xs text-fg-muted hover:text-danger" onClick={() => patch({ logo: null })}>Remove</button>}
                </div>
              </Field>
            </div>
            <Checkbox label="Mark as beta" checked={!!spec.beta} onChange={(v) => patch({ beta: v || undefined })} help="Shows a beta tag in the provider picker." />
            <div className="rounded-lg bg-surface-raised/60 p-3 text-xs text-fg-muted space-y-1">
              <div className="font-semibold text-fg">Where credentials go</div>
              {egressHosts.length ? (
                <p>API calls in this descriptor send credential material to: <span className="font-mono text-fg">{egressHosts.join(', ')}</span>. Only public HTTPS hosts are allowed; you will confirm this list when saving.</p>
              ) : <p>This descriptor makes no vendor API calls; credentials are only used to build proxy endpoints.</p>}
            </div>
          </div>
        )}
        {tab === 'credential' && <FieldListEditor spec={spec} scope="credential" fields={spec.credential_fields ?? []} onChange={(f) => patch({ credential_fields: f })} />}
        {tab === 'connector' && <FieldListEditor spec={spec} scope="connector" fields={spec.connector_fields ?? []} onChange={(f) => patch({ connector_fields: f })} />}
        {tab === 'proxy_types' && <ProxyTypeEditor spec={spec} onChange={patch} />}
        {tab === 'discovery' && <DiscoveryEditor spec={spec} onChange={patch} />}
        {tab === 'test' && <TestPanel key={JSON.stringify(spec.credential_fields) + JSON.stringify(spec.connector_fields)} spec={spec} presets={presets} />}
        {tab === 'yaml' && (
          <YamlTab
            spec={spec}
            validation={validation}
            onCheck={() => validateMutation.mutate()}
            draft={yamlDraft}
            setDraft={setYamlDraft}
            onImport={(yaml) => { setYamlDraft(yaml); importMutation.mutate(yaml) }}
            importing={importMutation.isPending}
            isEdit={isEdit}
          />
        )}
      </Inspector>

      {pendingHosts && (
        <ConfirmDialog
          title="Confirm where credentials are sent"
          message={<>This provider will send credential material (API keys, passwords) to the hosts below whenever it validates a credential, loads options or provisions proxies. Only continue if you trust these hosts.</>}
          confirmLabel={isEdit ? 'Confirm and save' : 'Confirm and create'}
          danger={false}
          onCancel={() => setPendingHosts(null)}
          onConfirm={() => saveMutation.mutate(pendingHosts)}
          isLoading={saveMutation.isPending}
          confirmDisabled={!hostsAcknowledged}
        >
          <ul className="space-y-1 mb-3">
            {pendingHosts.map((h) => <li key={h} className="font-mono text-xs bg-surface-raised rounded px-2 py-1">{h}</li>)}
          </ul>
          <Checkbox label="I understand credentials will be sent to these hosts" checked={hostsAcknowledged} onChange={setHostsAcknowledged} />
        </ConfirmDialog>
      )}
    </>
  )
}

function YamlTab({ spec, validation, onCheck, draft, setDraft, onImport, importing, isEdit }: {
  spec: ProviderSpec; validation: ProviderValidateResponse | null; onCheck: () => void; draft: string | null; setDraft: (v: string | null) => void; onImport: (yaml: string) => void; importing: boolean; isEdit: boolean
}) {
  const [mode, setMode] = useState<'view' | 'edit'>('view')
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-fg-muted">{mode === 'view' ? 'Normalised YAML of the current form. Run “Check” to refresh it, or paste YAML to import.' : 'Paste a descriptor. Importing creates it (or replaces this provider when editing) after host confirmation.'}</p>
        <div className="flex items-center gap-2">
          <Badge color="gray" className="py-0 text-[10px]">{mode}</Badge>
          <button type="button" className="text-xs text-primary hover:brightness-110" onClick={() => setMode(mode === 'view' ? 'edit' : 'view')}>{mode === 'view' ? 'Paste YAML' : 'Back to preview'}</button>
        </div>
      </div>
      {mode === 'view' ? (
        <>
          <pre className="text-[11px] font-mono bg-surface-raised rounded-lg p-3 overflow-auto max-h-[480px] whitespace-pre">{validation?.yaml ?? JSON.stringify(spec, null, 2)}</pre>
          {!validation?.yaml && <Button type="button" variant="outline" size="sm" onClick={onCheck}>Generate YAML</Button>}
        </>
      ) : (
        <>
          <Textarea rows={18} value={draft ?? ''} onChange={(e) => setDraft(e.target.value)} className={`${inputSm} font-mono text-xs`} placeholder={'id: acme\nname: Acme\n...'} />
          <Button type="button" size="sm" onClick={() => draft && onImport(draft)} disabled={!draft?.trim() || importing}>{importing ? 'Importing…' : isEdit ? 'Replace with this YAML' : 'Import as new provider'}</Button>
        </>
      )}
    </div>
  )
}
