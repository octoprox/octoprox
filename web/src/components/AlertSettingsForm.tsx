// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { RotateCcw } from 'lucide-react'
import type { AlertSettings, AlertSettingsOverrides, AlertSettingsResponse } from '../api/client'
import { cn } from '../utils/cn'
import { MAX_RETENTION_DAYS } from './RetentionCard'
import { Button, Input, InspectorSection } from './ui'

/** Every cut-off that is a number; `success_rate` is the one switch. */
type NumberField = Exclude<keyof AlertSettings, 'success_rate'>

/** The tab as it is being edited: each number as typed, empty for its default (for `keep_days`, the metrics'), and the switch. */
export interface AlertDraft {
  success_rate: boolean
  values: Partial<Record<NumberField, string>>
}

/** What is wrong with a typed value, by field. */
export type AlertDraftErrors = Partial<Record<NumberField, string>>

interface FieldSpec {
  key: NumberField
  label: string
  unit: string
  min: number
  max: number
  /** Said after the default, where the bounds mean something. */
  note?: string
}

interface GroupSpec {
  title: string
  intro: string
  fields: FieldSpec[]
}

const PERCENT = { unit: '%', min: 1, max: 100 }
const COUNT = { min: 1, max: 1_000_000 }

/** The number fields by group, in the tab's order; the success rate switch leads its group and keep_days is its own. */
const GROUPS: GroupSpec[] = [
  {
    title: 'Success rate',
    intro: 'The share of requests that reached a proxy and succeeded in the last 15 minutes, as Overview counts it.',
    fields: [
      { key: 'success_warning_below', label: 'Warning when below', ...PERCENT },
      { key: 'success_problem_below', label: 'Problem when below', unit: '%', min: 0, max: 99, note: '0 never makes it a problem' },
      { key: 'success_min_requests', label: 'Only with at least', unit: 'requests', ...COUNT },
    ],
  },
  {
    title: 'Requests turned away',
    intro: 'Requests Octoprox refused before using a proxy in the last 15 minutes, as a share of all requests.',
    fields: [
      { key: 'refusals_min', label: 'Only with at least', unit: 'requests', ...COUNT },
      { key: 'refusals_warning_percent', label: 'Warning from', ...PERCENT },
      { key: 'refusals_problem_percent', label: 'Problem from', ...PERCENT },
      { key: 'invalid_location_min', label: 'Locations that could not be read: warning from', unit: 'requests', ...COUNT },
    ],
  },
  {
    title: 'Vendor refusals',
    intro: 'A vendor refusing a connector\'s requests in the last 15 minutes, as a share of that connector\'s requests.',
    fields: [
      { key: 'vendor_min', label: 'Only with at least', unit: 'refused', ...COUNT },
      { key: 'vendor_login_problem_percent', label: 'Login rejected (407): problem from', ...PERCENT },
      { key: 'vendor_rate_limited_warning_percent', label: 'Too many requests (429): warning from', ...PERCENT },
      { key: 'vendor_rate_limited_problem_percent', label: 'Too many requests (429): problem from', ...PERCENT },
      { key: 'vendor_forbidden_warning_percent', label: 'Not allowed (403): warning from', ...PERCENT },
      { key: 'vendor_forbidden_problem_percent', label: 'Not allowed (403): problem from', ...PERCENT },
      { key: 'vendor_cannot_serve_warning_percent', label: 'Cannot serve (400, 502, 503): warning from', ...PERCENT },
      { key: 'vendor_other_warning_percent', label: 'Any other refusal: warning from', ...PERCENT },
    ],
  },
  {
    title: 'Connectors',
    intro: 'A connector whose proxies fail its requests, whose list sync or cloud instances keep failing, or that does not finish deleting.',
    fields: [
      { key: 'connector_failing_min_requests', label: 'Failing requests: only with at least', unit: 'requests', ...COUNT },
      { key: 'connector_failing_warning_percent', label: 'Failing requests: warning from', ...PERCENT },
      { key: 'connector_failing_problem_percent', label: 'Failing requests: problem from', ...PERCENT },
      { key: 'connector_errors_min', label: 'Sync or cloud errors: show after', unit: 'in a row', min: 1, max: 100 },
      { key: 'stuck_deleting_minutes', label: 'Still deleting: show after', unit: 'minutes', min: 5, max: 10_080 },
    ],
  },
  {
    title: 'Proxies and exits',
    intro: 'Proxies failing their health checks, exits outside a connector\'s countries or not where its vendor says, and sites that block every connector.',
    fields: [
      { key: 'unhealthy_share_percent', label: 'Unhealthy proxies: from', ...PERCENT },
      { key: 'unhealthy_min', label: 'Unhealthy proxies: and at least', unit: 'proxies', ...COUNT },
      { key: 'exits_outside_percent', label: 'Exits outside the listed countries: from', ...PERCENT },
      { key: 'exits_outside_min', label: 'Exits outside the listed countries: and at least', unit: 'proxies', ...COUNT },
      { key: 'exits_contradicted_percent', label: 'Exits not where the vendor says: from', ...PERCENT },
      { key: 'exits_contradicted_min', label: 'Exits not where the vendor says: and at least', unit: 'proxies', ...COUNT },
      { key: 'site_blocked_percent', label: 'A site blocking every connector: from', unit: '% blocked', min: 1, max: 100 },
    ],
  },
]

const KEEP_DAYS: FieldSpec = { key: 'keep_days', label: 'Keep resolved alerts for', unit: 'days', min: 0, max: MAX_RETENTION_DAYS }

const FIELDS: FieldSpec[] = [...GROUPS.flatMap((group) => group.fields), KEEP_DAYS]

/** A warning cut-off and the problem cut-off it may not pass. */
const PAIRS: [NumberField, NumberField][] = [
  ['refusals_warning_percent', 'refusals_problem_percent'],
  ['vendor_rate_limited_warning_percent', 'vendor_rate_limited_problem_percent'],
  ['vendor_forbidden_warning_percent', 'vendor_forbidden_problem_percent'],
  ['connector_failing_warning_percent', 'connector_failing_problem_percent'],
]

/** The draft of the stored settings: the values that differ from the defaults, the rest empty. */
export function draftOf({ settings, defaults }: AlertSettingsResponse): AlertDraft {
  const values: AlertDraft['values'] = {}
  for (const { key } of FIELDS) {
    const value = settings[key]
    if (value !== null && value !== defaults[key]) values[key] = String(value)
  }
  return { success_rate: settings.success_rate, values }
}

/** The overrides a draft keeps: each value that differs from its default. Read only from a draft without errors. */
export function overridesOf(draft: AlertDraft, defaults: AlertSettings): AlertSettingsOverrides {
  const overrides: AlertSettingsOverrides = {}
  if (draft.success_rate !== defaults.success_rate) overrides.success_rate = draft.success_rate
  for (const { key } of FIELDS) {
    const text = draft.values[key]?.trim() ?? ''
    if (text !== '' && Number(text) !== defaults[key]) overrides[key] = Number(text)
  }
  return overrides
}

/** Whether two sets of overrides keep the same values. */
export function sameOverrides(a: AlertSettingsOverrides, b: AlertSettingsOverrides): boolean {
  const keys = Object.keys(a) as (keyof AlertSettings)[]
  return keys.length === Object.keys(b).length && keys.every((key) => a[key] === b[key])
}

/** What is wrong with a draft, as the server would refuse it: a value out of bounds, or a warning past its problem. */
export function draftErrors(draft: AlertDraft, defaults: AlertSettings): AlertDraftErrors {
  const errors: AlertDraftErrors = {}
  const value = (key: NumberField): number | null => {
    const text = draft.values[key]?.trim() ?? ''
    return text === '' ? defaults[key] : Number(text)
  }
  for (const { key, min, max } of FIELDS) {
    const text = draft.values[key]?.trim() ?? ''
    if (text !== '' && (!/^\d+$/.test(text) || Number(text) < min || Number(text) > max)) {
      errors[key] = `A whole number from ${min} to ${max.toLocaleString()}.`
    }
  }
  for (const [warning, problem] of PAIRS) {
    if (errors[warning] || errors[problem] || (value(warning) ?? 0) <= (value(problem) ?? 0)) continue
    errors[warning] = 'Cannot be above the problem cut-off below.'
    errors[problem] = 'Cannot be below the warning cut-off above.'
  }
  if (!errors.success_warning_below && !errors.success_problem_below
    && (value('success_problem_below') ?? 0) >= (value('success_warning_below') ?? 0)) {
    errors.success_warning_below = 'Must be above the problem cut-off below.'
    errors.success_problem_below = 'Must be below the warning cut-off above.'
  }
  return errors
}

/**
 * Project settings, Alert settings: the cut-offs of the Needs attention lines and how long resolved alerts are kept.
 * Each field shows its default, empty follows it, and a reset drops an override; the form holds the draft and saves it.
 */
export function AlertSettingsForm({ response, draft, errors, onChange, metricsRetentionDays }: {
  response: AlertSettingsResponse
  draft: AlertDraft
  errors: AlertDraftErrors
  onChange: (draft: AlertDraft) => void
  /** The General tab's value as it stands, which an empty Keep resolved alerts follows. */
  metricsRetentionDays: number
}) {
  const { defaults } = response
  const setValue = (key: NumberField, text: string) => onChange({ ...draft, values: { ...draft.values, [key]: text } })
  const atDefaults = draft.success_rate === defaults.success_rate && Object.values(draft.values).every((text) => !text?.trim())

  return (
    <div className="space-y-5">
      {GROUPS.map((group, index) => (
        <InspectorSection key={group.title} title={group.title}>
          <p className="text-xs text-fg-muted">{group.intro}</p>
          {index === 0 && (
            <div>
              <label className="inline-flex items-center gap-2 text-[13px] text-fg pointer-coarse:min-h-11">
                <input type="checkbox" checked={draft.success_rate} onChange={(e) => onChange({ ...draft, success_rate: e.target.checked })} className="w-4 h-4" />
                Warn about a low success rate
              </label>
              <p className="text-[11px] text-fg-subtle">Default {defaults.success_rate ? 'on' : 'off'}</p>
            </div>
          )}
          <div className="divide-y divide-line">
            {group.fields.map((field) => (
              <NumberInput
                key={field.key}
                field={field}
                text={draft.values[field.key] ?? ''}
                error={errors[field.key]}
                placeholder={String(defaults[field.key])}
                hint={`Default ${defaults[field.key]}${field.note ? `; ${field.note}` : ''}`}
                resetLabel={`Reset to ${defaults[field.key]}`}
                disabled={index === 0 && !draft.success_rate}
                onChange={(text) => setValue(field.key, text)}
              />
            ))}
          </div>
        </InspectorSection>
      ))}

      <InspectorSection title="Keep resolved alerts">
        <p className="text-xs text-fg-muted">How long the Alerts page keeps an alert after it resolved; open alerts are always kept.</p>
        <NumberInput
          field={KEEP_DAYS}
          text={draft.values.keep_days ?? ''}
          error={errors.keep_days}
          placeholder={`Same as metrics (${metricsRetentionDays || 'forever'})`}
          hint="Empty follows Metrics retention on General; 0 keeps them forever"
          resetLabel="Reset to the metrics retention"
          wide
          onChange={(text) => setValue('keep_days', text)}
        />
      </InspectorSection>

      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={atDefaults}
        onClick={() => onChange({ success_rate: defaults.success_rate, values: {} })}
        className="pointer-coarse:h-11"
      >
        <RotateCcw className="w-3.5 h-3.5" /> Reset all to defaults
      </Button>
    </div>
  )
}

/**
 * One cut-off: its words, the field with its unit, and a reset where it is overridden (kept in place, unseen, where it
 * is not, so the fields stay in one column); the default under it, or what is wrong with the value.
 */
function NumberInput({ field, text, error, placeholder, hint, resetLabel, disabled, wide, onChange }: {
  field: FieldSpec
  text: string
  error: string | undefined
  placeholder: string
  hint: string
  resetLabel: string
  disabled?: boolean
  /** A field whose placeholder is words. */
  wide?: boolean
  onChange: (text: string) => void
}) {
  const id = `alert-setting-${field.key}`
  const overridden = text.trim() !== ''
  return (
    <div className="py-2 first:pt-1 last:pb-0">
      <div className="flex items-center gap-x-3 gap-y-1.5 flex-wrap">
        <label htmlFor={id} className={cn('flex-1 min-w-[12rem] text-[13px]', disabled ? 'text-fg-subtle' : 'text-fg')}>{field.label}</label>
        {/* The span may shrink below its content, so on a phone the wide field narrows instead of the row overflowing,
            the unit giving up its spare room first. */}
        <span className="flex items-center gap-2 min-w-0 max-w-full">
          <Input
            id={id}
            type="number"
            inputMode="numeric"
            min={field.min}
            max={field.max}
            step={1}
            value={text}
            placeholder={placeholder}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
            aria-invalid={!!error}
            aria-describedby={`${id}-hint`}
            className={cn(
              'px-3 py-1.5 text-base pointer-fine:text-[13px] pointer-coarse:h-11 tabular-nums disabled:bg-surface-raised disabled:cursor-not-allowed',
              wide ? 'w-52 min-w-0' : 'w-24',
              error && 'border-danger text-danger',
            )}
          />
          <span className="w-[4.5rem] shrink-[4] text-[13px] text-fg-muted">{field.unit}</span>
          <button
            type="button"
            onClick={() => onChange('')}
            disabled={!overridden}
            aria-label={resetLabel}
            title={resetLabel}
            aria-hidden={!overridden || undefined}
            tabIndex={overridden ? undefined : -1}
            className={cn(
              'flex-none inline-flex items-center justify-center p-1.5 rounded-md text-fg-subtle hover:text-fg hover:bg-surface-raised transition-colors pointer-coarse:w-11 pointer-coarse:h-11',
              !overridden && 'invisible',
            )}
          >
            <RotateCcw className="w-3.5 h-3.5" />
          </button>
        </span>
      </div>
      <p id={`${id}-hint`} className={cn('text-[11px] mt-0.5', error ? 'text-danger' : 'text-fg-subtle')}>{error ?? hint}</p>
    </div>
  )
}
