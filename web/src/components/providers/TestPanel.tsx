// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Play } from 'lucide-react'
import { ProviderField, ProviderSummary, ProviderTestResponse, testProvider } from '../../api/client'
import { Alert, Badge, Button, Select } from '../ui'
import { SchemaForm, FormValues, defaultValues, serializeValues } from '../SchemaForm'
import { Field, Spec } from './editors'

type Action = 'validate' | 'options' | 'list_proxies'

/** Connector keys referenced as `{connector.<key>}` anywhere in an HTTP call. */
function connectorKeysOf(call: Spec | undefined): string[] {
  if (!call) return []
  const text = [call.url ?? '', ...Object.values(call.headers ?? {}), ...Object.values(call.params ?? {}), call.body ? JSON.stringify(call.body) : '']
    .map(String).join(' ')
  const keys = new Set<string>()
  for (const m of text.matchAll(/\{connector\.([a-zA-Z_][a-zA-Z0-9_]*)/g)) keys.add(m[1])
  return [...keys]
}

/**
 * Exercise a (possibly unsaved) descriptor against the real vendor API with
 * throwaway config. Requests are shown with secrets redacted by the server.
 */
export function TestPanel({ spec, presets }: { spec: Spec; presets: Record<string, { value: string; label: string; description?: string | null }[]> }) {
  const credentialFields: ProviderField[] = (spec.credential_fields ?? []) as ProviderField[]
  const connectorFields: ProviderField[] = (spec.connector_fields ?? []) as ProviderField[]
  const optionNames = Object.keys(spec.options ?? {})
  const listTypes: Spec[] = (spec.proxy_types ?? []).filter((t: Spec) => t.mode === 'list')
  const available: Action[] = [
    ...(spec.validation ? (['validate'] as Action[]) : []),
    ...(optionNames.length ? (['options'] as Action[]) : []),
    ...(listTypes.length ? (['list_proxies'] as Action[]) : []),
  ]
  const [action, setAction] = useState<Action>(available[0] ?? 'validate')
  const [optionName, setOptionName] = useState(optionNames[0] ?? '')
  const [credential, setCredential] = useState<FormValues>(() => defaultValues(credentialFields))
  const [connector, setConnector] = useState<FormValues>(() => defaultValues(connectorFields))
  const [result, setResult] = useState<ProviderTestResponse | null>(null)
  useEffect(() => { if (!available.includes(action) && available[0]) setAction(available[0]) }, [available, action])

  // Only the connector inputs the chosen call actually reads are shown. Fields
  // that normally load their options from the vendor become plain inputs, so a
  // draft can be tested before it is saved.
  const neededKeys = useMemo(() => {
    if (action === 'options') return connectorKeysOf(spec.options?.[optionName]?.call)
    if (action === 'list_proxies') {
      const keys = new Set<string>()
      for (const t of listTypes) { for (const k of connectorKeysOf(t.list?.call)) keys.add(k); const cf = String(t.count_field ?? 'connector.num_proxies'); if (cf.startsWith('connector.')) keys.add(cf.slice(10)) }
      if (spec.proxy_type_field?.startsWith('connector.')) keys.add(spec.proxy_type_field.slice(10))
      return [...keys]
    }
    return []
  }, [action, optionName, spec, listTypes])
  const neededFields: ProviderField[] = useMemo(
    () => connectorFields.filter((f) => neededKeys.includes(f.key)).map((f) => (f.options_from ? { ...f, options_from: null, options_from_when: null, options: [], options_preset: null, type: 'text' as const, help: f.help ? `${f.help} Enter the vendor value directly here.` : 'Enter the vendor value directly, e.g. from an options test.' } : f)),
    [connectorFields, neededKeys],
  )
  // Optional fields (filters) may stay empty: the server drops empty parameters.
  const missing = neededFields.filter((f) => f.required && !connector[f.key]).map((f) => f.label)

  const pseudoProvider = { id: spec.id ?? 'draft', name: spec.name ?? 'Draft', egress_hosts: [], has_validation: !!spec.validation } as unknown as ProviderSummary
  const scopes = { credential, connector }

  const mutation = useMutation({
    mutationFn: () => testProvider(spec.id ?? 'draft', {
      action,
      option_name: action === 'options' ? optionName : undefined,
      credential_config: serializeValues(credentialFields, credential, scopes),
      connector_config: serializeValues(connectorFields, connector, scopes),
      spec,
    }),
    onSuccess: setResult,
    onError: (e: Error) => setResult({ ok: false, message: e.message || 'Request failed', result: null, traces: [] }),
  })

  if (!available.length) {
    return (
      <Alert variant="info" className="text-xs">
        This provider makes no vendor API calls, so there is nothing to test here. It only builds proxy endpoints from the credential and connector fields.
        Add a credential validation, an options source or a list-mode proxy type under <b>Discovery</b> / <b>Proxy types</b> to enable testing.
      </Alert>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-fg-muted">Runs the descriptor's vendor calls with the values below. Nothing is saved; secrets are redacted in the request log.</p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="What to test">
          <Select value={action} onChange={(e) => setAction(e.target.value as Action)} className="px-3 py-1.5 text-sm">
            {available.includes('validate') && <option value="validate">Credential validation</option>}
            {available.includes('options') && <option value="options">Options source</option>}
            {available.includes('list_proxies') && <option value="list_proxies">Proxy list</option>}
          </Select>
        </Field>
        {action === 'options' && (
          <Field label="Options source">
            <Select value={optionName} onChange={(e) => setOptionName(e.target.value)} className="px-3 py-1.5 text-sm">
              {optionNames.map((n) => <option key={n} value={n}>{n}</option>)}
            </Select>
          </Field>
        )}
      </div>
      <div className="rounded-lg border border-line p-3 space-y-3">
        <div className="text-xs font-semibold">Credential</div>
        {credentialFields.length ? (
          <SchemaForm provider={pseudoProvider} fields={credentialFields} values={credential} onChange={(k, v, fill) => setCredential((p) => ({ ...p, [k]: v, ...(fill ?? {}) }))} scopes={scopes} presets={presets} />
        ) : <p className="text-xs text-fg-muted">No credential fields defined yet.</p>}
      </div>
      {neededFields.length > 0 && (
        <div className="rounded-lg border border-line p-3 space-y-3">
          <div className="text-xs font-semibold">Connector values this call uses</div>
          <SchemaForm provider={pseudoProvider} fields={neededFields} values={connector} onChange={(k, v, fill) => setConnector((p) => ({ ...p, [k]: v, ...(fill ?? {}) }))} scopes={scopes} presets={presets} columns={2} />
        </div>
      )}
      <div className="flex items-center gap-3">
        <Button type="button" size="sm" onClick={() => mutation.mutate()} disabled={mutation.isPending || missing.length > 0}>
          <Play className="w-3.5 h-3.5" /> {mutation.isPending ? 'Running…' : 'Run test'}
        </Button>
        {missing.length > 0 && <span className="text-xs text-fg-muted">Fill in {missing.join(', ')} first.</span>}
      </div>
      {result && (
        <div className="space-y-3">
          <Alert variant={result.ok ? 'success' : 'error'} className="text-xs">{result.ok ? '✓ ' : ''}{result.message || (result.ok ? 'OK' : 'Failed')}</Alert>
          {result.result != null && (
            <pre className="text-[11px] font-mono bg-surface-raised rounded-lg p-3 overflow-auto max-h-64">{JSON.stringify(result.result, null, 2)}</pre>
          )}
          {result.traces.length > 0 && (
            <div className="space-y-1.5">
              <div className="text-xs font-semibold">Requests</div>
              {result.traces.map((t, i) => (
                <div key={i} className="rounded-lg border border-line px-3 py-2 text-[11px] font-mono space-y-1">
                  <div className="flex items-center gap-2">
                    <Badge color={t.error ? 'red' : t.status && t.status < 300 ? 'green' : 'yellow'} className="py-0 text-[10px]">{t.status ?? 'ERR'}</Badge>
                    <span className="font-semibold">{t.method}</span>
                    <span className="truncate flex-1">{t.url}</span>
                    <span className="text-fg-subtle">{Math.round(t.elapsed_ms)} ms</span>
                  </div>
                  {Object.keys(t.headers).length > 0 && <div className="text-fg-muted">{Object.entries(t.headers).map(([k, v]) => `${k}: ${v}`).join(' · ')}</div>}
                  {t.error && <div className="text-danger">{t.error}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
