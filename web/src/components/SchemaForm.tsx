// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Eye, EyeOff } from 'lucide-react'
import { ProviderCondition, ProviderField, ProviderOption, ProviderSummary, ResolvedProviderOption, resolveProviderOptions } from '../api/client'
import { Input, Label, Select, Textarea } from './ui'
import { RichSelect, RichSelectOption } from './RichSelect'

export type FormValues = Record<string, string>

/** Both scopes are visible to `show_when` conditions: `credential.<key>` / `connector.<key>`. */
export interface SchemaScopes {
  credential: Record<string, unknown>
  connector: Record<string, unknown>
}

export function evaluateCondition(condition: ProviderCondition | null | undefined, scopes: SchemaScopes): boolean {
  if (!condition) return true
  const [scope, key] = condition.field.split('.', 2)
  const source = scope === 'credential' ? scopes.credential : scope === 'connector' ? scopes.connector : {}
  const raw = key ? source[key] : undefined
  const text = raw == null ? '' : String(raw)
  let result: boolean
  if (condition.equals != null) result = text === condition.equals
  else if (condition.in) result = condition.in.includes(text)
  else result = text !== ''
  return condition.negate ? !result : result
}

/** Initial form values for a field list: declared defaults, or empty strings. */
export function defaultValues(fields: ProviderField[]): FormValues {
  const values: FormValues = {}
  for (const f of fields) values[f.key] = f.default == null ? '' : String(f.default)
  return values
}

/** Convert form strings back to typed values for the API. Hidden fields are dropped. */
export function serializeValues(fields: ProviderField[], values: FormValues, scopes: SchemaScopes): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const f of fields) {
    if (!evaluateCondition(f.show_when, scopes)) continue
    const raw = values[f.key]
    if (raw == null || raw === '') continue
    if (f.type === 'number') { const n = Number(raw); out[f.key] = Number.isNaN(n) ? raw : n }
    else if (f.type === 'boolean') out[f.key] = raw === 'true'
    else out[f.key] = raw
  }
  return out
}

/** Options currently loaded for each dynamic select, keyed by field key. Shared so siblings can read extras. */
type LoadedOptions = Record<string, ResolvedProviderOption[]>

interface SchemaFormProps {
  provider: ProviderSummary
  fields: ProviderField[]
  values: FormValues
  onChange: (key: string, value: string, fill?: Record<string, string>) => void
  scopes: SchemaScopes
  presets: Record<string, ProviderOption[]>
  /** For remote options: saved credential to resolve with. */
  credentialId?: string | null
  /** For remote options while creating a credential: the in-progress config. */
  credentialConfig?: Record<string, unknown> | null
  isEdit?: boolean
  disabled?: boolean
  /** Grid columns; credential forms use one column, connector forms two. */
  columns?: 1 | 2
}

/**
 * Renders a provider's field list. Selects with `options_from` fetch options
 * from the vendor through the server (re-fetching when a `depends_on` sibling
 * changes) and apply `fill` mappings on change. Number fields may take their
 * maximum from a sibling's selected option (`max_from_option`).
 */
export function SchemaForm({ provider, fields, values, onChange, scopes, presets, credentialId, credentialConfig, isEdit, disabled, columns = 1 }: SchemaFormProps) {
  const [loaded, setLoaded] = useState<LoadedOptions>({})
  const onLoaded = useCallback((key: string, options: ResolvedProviderOption[]) => {
    setLoaded((prev) => (prev[key] === options ? prev : { ...prev, [key]: options }))
  }, [])
  const visible = fields.filter((f) => evaluateCondition(f.show_when, scopes))
  return (
    <div className={columns === 2 ? 'grid grid-cols-2 gap-x-3 gap-y-3' : 'space-y-4'}>
      {visible.map((field) => (
        <SchemaField
          key={field.key}
          provider={provider}
          field={field}
          value={values[field.key] ?? ''}
          values={values}
          onChange={(v, fill) => onChange(field.key, v, fill)}
          scopes={scopes}
          presets={presets}
          credentialId={credentialId}
          credentialConfig={credentialConfig}
          isEdit={isEdit}
          disabled={disabled}
          loaded={loaded}
          onLoaded={onLoaded}
          wide={columns === 2 && field.type === 'textarea'}
        />
      ))}
    </div>
  )
}

/** Resolve `max_from_option`: the first referenced sibling option that carries a numeric extra wins. */
function optionMax(field: ProviderField, values: FormValues, loaded: LoadedOptions): number | null {
  for (const ref of field.max_from_option ?? []) {
    const selected = loaded[ref.field]?.find((o) => o.value === values[ref.field])
    const raw = selected?.extra?.[ref.extra]
    const n = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : NaN
    if (!Number.isNaN(n) && n > 0) return n
  }
  return null
}

function SchemaField({ provider, field, value, values, onChange, scopes, presets, credentialId, credentialConfig, isEdit, disabled, loaded, onLoaded, wide }: {
  provider: ProviderSummary
  field: ProviderField
  value: string
  values: FormValues
  onChange: (value: string, fill?: Record<string, string>) => void
  scopes: SchemaScopes
  presets: Record<string, ProviderOption[]>
  credentialId?: string | null
  credentialConfig?: Record<string, unknown> | null
  isEdit?: boolean
  disabled?: boolean
  loaded: LoadedOptions
  onLoaded: (key: string, options: ResolvedProviderOption[]) => void
  wide?: boolean
}) {
  const [show, setShow] = useState(false)
  const isSecret = field.secret || field.type === 'password'
  const readOnly = disabled || field.readonly
  const inputClass = 'px-3 py-1.5 text-sm'
  const useRemote = !!field.options_from && evaluateCondition(field.options_from_when, scopes)
  const dynamicMax = field.type === 'number' ? optionMax(field, values, loaded) : null
  const effectiveMax = dynamicMax ?? field.max ?? undefined

  // Clamp the value when a sibling selection lowers the ceiling.
  useEffect(() => {
    if (dynamicMax != null && value !== '' && Number(value) > dynamicMax) onChange(String(dynamicMax))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dynamicMax])

  const label = (
    <Label className="text-xs">
      {field.label}{field.required && <span className="text-danger ml-1">*</span>}
    </Label>
  )
  const help = (field.help || dynamicMax != null) && (
    <p className="text-xs text-fg-muted mt-1">
      {dynamicMax != null && <span className="font-medium text-fg">Max {dynamicMax}. </span>}
      {field.help}
    </p>
  )

  let control: React.ReactNode
  if (useRemote) {
    control = (
      <RemoteSelect
        provider={provider}
        field={field}
        value={value}
        onChange={onChange}
        credentialId={credentialId}
        credentialConfig={credentialConfig}
        connectorConfig={scopes.connector}
        disabled={readOnly}
        onLoaded={onLoaded}
      />
    )
  } else if (field.type === 'select' || field.type === 'country') {
    const options: ProviderOption[] = field.options.length ? field.options : (field.options_preset ? presets[field.options_preset] ?? [] : presets.countries ?? [])
    const rich: RichSelectOption[] = options.map((o) => ({ value: o.value, label: o.label, description: o.description ?? undefined }))
    if (field.readonly) {
      const selected = rich.find((o) => o.value === value)
      control = <Input value={selected?.label ?? value} readOnly disabled className={`${inputClass} bg-surface-raised`} placeholder={field.placeholder ?? '—'} />
    } else {
      control = rich.length > 12
        ? <RichSelect options={rich} value={value} onChange={(v) => onChange(v)} placeholder={field.placeholder ?? `Select ${field.label.toLowerCase()}`} required={field.required} disabled={readOnly} />
        : (
          <Select value={value} onChange={(e) => onChange(e.target.value)} className={inputClass} required={field.required} disabled={readOnly}>
            {!field.required && !rich.some((o) => o.value === '') && <option value="">{field.empty_label ?? '—'}</option>}
            {rich.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </Select>
        )
    }
  } else if (field.type === 'boolean') {
    control = (
      <label className="flex items-center gap-2 h-9 text-[13px] cursor-pointer select-none">
        <input type="checkbox" checked={value === 'true'} onChange={(e) => onChange(e.target.checked ? 'true' : 'false')} className="w-4 h-4" disabled={readOnly} />
        {field.placeholder ?? 'Enabled'}
      </label>
    )
  } else if (field.type === 'textarea') {
    control = (
      <Textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={5}
        className={`${inputClass} ${isSecret ? 'font-mono text-xs' : ''}`}
        placeholder={field.placeholder ?? (isEdit && isSecret ? 'Leave unchanged to keep the current secret' : field.label)}
        autoComplete="off"
        required={field.required}
        disabled={readOnly}
      />
    )
  } else {
    control = (
      <div className="relative">
        <Input
          type={field.type === 'number' ? 'number' : field.type === 'url' ? 'url' : isSecret && !show ? 'password' : 'text'}
          name={`octoprox-field-${field.key}`}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={`${inputClass} ${isSecret ? 'pr-10 font-mono' : ''} ${field.readonly ? 'bg-surface-raised' : ''}`}
          placeholder={field.placeholder ?? (isEdit && isSecret ? 'Leave unchanged to keep the current secret' : field.label)}
          autoComplete={isSecret ? 'new-password' : 'off'}
          min={field.type === 'number' && field.min != null ? field.min : undefined}
          max={field.type === 'number' ? effectiveMax : undefined}
          required={field.required}
          disabled={readOnly}
          readOnly={field.readonly}
          data-1p-ignore
          data-lpignore="true"
        />
        {isSecret && (
          <button type="button" onClick={() => setShow((s) => !s)} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-fg-subtle hover:text-fg-muted" tabIndex={-1}>
            {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
          </button>
        )}
      </div>
    )
  }

  return (
    <div className={wide ? 'col-span-2' : undefined}>
      {label}
      {control}
      {help}
    </div>
  )
}

function RemoteSelect({ provider, field, value, onChange, credentialId, credentialConfig, connectorConfig, disabled, onLoaded }: {
  provider: ProviderSummary
  field: ProviderField
  value: string
  onChange: (value: string, fill?: Record<string, string>) => void
  credentialId?: string | null
  credentialConfig?: Record<string, unknown> | null
  connectorConfig: Record<string, unknown>
  disabled?: boolean
  onLoaded: (key: string, options: ResolvedProviderOption[]) => void
}) {
  // Only the connector keys the source actually reads take part in the request and the cache key.
  const dependencies = useMemo(() => {
    const picked: Record<string, unknown> = {}
    for (const key of field.depends_on ?? []) if (connectorConfig[key] != null && connectorConfig[key] !== '') picked[key] = connectorConfig[key]
    return picked
  }, [field.depends_on, connectorConfig])
  const missingDependency = (field.depends_on ?? []).some((k) => dependencies[k] == null)
  const hasSource = !missingDependency && (!!credentialId || (!!credentialConfig && Object.values(credentialConfig).some((v) => v !== '' && v != null)))
  const { data, isLoading, error } = useQuery({
    queryKey: ['provider-options', provider.id, field.options_from, credentialId ?? null, credentialId ? null : credentialConfig, dependencies],
    queryFn: () => resolveProviderOptions(provider.id, field.options_from!, credentialId ? { credential_id: credentialId, connector_config: dependencies } : { credential_config: credentialConfig ?? {}, connector_config: dependencies }),
    enabled: hasSource,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })
  useEffect(() => { if (data) onLoaded(field.key, data) }, [data, field.key, onLoaded])

  const options: RichSelectOption[] = useMemo(() => {
    const list: RichSelectOption[] = (data ?? []).map((o) => ({ value: o.value, label: o.label, description: o.description ?? undefined }))
    if (!field.required && data) list.unshift({ value: '', label: field.empty_label ?? '—' })
    return list
  }, [data, field.required, field.empty_label])

  const handleChange = (v: string) => {
    const selected = data?.find((o) => o.value === v)
    const fill: Record<string, string> = {}
    if (selected) {
      for (const [target, source] of Object.entries(field.fill)) {
        const extra = selected.extra?.[source]
        if (extra != null) fill[target] = String(extra)
      }
    }
    onChange(v, fill)
  }

  const placeholder = missingDependency
    ? `Select ${(field.depends_on ?? []).join(', ').replace(/_/g, ' ')} first`
    : !hasSource ? 'Select a credential first' : isLoading ? 'Loading…' : options.length ? (field.placeholder ?? `Select ${field.label.toLowerCase()}`) : 'No options returned'

  return (
    <>
      <RichSelect options={options} value={value} onChange={handleChange} placeholder={placeholder} required={field.required} disabled={disabled || !hasSource || isLoading} />
      {error && <p className="text-xs text-danger mt-1">{(error as Error).message || 'Could not load options'}</p>}
    </>
  )
}
