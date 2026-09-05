// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Small building blocks for the provider builder. They edit fragments of the
 * descriptor document (a plain object mirroring the YAML schema) immutably
 * and know nothing about where in the document they live.
 */

import { type ReactNode, useState } from 'react'
import { ChevronDown, ChevronRight, Plus, Trash2 } from 'lucide-react'
import { Input, Label, Select, Textarea } from '../ui'
import { cn } from '../../utils/cn'

export type Spec = Record<string, any>

export const TEMPLATE_HELP = 'Templates: {credential.key}, {connector.key}, {session_id}, {index}, {port}, {discovered_ip}, {auth.token}, {item.key}; filters |lower |upper |urlencode |or:fallback'

export function Field({ label, help, children, className }: { label: ReactNode; help?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={className}>
      <Label className="text-xs">{label}</Label>
      {children}
      {help && <p className="text-[11px] text-fg-subtle mt-1">{help}</p>}
    </div>
  )
}

export const inputSm = 'px-3 py-1.5 text-sm'

export function TextField({ label, value, onChange, placeholder, help, mono, type = 'text', className, required }: {
  label: ReactNode; value: string | number | null | undefined; onChange: (v: string) => void; placeholder?: string; help?: ReactNode; mono?: boolean; type?: string; className?: string; required?: boolean
}) {
  return (
    <Field label={label} help={help} className={className}>
      <Input type={type} value={value ?? ''} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className={cn(inputSm, mono && 'font-mono text-xs')} required={required} />
    </Field>
  )
}

export function NumberField({ label, value, onChange, help, className, min }: { label: ReactNode; value: number | null | undefined; onChange: (v: number | null) => void; help?: ReactNode; className?: string; min?: number }) {
  return (
    <Field label={label} help={help} className={className}>
      <Input type="number" min={min} value={value ?? ''} onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))} className={inputSm} />
    </Field>
  )
}

export function SelectField<T extends string>({ label, value, onChange, options, help, className, allowEmpty }: {
  label: ReactNode; value: T | '' | null | undefined; onChange: (v: T | '') => void; options: { value: T; label: string }[]; help?: ReactNode; className?: string; allowEmpty?: string
}) {
  return (
    <Field label={label} help={help} className={className}>
      <Select value={value ?? ''} onChange={(e) => onChange(e.target.value as T | '')} className={inputSm}>
        {allowEmpty !== undefined && <option value="">{allowEmpty}</option>}
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </Select>
    </Field>
  )
}

export function Checkbox({ label, checked, onChange, help }: { label: ReactNode; checked: boolean; onChange: (v: boolean) => void; help?: ReactNode }) {
  return (
    <label className="flex items-start gap-2 text-[13px] cursor-pointer select-none py-1">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="w-4 h-4 mt-0.5" />
      <span>
        {label}
        {help && <span className="block text-[11px] text-fg-subtle font-normal">{help}</span>}
      </span>
    </label>
  )
}

/** Editable string→string map (headers, params, metadata templates, fill, capture). */
export function KeyValueEditor({ label, value, onChange, keyPlaceholder = 'key', valuePlaceholder = 'value', help, mono = true }: {
  label?: ReactNode; value: Record<string, string> | undefined; onChange: (v: Record<string, string>) => void; keyPlaceholder?: string; valuePlaceholder?: string; help?: ReactNode; mono?: boolean
}) {
  const entries = Object.entries(value ?? {})
  const [draft, setDraft] = useState<[string, string][]>(entries)
  // Keep local order stable while typing keys; commit as a map.
  const commit = (next: [string, string][]) => {
    setDraft(next)
    const map: Record<string, string> = {}
    for (const [k, v] of next) if (k.trim()) map[k.trim()] = v
    onChange(map)
  }
  return (
    <div>
      {label && <Label className="text-xs">{label}</Label>}
      <div className="space-y-1.5">
        {draft.map(([k, v], i) => (
          <div key={i} className="flex gap-1.5 items-center">
            <Input value={k} onChange={(e) => { const n = [...draft]; n[i] = [e.target.value, v]; commit(n) }} placeholder={keyPlaceholder} className={cn('px-2.5 py-1 text-xs flex-[0_0_38%]', mono && 'font-mono')} />
            <Input value={v} onChange={(e) => { const n = [...draft]; n[i] = [k, e.target.value]; commit(n) }} placeholder={valuePlaceholder} className={cn('px-2.5 py-1 text-xs flex-1', mono && 'font-mono')} />
            <button type="button" onClick={() => commit(draft.filter((_, j) => j !== i))} className="p-1 text-fg-subtle hover:text-danger" title="Remove"><Trash2 className="w-3.5 h-3.5" /></button>
          </div>
        ))}
        <button type="button" onClick={() => commit([...draft, ['', '']])} className="inline-flex items-center gap-1 text-xs text-primary hover:brightness-110"><Plus className="w-3.5 h-3.5" /> Add</button>
      </div>
      {help && <p className="text-[11px] text-fg-subtle mt-1">{help}</p>}
    </div>
  )
}

/** Comma-separated list as a single input. */
export function ListField({ label, value, onChange, help, placeholder }: { label: ReactNode; value: string[] | undefined; onChange: (v: string[]) => void; help?: ReactNode; placeholder?: string }) {
  return (
    <Field label={label} help={help}>
      <Input value={(value ?? []).join(', ')} onChange={(e) => onChange(e.target.value.split(',').map((s) => s.trim()).filter(Boolean))} placeholder={placeholder} className={cn(inputSm, 'font-mono text-xs')} />
    </Field>
  )
}

/** `show_when` / template part conditions. */
export function ConditionEditor({ label, value, onChange, fieldPaths }: { label: ReactNode; value: Spec | null | undefined; onChange: (v: Spec | null) => void; fieldPaths: string[] }) {
  const enabled = !!value
  const op: 'truthy' | 'equals' | 'in' = value?.equals != null ? 'equals' : value?.in ? 'in' : 'truthy'
  const set = (patch: Spec) => onChange({ field: value?.field ?? fieldPaths[0] ?? '', ...value, ...patch })
  return (
    <div className="rounded-lg border border-line p-2.5 space-y-2">
      <Checkbox label={label} checked={enabled} onChange={(on) => onChange(on ? { field: fieldPaths[0] ?? '' } : null)} />
      {enabled && (
        <div className="grid grid-cols-[1fr_auto_1fr_auto] gap-1.5 items-end">
          <Field label="Variable">
            <Input list="condition-field-paths" value={value?.field ?? ''} onChange={(e) => set({ field: e.target.value })} className="px-2.5 py-1 text-xs font-mono" placeholder="connector.country_code" />
            <datalist id="condition-field-paths">{fieldPaths.map((p) => <option key={p} value={p} />)}</datalist>
          </Field>
          <Field label="Is">
            <Select value={op} onChange={(e) => {
              const next = e.target.value
              const base: Spec = { field: value?.field ?? '', negate: value?.negate }
              onChange(next === 'equals' ? { ...base, equals: '' } : next === 'in' ? { ...base, in: [] } : base)
            }} className="px-2 py-1 text-xs">
              <option value="truthy">set</option>
              <option value="equals">equal to</option>
              <option value="in">one of</option>
            </Select>
          </Field>
          <Field label={op === 'in' ? 'Values (comma-separated)' : 'Value'}>
            {op === 'truthy' ? <Input disabled value="" className="px-2.5 py-1 text-xs" /> : op === 'equals'
              ? <Input value={value?.equals ?? ''} onChange={(e) => set({ equals: e.target.value })} className="px-2.5 py-1 text-xs font-mono" />
              : <Input value={(value?.in ?? []).join(', ')} onChange={(e) => set({ in: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} className="px-2.5 py-1 text-xs font-mono" />}
          </Field>
          <Checkbox label="not" checked={!!value?.negate} onChange={(v) => set({ negate: v || undefined })} />
        </div>
      )}
    </div>
  )
}

/** Username/password/host templates: a plain string or `{separator, parts[{text, when}]}`. */
export function TemplateEditor({ label, value, onChange, fieldPaths, help }: { label: ReactNode; value: string | Spec | null | undefined; onChange: (v: string | Spec | null) => void; fieldPaths: string[]; help?: ReactNode }) {
  const composed = value != null && typeof value === 'object'
  const parts: Spec[] = composed ? (value.parts ?? []) : []
  const separator: string = composed ? (value.separator ?? '-') : '-'
  const setParts = (next: Spec[]) => onChange({ separator, parts: next })
  // Remember the form we left so toggling back and forth is lossless.
  const [savedComposed, setSavedComposed] = useState<Spec | null>(null)
  const [savedSingle, setSavedSingle] = useState<string | null>(null)
  const toggle = () => {
    if (composed) {
      setSavedComposed(value)
      onChange(savedSingle ?? parts.map((p) => p.text).join(separator))
    } else {
      setSavedSingle(typeof value === 'string' ? value : '')
      onChange(savedComposed ?? { separator: '-', parts: [{ text: typeof value === 'string' ? value : '' }] })
    }
  }
  return (
    <div className="rounded-lg border border-line p-2.5 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label className="text-xs mb-0">{label}</Label>
        <button type="button" className="text-[11px] text-primary hover:brightness-110" onClick={toggle}>
          {composed ? 'Use a single template' : savedComposed ? 'Back to conditional parts' : 'Split into conditional parts'}
        </button>
      </div>
      {!composed ? (
        <Input value={typeof value === 'string' ? value : ''} onChange={(e) => onChange(e.target.value)} className={cn(inputSm, 'font-mono text-xs')} placeholder="customer-{credential.username}-sessid-{session_id}" />
      ) : (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs">
            <span className="text-fg-muted">Join parts with</span>
            <Input value={separator} onChange={(e) => onChange({ separator: e.target.value, parts })} className="px-2 py-1 text-xs font-mono w-16" />
          </div>
          {parts.map((part, i) => (
            <div key={i} className="rounded-md bg-surface-raised/60 p-2 space-y-1.5">
              <div className="flex gap-1.5">
                <Input value={part.text ?? ''} onChange={(e) => { const n = [...parts]; n[i] = { ...part, text: e.target.value }; setParts(n) }} className="px-2.5 py-1 text-xs font-mono flex-1" placeholder="cc-{connector.country_code}" />
                <button type="button" onClick={() => setParts(parts.filter((_, j) => j !== i))} className="p-1 text-fg-subtle hover:text-danger"><Trash2 className="w-3.5 h-3.5" /></button>
              </div>
              <ConditionEditor label={<span className="text-xs">Only include when…</span>} value={part.when} onChange={(w) => { const n = [...parts]; n[i] = w ? { ...part, when: w } : { text: part.text }; setParts(n) }} fieldPaths={fieldPaths} />
            </div>
          ))}
          <button type="button" onClick={() => setParts([...parts, { text: '' }])} className="inline-flex items-center gap-1 text-xs text-primary hover:brightness-110"><Plus className="w-3.5 h-3.5" /> Add part</button>
        </div>
      )}
      <p className="text-[11px] text-fg-subtle">{help ?? TEMPLATE_HELP}</p>
    </div>
  )
}

/** JMESPath input; a "map" toggle exposes the {path, map[], default} form. */
export function ValueSourceInput({ label, value, onChange, help, placeholder }: { label: ReactNode; value: string | Spec | null | undefined; onChange: (v: string | Spec | null) => void; help?: ReactNode; placeholder?: string }) {
  const mapped = value != null && typeof value === 'object'
  return (
    <div>
      <div className="flex items-center justify-between">
        <Label className="text-xs">{label}</Label>
        <button type="button" className="text-[11px] text-primary hover:brightness-110 mb-1" onClick={() => onChange(mapped ? (value.path ?? '') : { path: typeof value === 'string' ? value : '', map: [], default: null })}>
          {mapped ? 'Plain JMESPath' : 'Map values'}
        </button>
      </div>
      {!mapped ? (
        <Input value={typeof value === 'string' ? value : ''} onChange={(e) => onChange(e.target.value === '' ? null : e.target.value)} className={cn(inputSm, 'font-mono text-xs')} placeholder={placeholder ?? 'JMESPath, e.g. name or results[].id'} />
      ) : (
        <div className="rounded-lg border border-line p-2.5 space-y-2">
          <TextField label="Path" value={value.path} onChange={(v) => onChange({ ...value, path: v })} mono placeholder="type" />
          <div className="space-y-1.5">
            <Label className="text-xs">Rules (first match wins)</Label>
            {(value.map ?? []).map((rule: Spec, i: number) => {
              const kind: 'equals' | 'starts_with' | 'regex' = rule.starts_with != null ? 'starts_with' : rule.regex != null ? 'regex' : 'equals'
              const setRule = (next: Spec) => { const n = [...value.map]; n[i] = next; onChange({ ...value, map: n }) }
              return (
                <div key={i} className="flex gap-1.5 items-center">
                  <Select value={kind} onChange={(e) => setRule({ [e.target.value]: rule[kind] ?? '', to: rule.to ?? '' })} className="px-2 py-1 text-xs w-28">
                    <option value="equals">equals</option><option value="starts_with">starts with</option><option value="regex">regex</option>
                  </Select>
                  <Input value={rule[kind] ?? ''} onChange={(e) => setRule({ [kind]: e.target.value, to: rule.to ?? '' })} className="px-2.5 py-1 text-xs font-mono flex-1" placeholder="res" />
                  <span className="text-xs text-fg-subtle">→</span>
                  <Input value={rule.to ?? ''} onChange={(e) => setRule({ [kind]: rule[kind] ?? '', to: e.target.value })} className="px-2.5 py-1 text-xs font-mono flex-1" placeholder="residential" />
                  <button type="button" onClick={() => onChange({ ...value, map: value.map.filter((_: Spec, j: number) => j !== i) })} className="p-1 text-fg-subtle hover:text-danger"><Trash2 className="w-3.5 h-3.5" /></button>
                </div>
              )
            })}
            <button type="button" onClick={() => onChange({ ...value, map: [...(value.map ?? []), { equals: '', to: '' }] })} className="inline-flex items-center gap-1 text-xs text-primary hover:brightness-110"><Plus className="w-3.5 h-3.5" /> Add rule</button>
          </div>
          <TextField label="Default when nothing matches" value={value.default ?? ''} onChange={(v) => onChange({ ...value, default: v || null })} mono />
        </div>
      )}
      {help && <p className="text-[11px] text-fg-subtle mt-1">{help}</p>}
    </div>
  )
}

/** One declarative HTTP request. */
export function HttpCallEditor({ value, onChange, authNames, title }: { value: Spec | undefined; onChange: (v: Spec) => void; authNames: string[]; title?: ReactNode }) {
  const call: Spec = value ?? { method: 'GET', url: '' }
  const set = (patch: Spec) => onChange({ ...call, ...patch })
  const [bodyText, setBodyText] = useState(call.body ? JSON.stringify(call.body, null, 2) : '')
  const [bodyError, setBodyError] = useState<string | null>(null)
  return (
    <div className="rounded-lg border border-line p-3 space-y-3">
      {title && <div className="text-xs font-semibold text-fg">{title}</div>}
      <div className="grid grid-cols-[110px_1fr] gap-2">
        <SelectField label="Method" value={call.method ?? 'GET'} onChange={(v) => set({ method: v || 'GET' })} options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => ({ value: m, label: m }))} />
        <TextField label="URL" value={call.url} onChange={(v) => set({ url: v })} mono placeholder="https://api.vendor.com/v1/resource" required />
      </div>
      <KeyValueEditor label="Headers" value={call.headers} onChange={(v) => set({ headers: Object.keys(v).length ? v : undefined })} keyPlaceholder="Authorization" valuePlaceholder="Bearer {credential.token}" />
      <KeyValueEditor label="Query parameters" value={call.params} onChange={(v) => set({ params: Object.keys(v).length ? v : undefined })} keyPlaceholder="zone" valuePlaceholder="{connector.zone_name}" help="Parameters whose rendered value is empty are dropped, which is how optional filters work." />
      <div className="grid grid-cols-2 gap-2">
        <SelectField label="Authenticate first with" value={call.auth ?? ''} onChange={(v) => set({ auth: v || undefined })} options={authNames.map((n) => ({ value: n, label: n }))} allowEmpty="No auth flow" help="Two-step login flows are defined under Discovery → Auth flows." />
        <TextField label="Follow pagination (JMESPath to next URL)" value={call.paginate?.next_url ?? ''} onChange={(v) => set({ paginate: v ? { next_url: v } : undefined })} mono placeholder="next" />
      </div>
      <Field label="JSON body (POST/PUT)" help={bodyError ?? 'String values inside the body are templates.'}>
        <Textarea rows={3} value={bodyText} onChange={(e) => {
          setBodyText(e.target.value)
          if (!e.target.value.trim()) { setBodyError(null); set({ body: undefined }); return }
          try { set({ body: JSON.parse(e.target.value) }); setBodyError(null) } catch { setBodyError('Body is not valid JSON') }
        }} className={cn('px-3 py-1.5 font-mono text-xs', bodyError && 'border-danger/50')} placeholder='{"email": "{credential.email}"}' />
      </Field>
    </div>
  )
}

/** Collapsible list row used for fields, proxy types, options sources. */
export function Collapsible({ title, subtitle, open, onToggle, onRemove, children, badge }: { title: ReactNode; subtitle?: ReactNode; open: boolean; onToggle: () => void; onRemove?: () => void; children: ReactNode; badge?: ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-surface">
      <div className="flex items-center gap-2 px-3 py-2">
        <button type="button" onClick={onToggle} className="flex items-center gap-2 flex-1 min-w-0 text-left">
          {open ? <ChevronDown className="w-4 h-4 text-fg-subtle flex-none" /> : <ChevronRight className="w-4 h-4 text-fg-subtle flex-none" />}
          <span className="text-[13px] font-medium truncate">{title}</span>
          {badge}
          {subtitle && <span className="text-xs text-fg-muted truncate">{subtitle}</span>}
        </button>
        {onRemove && <button type="button" onClick={onRemove} className="p-1 text-fg-subtle hover:text-danger" title="Remove"><Trash2 className="w-4 h-4" /></button>}
      </div>
      {open && <div className="px-3 pb-3 pt-1 border-t border-line space-y-3">{children}</div>}
    </div>
  )
}

export const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40)

/** Variable paths a template may reference, from the declared fields. */
export function fieldPathsOf(spec: Spec, extra: string[] = []): string[] {
  const paths: string[] = []
  for (const f of spec.credential_fields ?? []) if (f.key) paths.push(`credential.${f.key}`)
  for (const key of Object.keys(spec.validation?.capture ?? {})) paths.push(`credential.${key}`)
  for (const f of spec.connector_fields ?? []) if (f.key) paths.push(`connector.${f.key}`)
  return [...paths, ...extra]
}
