// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Eye, EyeOff, X } from 'lucide-react'
import { ProviderCondition, ProviderField, ProviderOption, ProviderSpec, ProviderSummary, ResolvedProviderOption, resolveProviderOptions } from '../api/client'
import { InfoTip, Input, Label, Select, Textarea } from './ui'
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

/** Country fields hold a list of ISO codes; the form keeps them as a comma-separated string. */
export const isCountryField = (f: ProviderField) => f.type === 'country' || f.options_preset === 'countries'
export const splitCountries = (raw: string | undefined | null): string[] =>
  (raw ?? '').split(',').map((c) => c.trim().toUpperCase()).filter(Boolean)

/** Convert form strings back to typed values for the API. Hidden fields are dropped. */
export function serializeValues(fields: ProviderField[], values: FormValues, scopes: SchemaScopes): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const f of fields) {
    if (!evaluateCondition(f.show_when, scopes)) continue
    const raw = values[f.key]
    if (raw == null || raw === '') continue
    if (f.type === 'number') { const n = Number(raw); out[f.key] = Number.isNaN(n) ? raw : n }
    else if (f.type === 'boolean') out[f.key] = raw === 'true'
    else if (isCountryField(f)) { const codes = splitCountries(raw); if (codes.length) out[f.key] = codes }
    else out[f.key] = raw
  }
  return out
}

/** Picks several countries from a preset list. `value` is the comma-separated form string. */
export function MultiCountryPicker({ options, value, onChange, disabled, placeholder }: {
  options: ProviderOption[]
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  placeholder?: string
}) {
  const selected = splitCountries(value)
  const find = (code: string) => options.find((o) => o.value.toUpperCase() === code)
  // Vendor lists often label a country with its bare code in lower case ("nz"); show codes upper-cased.
  const displayLabel = (o: ProviderOption) => (o.label.toUpperCase() === o.value.toUpperCase() ? o.value.toUpperCase() : o.label)
  const labelFor = (code: string) => { const o = find(code); return o ? displayLabel(o) : code }
  const available: RichSelectOption[] = options
    .filter((o) => o.value && !selected.includes(o.value.toUpperCase()))
    .map((o) => ({ value: o.value.toUpperCase(), label: displayLabel(o), description: o.description ?? o.value.toUpperCase() }))
  const add = (code: string) => { if (code) onChange([...selected, code.toUpperCase()].join(',')) }
  const remove = (code: string) => onChange(selected.filter((c) => c !== code).join(','))
  return (
    <div className="space-y-1.5">
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {selected.map((code) => (
            <span key={code} className="inline-flex items-center gap-1.5 pl-2 pr-1.5 py-0.5 rounded-full border border-line bg-surface-raised text-xs">
              <span className="font-mono font-medium">{code}</span>
              <span className="text-fg-muted truncate max-w-[10rem]">{labelFor(code)}</span>
              {!disabled && (
                <button type="button" onClick={() => remove(code)} className="text-fg-subtle hover:text-fg" aria-label={`Remove ${code}`}>
                  <X className="w-3 h-3" />
                </button>
              )}
            </span>
          ))}
        </div>
      )}
      <RichSelect options={available} value="" onChange={add} disabled={disabled} placeholder={placeholder ?? (selected.length ? 'Add another country' : 'All countries')} />
    </div>
  )
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
  /** For remote options on an unsaved provider (admin test panel): the draft descriptor to resolve against. */
  spec?: ProviderSpec | null
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
export function SchemaForm({ provider, fields, values, onChange, scopes, presets, credentialId, credentialConfig, spec, isEdit, disabled, columns = 1 }: SchemaFormProps) {
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
          spec={spec}
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

/** Resolve `max_from_option`: the first referenced sibling option that carries a numeric extra wins.
 * A multi-value sibling (countries) sums the extra across its selected options. */
function optionMax(field: ProviderField, values: FormValues, loaded: LoadedOptions): number | null {
  const toNumber = (raw: unknown) => (typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : NaN)
  for (const ref of field.max_from_option ?? []) {
    const wanted = (values[ref.field] ?? '').split(',').map((v) => v.trim().toUpperCase()).filter(Boolean)
    const matches = (loaded[ref.field] ?? []).filter((o) => wanted.includes(o.value.toUpperCase()))
    const total = matches.reduce((sum, o) => { const n = toNumber(o.extra?.[ref.extra]); return Number.isNaN(n) ? sum : sum + n }, 0)
    if (total > 0) return total
  }
  return null
}

function SchemaField({ provider, field, value, values, onChange, scopes, presets, credentialId, credentialConfig, spec, isEdit, disabled, loaded, onLoaded, wide }: {
  provider: ProviderSummary
  field: ProviderField
  value: string
  values: FormValues
  onChange: (value: string, fill?: Record<string, string>) => void
  scopes: SchemaScopes
  presets: Record<string, ProviderOption[]>
  credentialId?: string | null
  credentialConfig?: Record<string, unknown> | null
  spec?: ProviderSpec | null
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
      <span className="inline-flex items-center gap-1.5">
        <span>{field.label}{field.required && <span className="text-danger ml-1">*</span>}</span>
        {field.details && <InfoTip label={`About ${field.label.toLowerCase()}`}>{field.details}</InfoTip>}
      </span>
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
        spec={spec}
        connectorConfig={scopes.connector}
        disabled={readOnly}
        onLoaded={onLoaded}
      />
    )
  } else if (isCountryField(field)) {
    const options: ProviderOption[] = field.options.length ? field.options : presets.countries ?? []
    control = <MultiCountryPicker options={options} value={value} onChange={onChange} disabled={readOnly} placeholder={field.placeholder ?? undefined} />
  } else if (field.type === 'select') {
    const options: ProviderOption[] = field.options.length ? field.options : (field.options_preset ? presets[field.options_preset] ?? [] : presets.countries ?? [])
    const rich: RichSelectOption[] = options.map((o) => ({ value: o.value, label: o.label, description: o.description ?? undefined }))
    if (field.readonly) {
      const selected = rich.find((o) => o.value === value)
      control = <Input value={selected?.label ?? value} readOnly disabled className={`${inputClass} bg-surface-raised`} placeholder={field.placeholder ?? '-'} />
    } else {
      control = rich.length > 12
        ? <RichSelect options={rich} value={value} onChange={(v) => onChange(v)} placeholder={field.placeholder ?? `Select ${field.label.toLowerCase()}`} required={field.required} disabled={readOnly} />
        : (
          <Select value={value} onChange={(e) => onChange(e.target.value)} className={inputClass} required={field.required} disabled={readOnly}>
            {!field.required && !rich.some((o) => o.value === '') && <option value="">{field.empty_label ?? '-'}</option>}
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

function RemoteSelect({ provider, field, value, onChange, credentialId, credentialConfig, spec, connectorConfig, disabled, onLoaded }: {
  provider: ProviderSummary
  field: ProviderField
  value: string
  onChange: (value: string, fill?: Record<string, string>) => void
  credentialId?: string | null
  credentialConfig?: Record<string, unknown> | null
  spec?: ProviderSpec | null
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
    queryKey: ['provider-options', provider.id, field.options_from, credentialId ?? null, credentialId ? null : credentialConfig, dependencies, spec ?? null],
    queryFn: () => resolveProviderOptions(provider.id, field.options_from!, {
      ...(credentialId ? { credential_id: credentialId } : { credential_config: credentialConfig ?? {} }),
      connector_config: dependencies,
      ...(spec ? { spec } : {}),
    }),
    enabled: hasSource,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })
  useEffect(() => { if (data) onLoaded(field.key, data) }, [data, field.key, onLoaded])

  const options: RichSelectOption[] = useMemo(() => {
    const list: RichSelectOption[] = (data ?? []).map((o) => ({ value: o.value, label: o.label, description: o.description ?? undefined }))
    if (!field.required && data) list.unshift({ value: '', label: field.empty_label ?? '-' })
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

  if (isCountryField(field)) {
    // Vendor-listed countries (e.g. the countries an ISP zone has IPs in) are still a multi-select.
    const countryOptions: ProviderOption[] = (data ?? []).map((o) => ({ value: o.value, label: o.label, description: o.description ?? undefined }))
    return (
      <>
        <MultiCountryPicker options={countryOptions} value={value} onChange={(v) => onChange(v)} disabled={disabled || !hasSource || isLoading} placeholder={hasSource && !isLoading ? undefined : placeholder} />
        {error && <p className="text-xs text-danger mt-1">{(error as Error).message || 'Could not load options'}</p>}
      </>
    )
  }

  return (
    <>
      <RichSelect options={options} value={value} onChange={handleChange} placeholder={placeholder} required={field.required} disabled={disabled || !hasSource || isLoading} />
      {error && <p className="text-xs text-danger mt-1">{(error as Error).message || 'Could not load options'}</p>}
    </>
  )
}
