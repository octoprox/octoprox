// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from 'react'
import { Plus } from 'lucide-react'
import { Badge, Textarea } from '../ui'
import { Checkbox, Collapsible, ConditionEditor, Field, KeyValueEditor, NumberField, SelectField, Spec, TextField, fieldPathsOf, slugify } from './editors'

const FIELD_TYPES = ['text', 'password', 'number', 'select', 'boolean', 'textarea', 'url', 'country'] as const

/** Edits `credential_fields` or `connector_fields`. */
export function FieldListEditor({ spec, scope, fields, onChange }: { spec: Spec; scope: 'credential' | 'connector'; fields: Spec[]; onChange: (fields: Spec[]) => void }) {
  const [open, setOpen] = useState<number | null>(fields.length ? null : 0)
  const fieldPaths = fieldPathsOf(spec)
  const optionSources = Object.keys(spec.options ?? {})
  const update = (i: number, patch: Spec) => { const n = [...fields]; n[i] = { ...fields[i], ...patch }; onChange(n) }
  const add = () => { onChange([...fields, { key: '', label: '', type: 'text', required: scope === 'credential' }]); setOpen(fields.length) }

  return (
    <div className="space-y-2">
      <p className="text-xs text-fg-muted">
        {scope === 'credential'
          ? 'What a user enters once per account: API keys, proxy usernames and passwords. Mark secrets so they are never written into proxy rows.'
          : 'Per-connector settings: proxy counts, countries, zones. Selects can load their options from the vendor API.'}
      </p>
      {fields.map((f, i) => (
        <Collapsible
          key={i}
          title={f.label || f.key || 'New field'}
          subtitle={f.key ? <code className="font-mono">{f.key}</code> : undefined}
          badge={<span className="inline-flex gap-1">{f.secret && <Badge color="yellow" className="py-0 text-[10px]">secret</Badge>}{f.required && <Badge color="gray" className="py-0 text-[10px]">required</Badge>}</span>}
          open={open === i}
          onToggle={() => setOpen(open === i ? null : i)}
          onRemove={() => { onChange(fields.filter((_, j) => j !== i)); setOpen(null) }}
        >
          <div className="grid grid-cols-2 gap-3">
            <TextField label="Label" value={f.label} onChange={(v) => update(i, { label: v, key: f.key || slugify(v) })} placeholder="API token" required />
            <TextField label="Key" value={f.key} onChange={(v) => update(i, { key: slugify(v) })} mono placeholder="api_token" help={`Referenced as {${scope}.${f.key || 'key'}}`} required />
            <SelectField label="Type" value={f.type ?? 'text'} onChange={(v) => update(i, { type: v || 'text' })} options={FIELD_TYPES.map((t) => ({ value: t, label: t }))} />
            <TextField label="Group (connector tab)" value={f.group ?? 'general'} onChange={(v) => update(i, { group: slugify(v) || 'general' })} mono />
            <TextField label="Default" value={f.default == null ? '' : String(f.default)} onChange={(v) => update(i, { default: v === '' ? null : (f.type === 'number' && !Number.isNaN(Number(v)) ? Number(v) : f.type === 'boolean' ? v === 'true' : v) })} />
            <TextField label="Placeholder" value={f.placeholder} onChange={(v) => update(i, { placeholder: v || null })} />
            <TextField label="Help text" value={f.help} onChange={(v) => update(i, { help: v || null })} className="col-span-2" />
          </div>
          <div className="flex flex-wrap gap-x-6">
            <Checkbox label="Required" checked={!!f.required} onChange={(v) => update(i, { required: v })} />
            <Checkbox label="Secret" checked={!!f.secret} onChange={(v) => update(i, { secret: v })} help="Stored, but only ever substituted at request time." />
          </div>
          {(f.type === 'select' || f.type === 'country') && (
            <div className="rounded-lg bg-surface-raised/50 p-2.5 space-y-2">
              <SelectField
                label="Options come from"
                value={f.options_from ? 'remote' : f.options_preset ? 'preset' : 'static'}
                onChange={(v) => update(i, v === 'remote' ? { options: [], options_preset: null, options_from: optionSources[0] ?? '' } : v === 'preset' ? { options: [], options_preset: 'countries', options_from: null, fill: undefined } : { options_preset: null, options_from: null, fill: undefined })}
                options={[{ value: 'static', label: 'A fixed list' }, { value: 'preset', label: 'Built-in country list' }, { value: 'remote', label: 'The vendor API (options source)' }]}
              />
              {f.options_from != null && (
                <>
                  <SelectField label="Options source" value={f.options_from} onChange={(v) => update(i, { options_from: v })} options={optionSources.map((n) => ({ value: n, label: n }))} allowEmpty={optionSources.length ? undefined : 'Define one under Discovery first'} />
                  <KeyValueEditor label="When selected, fill other fields" value={f.fill} onChange={(v) => update(i, { fill: Object.keys(v).length ? v : undefined })} keyPlaceholder="zone_password (field key)" valuePlaceholder="password (option extra)" help="Left: connector field to set. Right: extra value from the option." />
                </>
              )}
              {!f.options_from && !f.options_preset && (
                <OptionsTextarea options={f.options ?? []} onChange={(options) => update(i, { options })} />
              )}
            </div>
          )}
          <div className="grid grid-cols-3 gap-3">
            <NumberField label={f.type === 'number' ? 'Min value' : 'Min length'} value={f.min} onChange={(v) => update(i, { min: v })} />
            <NumberField label={f.type === 'number' ? 'Max value' : 'Max length'} value={f.max} onChange={(v) => update(i, { max: v })} />
            <SelectField label="Transform" value={f.transform ?? ''} onChange={(v) => update(i, { transform: v || null })} options={[{ value: 'upper', label: 'UPPERCASE' }, { value: 'lower', label: 'lowercase' }, { value: 'strip', label: 'strip whitespace' }]} allowEmpty="none" />
            <TextField label="Pattern (regex)" value={f.pattern} onChange={(v) => update(i, { pattern: v || null })} mono className="col-span-3" placeholder="^[0-9]+[smh]$" />
          </div>
          <ConditionEditor label="Only show when…" value={f.show_when} onChange={(v) => update(i, { show_when: v })} fieldPaths={fieldPaths.filter((p) => !p.endsWith(`.${f.key}`))} />
        </Collapsible>
      ))}
      <button type="button" onClick={add} className="inline-flex items-center gap-1 text-xs text-primary hover:brightness-110"><Plus className="w-3.5 h-3.5" /> Add field</button>
    </div>
  )
}


const formatOptions = (options: Spec[]) =>
  options.map((o) => (o.description ? `${o.value} | ${o.label} | ${o.description}` : `${o.value} | ${o.label}`)).join('\n')

const parseOptions = (text: string): Spec[] =>
  text.split('\n').filter((l) => l.trim()).map((line) => {
    const [value, label, description] = line.split('|').map((s) => s.trim())
    return { value, label: label || value, ...(description ? { description } : {}) }
  })

/**
 * Static option list as "value | label | description" lines. Keeps its own raw
 * text so half-typed lines and blank lines survive re-renders; Enter on a line
 * without a pipe completes it to "value | value" before moving on.
 */
function OptionsTextarea({ options, onChange }: { options: Spec[]; onChange: (options: Spec[]) => void }) {
  const [text, setText] = useState(() => formatOptions(options))
  const lastEmitted = useRef(formatOptions(options))
  // Adopt external changes (e.g. YAML import) but not our own echoes.
  useEffect(() => {
    const incoming = formatOptions(options)
    if (incoming !== lastEmitted.current) { setText(incoming); lastEmitted.current = incoming }
  }, [options])
  const commit = (next: string) => {
    setText(next)
    const parsed = parseOptions(next)
    lastEmitted.current = formatOptions(parsed)
    onChange(parsed)
  }
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter') return
    const el = e.currentTarget
    const { selectionStart, value } = el
    const lineStart = value.lastIndexOf('\n', selectionStart - 1) + 1
    const lineEndIdx = value.indexOf('\n', selectionStart)
    const lineEnd = lineEndIdx === -1 ? value.length : lineEndIdx
    const line = value.slice(lineStart, lineEnd)
    if (line.trim() && !line.includes('|')) {
      e.preventDefault()
      const completed = `${line.trim()} | ${line.trim()}`
      const next = `${value.slice(0, lineStart)}${completed}\n${value.slice(lineEnd)}`
      commit(next)
      const caret = lineStart + completed.length + 1
      requestAnimationFrame(() => el.setSelectionRange(caret, caret))
    }
  }
  return (
    <Field label="Options, one per line as value | label" help="Example: residential | Residential. Press Enter on a bare value to copy it as the label.">
      <Textarea rows={Math.max(4, text.split('\n').length + 1)} value={text} onChange={(e) => commit(e.target.value)} onKeyDown={handleKeyDown} className="px-3 py-1.5 font-mono text-xs" placeholder={'residential | Residential\nmobile | Mobile'} spellCheck={false} />
    </Field>
  )
}
