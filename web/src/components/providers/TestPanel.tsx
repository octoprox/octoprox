// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Play } from 'lucide-react'
import { ProviderField, ProviderSummary, ProviderTestAction, ProviderTestResponse, testProvider } from '../../api/client'
import { Alert, Badge, Button, Select } from '../ui'
import { SchemaForm, FormValues, defaultValues, serializeValues } from '../SchemaForm'
import { Field, Spec, TextField } from './editors'

type Action = ProviderTestAction

const ACTION_LABELS: Record<Action, string> = {
  proxy_request: 'Request through a proxy',
  validate: 'Credential validation',
  options: 'Options source',
  list_proxies: 'Proxy list',
}

const ACTION_HELP: Record<Action, string> = {
  proxy_request: 'Builds one proxy endpoint exactly as a connector would (including IP discovery or the vendor list) and fetches a URL through it. This is the closest thing to the real thing.',
  validate: "Runs the descriptor's credential validation call and shows the captured values.",
  options: 'Loads a dynamic select from the vendor API.',
  list_proxies: 'Fetches the vendor proxy list and previews the first entries.',
}

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
 * Give a draft's fields the `depends_on` annotation the server adds when it
 * serves a stored provider, so dynamic selects wait for (and refetch on) the
 * connector values their options source reads. Mirrors `_with_dependencies`.
 */
function annotateDependencies(fields: ProviderField[], spec: Spec): ProviderField[] {
  return fields.map((f) => {
    const source: Spec | undefined = f.options_from ? spec.options?.[f.options_from] : undefined
    if (!source) return f
    const keys = new Set<string>(connectorKeysOf(source.call))
    for (const e of source.enrich ?? []) for (const k of connectorKeysOf(e.call)) keys.add(k)
    return { ...f, depends_on: [...keys].sort() }
  })
}

interface ProxyRequestResult {
  proxy: { host: string; port: number; protocol: string; username: string | null; metadata: Record<string, string> }
  target_url: string
  status?: number
  elapsed_ms?: number
  exit_ip?: string | null
  body?: string
}

/**
 * Exercise a descriptor against the real vendor with throwaway config. Works for
 * unsaved drafts (the in-editor `spec` is sent along) and for stored providers,
 * built-in or custom (`stored` is the served catalog entry: the server uses its
 * own copy of the descriptor, and the served fields carry the `depends_on`
 * annotations dynamic selects need). Requests are shown with secrets redacted
 * by the server; nothing is persisted.
 */
export function TestPanel({ spec, presets, stored }: {
  spec: Spec
  presets: Record<string, { value: string; label: string; description?: string | null }[]>
  stored?: ProviderSummary
}) {
  const credentialFields: ProviderField[] = useMemo(
    () => stored?.credential_fields ?? annotateDependencies((spec.credential_fields ?? []) as ProviderField[], spec),
    [stored, spec],
  )
  const connectorFields: ProviderField[] = useMemo(
    () => stored?.connector_fields ?? annotateDependencies((spec.connector_fields ?? []) as ProviderField[], spec),
    [stored, spec],
  )
  const optionNames = Object.keys(spec.options ?? {})
  const proxyTypes: Spec[] = spec.proxy_types ?? []
  const listTypes: Spec[] = proxyTypes.filter((t: Spec) => t.mode === 'list')
  const available: Action[] = [
    ...(proxyTypes.length ? (['proxy_request'] as Action[]) : []),
    ...(spec.validation ? (['validate'] as Action[]) : []),
    ...(optionNames.length ? (['options'] as Action[]) : []),
    ...(listTypes.length ? (['list_proxies'] as Action[]) : []),
  ]
  const [action, setAction] = useState<Action>(available[0] ?? 'proxy_request')
  const [optionName, setOptionName] = useState(optionNames[0] ?? '')
  const [targetUrl, setTargetUrl] = useState('')
  const [credential, setCredential] = useState<FormValues>(() => defaultValues(credentialFields))
  const [connector, setConnector] = useState<FormValues>(() => defaultValues(connectorFields))
  const [result, setResult] = useState<ProviderTestResponse | null>(null)
  useEffect(() => { if (!available.includes(action) && available[0]) setAction(available[0]) }, [available, action])

  // Only the connector inputs the chosen call actually reads are shown. A proxy
  // request needs the whole connector form, since any field may shape the endpoint.
  const neededKeys = useMemo(() => {
    if (action === 'proxy_request') return connectorFields.map((f) => f.key)
    if (action === 'options') return connectorKeysOf(spec.options?.[optionName]?.call)
    if (action === 'list_proxies') {
      const keys = new Set<string>()
      for (const t of listTypes) { for (const k of connectorKeysOf(t.list?.call)) keys.add(k); const cf = String(t.count_field ?? 'connector.num_proxies'); if (cf.startsWith('connector.')) keys.add(cf.slice(10)) }
      if (spec.proxy_type_field?.startsWith('connector.')) keys.add(spec.proxy_type_field.slice(10))
      return [...keys]
    }
    return []
  }, [action, optionName, spec, listTypes, connectorFields])
  const neededFields: ProviderField[] = useMemo(
    () => connectorFields.filter((f) => neededKeys.includes(f.key)),
    [connectorFields, neededKeys],
  )
  const scopes = { credential, connector }
  const credentialConfig = serializeValues(credentialFields, credential, scopes)
  // Optional fields (filters) may stay empty: the server drops empty parameters.
  const missing = neededFields.filter((f) => f.required && !connector[f.key]).map((f) => f.label)

  const pseudoProvider = stored ?? ({ id: spec.id ?? 'draft', name: spec.name ?? 'Draft', egress_hosts: [], has_validation: !!spec.validation } as unknown as ProviderSummary)
  // Dynamic selects on a draft resolve against the in-editor descriptor (admin-only server-side).
  const draftSpec = stored ? undefined : spec
  const healthcheckPlaceholder = proxyTypes.find((t: Spec) => t.healthcheck_url)?.healthcheck_url ?? 'https://httpbin.org/ip'

  const mutation = useMutation({
    mutationFn: () => testProvider(spec.id ?? 'draft', {
      action,
      option_name: action === 'options' ? optionName : undefined,
      target_url: action === 'proxy_request' && targetUrl.trim() ? targetUrl.trim() : undefined,
      credential_config: credentialConfig,
      connector_config: serializeValues(connectorFields, connector, scopes),
      spec: stored ? undefined : spec,
    }),
    onSuccess: setResult,
    onError: (e: Error) => setResult({ ok: false, message: e.message || 'Request failed', result: null, traces: [] }),
  })

  if (!available.length) {
    return (
      <Alert variant="info" className="text-xs">
        There is nothing to test yet. Add a proxy type under <b>Proxy types</b> to send a request through a proxy, or a credential validation or options source under <b>Discovery</b> to exercise the vendor API.
      </Alert>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-fg-muted">
        Try the provider with the values below before creating a credential or connector. Dropdowns load from the vendor as they would in the real forms. Nothing is saved; secrets are redacted in the request log.
      </p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="What to test" help={ACTION_HELP[action]}>
          <Select value={action} onChange={(e) => setAction(e.target.value as Action)} className="px-3 py-1.5 text-sm">
            {available.map((a) => <option key={a} value={a}>{ACTION_LABELS[a]}</option>)}
          </Select>
        </Field>
        {action === 'options' && (
          <Field label="Options source">
            <Select value={optionName} onChange={(e) => setOptionName(e.target.value)} className="px-3 py-1.5 text-sm">
              {optionNames.map((n) => <option key={n} value={n}>{n}</option>)}
            </Select>
          </Field>
        )}
        {action === 'proxy_request' && (
          <TextField label="URL to fetch through the proxy" value={targetUrl} onChange={setTargetUrl} mono placeholder={healthcheckPlaceholder} help="Optional. Defaults to the healthcheck URL. Must be public and HTTPS." />
        )}
      </div>
      <div className="rounded-lg border border-line p-3 space-y-3">
        <div className="text-xs font-semibold">Credential</div>
        {credentialFields.length ? (
          <SchemaForm provider={pseudoProvider} fields={credentialFields} values={credential} onChange={(k, v, fill) => setCredential((p) => ({ ...p, [k]: v, ...(fill ?? {}) }))} scopes={scopes} presets={presets} credentialConfig={credentialConfig} spec={draftSpec} />
        ) : <p className="text-xs text-fg-muted">No credential fields defined yet.</p>}
      </div>
      {neededFields.length > 0 && (
        <div className="rounded-lg border border-line p-3 space-y-3">
          <div className="text-xs font-semibold">{action === 'proxy_request' ? 'Connector' : 'Connector values this call uses'}</div>
          <SchemaForm provider={pseudoProvider} fields={neededFields} values={connector} onChange={(k, v, fill) => setConnector((p) => ({ ...p, [k]: v, ...(fill ?? {}) }))} scopes={scopes} presets={presets} credentialConfig={credentialConfig} spec={draftSpec} columns={2} />
          {action === 'proxy_request' && <p className="text-[11px] text-fg-subtle">Only one proxy is built for the test, whatever the proxy count says.</p>}
        </div>
      )}
      <div className="flex items-center gap-3">
        <Button type="button" size="sm" onClick={() => mutation.mutate()} disabled={mutation.isPending || missing.length > 0}>
          <Play className="w-3.5 h-3.5" /> {mutation.isPending ? 'Running…' : action === 'proxy_request' ? 'Send request' : 'Run test'}
        </Button>
        {missing.length > 0 && <span className="text-xs text-fg-muted">Fill in {missing.join(', ')} first.</span>}
      </div>
      {result && (
        <div className="space-y-3">
          <Alert variant={result.ok ? 'success' : 'error'} className="text-xs">{result.ok ? '✓ ' : ''}{result.message || (result.ok ? 'OK' : 'Failed')}</Alert>
          {action === 'proxy_request' && isProxyRequestResult(result.result) ? (
            <ProxyRequestSummary result={result.result} />
          ) : result.result != null && (
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

function isProxyRequestResult(value: unknown): value is ProxyRequestResult {
  return !!value && typeof value === 'object' && 'proxy' in (value as Record<string, unknown>) && 'target_url' in (value as Record<string, unknown>)
}

/** The endpoint that was built (secrets stay `{placeholders}`) and what came back through it. */
function ProxyRequestSummary({ result }: { result: ProxyRequestResult }) {
  const { proxy } = result
  const endpoint = `${proxy.protocol}://${proxy.username ? `${proxy.username}@` : ''}${proxy.host}:${proxy.port}`
  const metadata = Object.entries(proxy.metadata).filter(([k]) => !['provider', 'proxy_type'].includes(k))
  return (
    <div className="rounded-lg border border-line divide-y divide-line text-xs">
      <Row label="Proxy endpoint"><span className="font-mono break-all">{endpoint}</span></Row>
      {metadata.length > 0 && <Row label="Metadata"><span className="font-mono">{metadata.map(([k, v]) => `${k}=${v}`).join(' · ')}</span></Row>}
      <Row label="Fetched"><span className="font-mono break-all">{result.target_url}</span></Row>
      {result.status != null && (
        <Row label="Response">
          <span className="flex items-center gap-2">
            <Badge color={result.status < 300 ? 'green' : 'yellow'} className="py-0 text-[10px]">{result.status}</Badge>
            {result.elapsed_ms != null && <span className="text-fg-muted">{Math.round(result.elapsed_ms)} ms</span>}
            {result.exit_ip && <span>exit IP <span className="font-mono">{result.exit_ip}</span></span>}
          </span>
        </Row>
      )}
      {result.body != null && result.body !== '' && (
        <div className="px-3 py-2">
          <div className="text-fg-muted mb-1">Body</div>
          <pre className="text-[11px] font-mono bg-surface-raised rounded-lg p-2 overflow-auto max-h-40 whitespace-pre-wrap break-all">{result.body}</pre>
        </div>
      )}
    </div>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 px-3 py-2">
      <span className="w-28 flex-none text-fg-muted">{label}</span>
      <span className="min-w-0 flex-1">{children}</span>
    </div>
  )
}
