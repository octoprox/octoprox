// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Plus } from 'lucide-react'
import { Badge } from '../ui'
import { Checkbox, Collapsible, HttpCallEditor, KeyValueEditor, ListField, NumberField, SelectField, Spec, TemplateEditor, TextField, ValueSourceInput, fieldPathsOf, slugify } from './editors'

const MODES = [
  { value: 'session', label: 'Session — gateway + fresh session id per slot' },
  { value: 'port', label: 'Port — one exit IP per slot (gateway port or pinned IP)' },
  { value: 'list', label: 'List — the vendor API returns host:port entries' },
]

/** Edits `proxy_types`, `proxy_type_field` and `session_id`. */
export function ProxyTypeEditor({ spec, onChange }: { spec: Spec; onChange: (patch: Spec) => void }) {
  const types: Spec[] = spec.proxy_types ?? []
  const [open, setOpen] = useState<number | null>(types.length ? null : 0)
  const authNames = Object.keys(spec.auth ?? {})
  const slotPaths = ['session_id', 'index', 'port', 'discovered_ip']
  const fieldPaths = fieldPathsOf(spec, slotPaths)
  const selectorCandidates = [
    ...(spec.credential_fields ?? []).filter((f: Spec) => f.type === 'select').map((f: Spec) => `credential.${f.key}`),
    ...(spec.connector_fields ?? []).filter((f: Spec) => f.type === 'select').map((f: Spec) => `connector.${f.key}`),
  ]
  const setTypes = (next: Spec[]) => onChange({ proxy_types: next })
  const update = (i: number, patch: Spec) => { const n = [...types]; n[i] = { ...types[i], ...patch }; setTypes(n) }
  const sessionId: Spec = spec.session_id ?? {}

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3">
        <SelectField
          label="Field that selects the proxy type"
          value={spec.proxy_type_field ?? ''}
          onChange={(v) => onChange({ proxy_type_field: v || null })}
          options={selectorCandidates.map((p) => ({ value: p, label: p }))}
          allowEmpty={types.length > 1 ? 'Choose a select field…' : 'Not needed (single proxy type)'}
          help="A select field whose option values match the proxy type keys below."
        />
        <div className="grid grid-cols-3 gap-2">
          <NumberField label="Session id length" value={sessionId.length ?? 12} onChange={(v) => onChange({ session_id: { ...sessionId, length: v ?? 12 } })} min={1} />
          <SelectField label="Alphabet" value={sessionId.alphabet ?? 'lower_digits'} onChange={(v) => onChange({ session_id: { ...sessionId, alphabet: v || 'lower_digits' } })} options={[{ value: 'lower_digits', label: 'a-z 0-9' }, { value: 'digits', label: '0-9' }, { value: 'alnum', label: 'A-Z a-z 0-9' }, { value: 'lower', label: 'a-z' }]} />
          <TextField label="Prefix" value={sessionId.prefix ?? ''} onChange={(v) => onChange({ session_id: { ...sessionId, prefix: v } })} mono placeholder="glob_" />
        </div>
      </div>

      {types.map((t, i) => (
        <Collapsible
          key={i}
          title={t.label || t.key || 'New proxy type'}
          subtitle={t.key ? <code className="font-mono">{t.key}</code> : undefined}
          badge={<Badge color="blue" className="py-0 text-[10px]">{t.mode ?? 'session'}</Badge>}
          open={open === i}
          onToggle={() => setOpen(open === i ? null : i)}
          onRemove={() => { setTypes(types.filter((_, j) => j !== i)); setOpen(null) }}
        >
          <div className="grid grid-cols-2 gap-3">
            <TextField label="Label" value={t.label} onChange={(v) => update(i, { label: v, key: t.key || slugify(v) })} placeholder="Residential" required />
            <TextField label="Key" value={t.key} onChange={(v) => update(i, { key: slugify(v) })} mono placeholder="residential" required help="Must equal one option value of the selector field." />
            <SelectField label="Mode" value={t.mode ?? 'session'} onChange={(v) => update(i, { mode: v || 'session', ...(v === 'list' ? { host: undefined, port: undefined, discovery: undefined, known_ips: undefined } : { list: undefined }) })} options={MODES} className="col-span-2" />
          </div>

          {t.mode !== 'list' && (
            <>
              <div className="grid grid-cols-[1fr_120px_120px] gap-3">
                <TextField label="Gateway host" value={typeof t.host === 'string' ? t.host : ''} onChange={(v) => update(i, { host: v })} mono placeholder="pr.oxylabs.io" help="May be a template, e.g. {connector.entry_node|or:geo.iproyal.com}" />
                <NumberField label={t.mode === 'port' && t.port_strategy !== 'fixed' ? 'First port' : 'Port'} value={t.port} onChange={(v) => update(i, { port: v })} min={1} />
                <SelectField label="Protocol" value={t.protocol ?? 'http'} onChange={(v) => update(i, { protocol: v || 'http' })} options={['http', 'https', 'socks5', 'socks4'].map((p) => ({ value: p, label: p }))} />
              </div>
              <TemplateEditor label="Username template" value={t.username} onChange={(v) => update(i, { username: v })} fieldPaths={fieldPaths} />
              <TemplateEditor label="Password template" value={t.password} onChange={(v) => update(i, { password: v })} fieldPaths={fieldPaths} help="Secret fields stay as {key} placeholders and are resolved per request; everything else is baked in." />
            </>
          )}

          {t.mode === 'port' && (
            <div className="rounded-lg bg-surface-raised/50 p-2.5 space-y-3">
              <SelectField
                label="Port strategy"
                value={t.port_strategy ?? 'sequential'}
                onChange={(v) => update(i, { port_strategy: v || 'sequential' })}
                options={[{ value: 'sequential', label: 'Sequential ports — slot n uses first port + n (Oxylabs style)' }, { value: 'fixed', label: 'Fixed port — pin the exit IP with {discovered_ip} in the username (Bright Data style)' }]}
              />
              <div className="grid grid-cols-2 gap-3">
                <TextField label="IP discovery URL (requested through the proxy)" value={t.discovery?.url ?? 'https://httpbin.org/ip'} onChange={(v) => update(i, { discovery: { ...(t.discovery ?? {}), url: v } })} mono />
                <TextField label="JMESPath to the IP (or @text)" value={t.discovery?.ip_path ?? 'origin'} onChange={(v) => update(i, { discovery: { ...(t.discovery ?? {}), ip_path: v } })} mono />
                <NumberField label="Stop after N failed slots" value={t.discovery?.max_consecutive_failures ?? 3} onChange={(v) => update(i, { discovery: { ...(t.discovery ?? {}), max_consecutive_failures: v ?? 3 } })} min={1} />
                <NumberField label={t.port_strategy === 'fixed' ? 'Retries per slot on duplicate IP' : 'Stop after N duplicate ports'} value={t.port_strategy === 'fixed' ? (t.discovery?.max_retries_per_slot ?? 3) : (t.discovery?.max_consecutive_duplicates ?? 3)} onChange={(v) => update(i, { discovery: { ...(t.discovery ?? {}), [t.port_strategy === 'fixed' ? 'max_retries_per_slot' : 'max_consecutive_duplicates']: v ?? 3 } })} min={1} />
              </div>
              <Checkbox label="The vendor API lists the account's exit IPs" checked={!!t.known_ips} onChange={(on) => update(i, { known_ips: on ? { call: { method: 'GET', url: '' }, items: '@', ip: 'ip' } : undefined })} help="Preferred over per-slot discovery when available; discovery remains the fallback." />
              {t.known_ips && (
                <>
                  <HttpCallEditor value={t.known_ips.call} onChange={(c) => update(i, { known_ips: { ...t.known_ips, call: c } })} authNames={authNames} title="Known IPs request" />
                  <div className="grid grid-cols-3 gap-3">
                    <TextField label="Items (JMESPath)" value={t.known_ips.items ?? '@'} onChange={(v) => update(i, { known_ips: { ...t.known_ips, items: v } })} mono />
                    <ValueSourceInput label="IP" value={t.known_ips.ip} onChange={(v) => update(i, { known_ips: { ...t.known_ips, ip: v ?? 'ip' } })} />
                    <ValueSourceInput label="Country" value={t.known_ips.country} onChange={(v) => update(i, { known_ips: { ...t.known_ips, country: v } })} />
                  </div>
                </>
              )}
            </div>
          )}

          {t.mode === 'list' && (
            <div className="rounded-lg bg-surface-raised/50 p-2.5 space-y-3">
              <HttpCallEditor value={t.list?.call} onChange={(c) => update(i, { list: { ...(t.list ?? {}), call: c } })} authNames={authNames} title="Proxy list request" />
              <div className="grid grid-cols-3 gap-3">
                <TextField label="Items (JMESPath)" value={t.list?.items ?? '@'} onChange={(v) => update(i, { list: { ...(t.list ?? {}), items: v } })} mono placeholder="results" />
                <ValueSourceInput label="Host" value={t.list?.host} onChange={(v) => update(i, { list: { ...(t.list ?? {}), host: v } })} placeholder="proxy_address" />
                <ValueSourceInput label="Port" value={t.list?.port} onChange={(v) => update(i, { list: { ...(t.list ?? {}), port: v } })} placeholder="port" />
                <ValueSourceInput label="Username" value={t.list?.username} onChange={(v) => update(i, { list: { ...(t.list ?? {}), username: v } })} />
                <ValueSourceInput label="Password" value={t.list?.password} onChange={(v) => update(i, { list: { ...(t.list ?? {}), password: v } })} />
                <ValueSourceInput label="Protocol" value={t.list?.protocol} onChange={(v) => update(i, { list: { ...(t.list ?? {}), protocol: v } })} />
                <ValueSourceInput label="Country" value={t.list?.country} onChange={(v) => update(i, { list: { ...(t.list ?? {}), country: v } })} />
                <ValueSourceInput label="Stable identity" value={t.list?.identity} onChange={(v) => update(i, { list: { ...(t.list ?? {}), identity: v } })} placeholder="id (defaults to host:port)" />
                <TextField label="Keep only when (JMESPath)" value={t.list?.filter ?? ''} onChange={(v) => update(i, { list: { ...(t.list ?? {}), filter: v || undefined } })} mono placeholder="valid" />
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <TextField label="Slot count field" value={t.count_field ?? 'connector.num_proxies'} onChange={(v) => update(i, { count_field: v || 'connector.num_proxies' })} mono help={t.mode === 'list' ? 'Optional cap on how many listed proxies to mirror.' : 'Variable holding the number of proxies to create.'} />
            <ListField label="Tags" value={t.tags} onChange={(v) => update(i, { tags: v })} placeholder="vendor, residential" />
            <TextField label="Healthcheck URL override" value={t.healthcheck_url ?? ''} onChange={(v) => update(i, { healthcheck_url: v || null })} mono className="col-span-2" placeholder="https://httpbin.org/ip" />
          </div>
          <KeyValueEditor label="Metadata (templates, shown in the proxy inspector)" value={t.metadata} onChange={(v) => update(i, { metadata: Object.keys(v).length ? v : undefined })} keyPlaceholder="country_code" valuePlaceholder="{connector.country_code}" />
        </Collapsible>
      ))}
      <button type="button" onClick={() => { setTypes([...types, { key: '', label: '', mode: 'session', protocol: 'http', tags: [] }]); setOpen(types.length) }} className="inline-flex items-center gap-1 text-xs text-primary hover:brightness-110"><Plus className="w-3.5 h-3.5" /> Add proxy type</button>
    </div>
  )
}
