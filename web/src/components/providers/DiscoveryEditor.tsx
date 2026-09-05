// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Plus } from 'lucide-react'
import { InspectorSection } from '../ui'
import { Checkbox, Collapsible, ConditionEditor, HttpCallEditor, KeyValueEditor, NumberField, Spec, TextField, ValueSourceInput, fieldPathsOf, slugify } from './editors'

/** Edits `validation`, `options` and `auth`. */
export function DiscoveryEditor({ spec, onChange }: { spec: Spec; onChange: (patch: Spec) => void }) {
  const authNames = Object.keys(spec.auth ?? {})
  const fieldPaths = fieldPathsOf(spec)
  return (
    <div className="space-y-6">
      <InspectorSection title="Credential validation">
        <p className="text-xs text-fg-muted">Run when a credential is saved. Values captured from the response (e.g. a customer id) are stored on the credential and usable as <code className="font-mono">{'{credential.<key>}'}</code>.</p>
        <Checkbox label="Validate credentials against the vendor API" checked={!!spec.validation} onChange={(on) => onChange({ validation: on ? { call: { method: 'GET', url: '' }, error_message: 'Credential validation failed' } : null })} />
        {spec.validation && (
          <div className="space-y-3">
            <HttpCallEditor value={spec.validation.call} onChange={(c) => onChange({ validation: { ...spec.validation, call: c } })} authNames={authNames} />
            <div className="grid grid-cols-2 gap-3">
              <TextField label="Success predicate (JMESPath, optional)" value={spec.validation.success ?? ''} onChange={(v) => onChange({ validation: { ...spec.validation, success: v || null } })} mono placeholder="status == 'active'" help="A 2xx response is enough when empty." />
              <TextField label="Error message" value={spec.validation.error_message} onChange={(v) => onChange({ validation: { ...spec.validation, error_message: v } })} />
            </div>
            <KeyValueEditor label="Capture into the credential" value={spec.validation.capture} onChange={(v) => onChange({ validation: { ...spec.validation, capture: Object.keys(v).length ? v : undefined } })} keyPlaceholder="customer_id" valuePlaceholder="customer (JMESPath)" />
            <ConditionEditor label="Only validate when…" value={spec.validation.when} onChange={(v) => onChange({ validation: { ...spec.validation, when: v } })} fieldPaths={fieldPaths} />
          </div>
        )}
      </InspectorSection>

      <OptionsSourcesEditor spec={spec} onChange={onChange} authNames={authNames} />
      <AuthFlowsEditor spec={spec} onChange={onChange} />
    </div>
  )
}

function OptionsSourcesEditor({ spec, onChange, authNames }: { spec: Spec; onChange: (patch: Spec) => void; authNames: string[] }) {
  const sources: Record<string, Spec> = spec.options ?? {}
  const names = Object.keys(sources)
  const [open, setOpen] = useState<string | null>(null)
  const setSource = (name: string, value: Spec | null) => {
    const next = { ...sources }
    if (value === null) delete next[name]; else next[name] = value
    onChange({ options: Object.keys(next).length ? next : undefined })
  }
  const rename = (from: string, to: string) => {
    if (!to || to === from || sources[to]) return
    const next: Record<string, Spec> = {}
    for (const [k, v] of Object.entries(sources)) next[k === from ? to : k] = v
    onChange({ options: next })
    setOpen(to)
  }
  return (
    <InspectorSection title="Options sources">
      <p className="text-xs text-fg-muted">Dynamic select options fetched from the vendor: zones, sub-users, entry nodes. Reference them from a select field's "options come from" setting.</p>
      {names.map((name) => {
        const src = sources[name]
        const set = (patch: Spec) => setSource(name, { ...src, ...patch })
        return (
          <Collapsible key={name} title={name} subtitle={src.call?.url} open={open === name} onToggle={() => setOpen(open === name ? null : name)} onRemove={() => setSource(name, null)}>
            <TextField label="Name" value={name} onChange={(v) => rename(name, slugify(v))} mono />
            <HttpCallEditor value={src.call} onChange={(c) => set({ call: c })} authNames={authNames} />
            <div className="grid grid-cols-2 gap-3">
              <TextField label="Items (JMESPath)" value={src.items ?? '@'} onChange={(v) => set({ items: v })} mono />
              <ValueSourceInput label="Value" value={src.value} onChange={(v) => set({ value: v ?? '' })} placeholder="name" />
              <ValueSourceInput label="Label" value={src.label} onChange={(v) => set({ label: v })} />
              <ValueSourceInput label="Description" value={src.description} onChange={(v) => set({ description: v })} />
            </div>
            <ExtraEditor value={src.extra} onChange={(v) => set({ extra: Object.keys(v).length ? v : undefined })} />
            <EnrichEditor value={src.enrich ?? []} onChange={(v) => set({ enrich: v.length ? v : undefined })} authNames={authNames} />
            <div className="grid grid-cols-2 gap-3">
              <TextField label="Keep only when (JMESPath over the option)" value={src.filter ?? ''} onChange={(v) => set({ filter: v || null })} mono placeholder="proxy_type && password" />
              <NumberField label="Cache seconds" value={src.cache_seconds ?? 300} onChange={(v) => set({ cache_seconds: v ?? 0 })} min={0} />
            </div>
          </Collapsible>
        )
      })}
      <button type="button" onClick={() => { const name = names.includes('options') ? `options_${names.length + 1}` : 'options'; setSource(name, { call: { method: 'GET', url: '' }, items: '@', value: 'id', label: 'name' }); setOpen(name) }} className="inline-flex items-center gap-1 text-xs text-primary hover:brightness-110"><Plus className="w-3.5 h-3.5" /> Add options source</button>
    </InspectorSection>
  )
}

/** `extra`: name → JMESPath or mapping. Rendered as a list of ValueSourceInputs so mappings stay editable. */
function ExtraEditor({ value, onChange }: { value: Record<string, Spec | string> | undefined; onChange: (v: Record<string, Spec | string>) => void }) {
  const entries = Object.entries(value ?? {})
  const set = (next: [string, Spec | string][]) => { const map: Record<string, Spec | string> = {}; for (const [k, v] of next) if (k.trim()) map[k.trim()] = v; onChange(map) }
  return (
    <div className="space-y-2">
      <div className="text-xs font-medium text-fg-muted">Extra values per option (available to fill, filter and enrichment)</div>
      {entries.map(([k, v], i) => (
        <div key={i} className="grid grid-cols-[38%_1fr] gap-1.5 items-end">
          <TextField label="Extra key" value={k} onChange={(nk) => { const n = [...entries] as [string, Spec | string][]; n[i] = [nk, v]; set(n) }} mono placeholder="proxy_type" />
          <ValueSourceInput label="From" value={v} onChange={(nv) => { const n = [...entries] as [string, Spec | string][]; n[i] = [k, nv ?? '']; set(n) }} />
        </div>
      ))}
      <button type="button" onClick={() => set([...entries, ['', '']] as [string, Spec | string][])} className="inline-flex items-center gap-1 text-xs text-primary hover:brightness-110"><Plus className="w-3.5 h-3.5" /> Add extra</button>
    </div>
  )
}

function EnrichEditor({ value, onChange, authNames }: { value: Spec[]; onChange: (v: Spec[]) => void; authNames: string[] }) {
  return (
    <div className="space-y-2">
      <div className="text-xs font-medium text-fg-muted">Enrich each option with follow-up requests (use <code className="font-mono">{'{item.value}'}</code> etc.)</div>
      {value.map((e, i) => (
        <div key={i} className="rounded-lg border border-line p-2.5 space-y-2">
          <HttpCallEditor value={e.call} onChange={(c) => { const n = [...value]; n[i] = { ...e, call: c }; onChange(n) }} authNames={authNames} title={`Enrichment ${i + 1}`} />
          <TextField label="Only when (JMESPath over the option)" value={e.when ?? ''} onChange={(v) => { const n = [...value]; n[i] = { ...e, when: v || null }; onChange(n) }} mono placeholder="proxy_type == 'isp'" />
          <KeyValueEditor label="Merge into the option" value={e.merge} onChange={(v) => { const n = [...value]; n[i] = { ...e, merge: v }; onChange(n) }} keyPlaceholder="password" valuePlaceholder="password[0] (JMESPath)" />
          <button type="button" onClick={() => onChange(value.filter((_, j) => j !== i))} className="text-xs text-danger hover:brightness-110">Remove enrichment</button>
        </div>
      ))}
      <button type="button" onClick={() => onChange([...value, { call: { method: 'GET', url: '' }, merge: {} }])} className="inline-flex items-center gap-1 text-xs text-primary hover:brightness-110"><Plus className="w-3.5 h-3.5" /> Add enrichment</button>
    </div>
  )
}

function AuthFlowsEditor({ spec, onChange }: { spec: Spec; onChange: (patch: Spec) => void }) {
  const flows: Record<string, Spec> = spec.auth ?? {}
  const names = Object.keys(flows)
  const [open, setOpen] = useState<string | null>(null)
  const setFlow = (name: string, value: Spec | null) => {
    const next = { ...flows }
    if (value === null) delete next[name]; else next[name] = value
    onChange({ auth: Object.keys(next).length ? next : undefined })
  }
  return (
    <InspectorSection title="Auth flows">
      <p className="text-xs text-fg-muted">For vendors whose API needs a login call first (e.g. Basic auth → JWT). The token is cached and exposed as <code className="font-mono">{'{auth.token}'}</code> to calls that select the flow.</p>
      {names.map((name) => {
        const flow = flows[name]
        return (
          <Collapsible key={name} title={name} subtitle={flow.call?.url} open={open === name} onToggle={() => setOpen(open === name ? null : name)} onRemove={() => setFlow(name, null)}>
            <HttpCallEditor value={flow.call} onChange={(c) => setFlow(name, { ...flow, call: c })} authNames={[]} />
            <div className="grid grid-cols-2 gap-3">
              <TextField label="JMESPath to the token" value={flow.token_path ?? 'token'} onChange={(v) => setFlow(name, { ...flow, token_path: v })} mono />
              <NumberField label="Cache token for (seconds)" value={flow.ttl_seconds ?? 3000} onChange={(v) => setFlow(name, { ...flow, ttl_seconds: v ?? 3000 })} min={1} />
            </div>
          </Collapsible>
        )
      })}
      <button type="button" onClick={() => { const name = names.includes('login') ? `login_${names.length + 1}` : 'login'; setFlow(name, { call: { method: 'POST', url: '' }, token_path: 'token', ttl_seconds: 3000 }); setOpen(name) }} className="inline-flex items-center gap-1 text-xs text-primary hover:brightness-110"><Plus className="w-3.5 h-3.5" /> Add auth flow</button>
    </InspectorSection>
  )
}
