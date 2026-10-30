// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Download, Eye, EyeOff, RefreshCw } from 'lucide-react'
import { downloadCaCertificate, fetchConnectInfo, type ConnectInfo, type ConnectParams } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useToast } from '../contexts/ToastContext'
import { useDebounced } from '../hooks/useDebounced'
import { Page } from '../components/layout/Page'
import { Alert, Button, Card, CardHeader, CopyButton, InfoTip, Input, Label, Segmented, Select, Tabs } from '../components/ui'
import {
  COUNTRY_CODES, SNIPPETS, US_STATES, countryLabel, effectivePort, formatEndpoint, needsEncoding, omitsPort, parseEndpoint,
  proxyLine, proxyUrl, randomSessionId, snippet, type ProxyAccess, type ProxyEndpoint, type SnippetKind,
} from '../utils/connect'

const SNIPPET_KEY = 'octoprox_connect_snippet'
const DEFAULT_TARGET = 'https://ipinfo.io/json'
const MASK = '********'
const CONTROL = 'px-3 text-[13px]'

const STRATEGY_LABELS: Record<string, string> = {
  round_robin: 'Round Robin', least_used: 'Least Used', random: 'Random', sticky: 'Sticky', health_based: 'Health Based',
}

/**
 * One labelled value with a copy button; `copy` is what the clipboard gets when the shown value is masked,
 * and `note` a short remark on a line of its own under the value.
 */
function Field({ label, value, copy, extra, note }: { label: ReactNode; value: string; copy?: string; extra?: ReactNode; note?: ReactNode }) {
  return (
    <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 items-center px-4 py-2 border-b border-line last:border-b-0">
      <span className="text-xs text-fg-muted">{label}</span>
      <div className="flex items-center gap-1 min-w-0">
        <span className="flex-1 min-w-0">
          <span className="block font-mono text-[12.5px] text-fg break-all">{value}</span>
          {note && <span className="block text-xs text-fg-subtle">{note}</span>}
        </span>
        {extra}
        <CopyButton text={copy ?? value} label={`Copy ${typeof label === 'string' ? label.toLowerCase() : 'value'}`} />
      </div>
    </div>
  )
}

/** A city slug as words, for prose: `new_york` reads New York. */
const cityWords = (slug: string) => slug.split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')

function placeText(location: ConnectInfo['location']): string {
  if (!location?.country) return 'this request'
  return [location.city && cityWords(location.city), location.state, countryLabel(location.country)].filter(Boolean).join(', ')
}

/** What the city field becomes without the server's aliases, so a note only shows when the server changed more than case and spacing. */
const plainSlug = (text: string) => text.trim().toLowerCase().replace(/[\s-]+/g, '_')

/**
 * Builds what a client needs to use this project's proxy: the endpoint, the
 * username with the place and session it asks for, and ready-made snippets.
 * The server reads the username exactly as the proxy listener does, so the
 * normalised form, any refusal and the connectors it would reach are the
 * real thing rather than a guess made here.
 */
export default function ConnectPage() {
  const { selectedProjectId, selectedProject } = useProject()
  const toast = useToast()
  const navigate = useNavigate()
  const [country, setCountry] = useState('')
  const [state, setState] = useState('')
  const [city, setCity] = useState('')
  const [sticky, setSticky] = useState(false)
  const [sessid, setSessid] = useState(() => randomSessionId())
  // The endpoint as typed over the default, or null while it is the default.
  const [endpointText, setEndpointText] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const [target, setTarget] = useState(DEFAULT_TARGET)
  const [kind, setKind] = useState<SnippetKind>(() => {
    try {
      const saved = localStorage.getItem(SNIPPET_KEY)
      if (SNIPPETS.some((s) => s.id === saved)) return saved as SnippetKind
    } catch { /* storage unavailable */ }
    return 'curl'
  })
  const chooseKind = (next: SnippetKind) => {
    setKind(next)
    try { localStorage.setItem(SNIPPET_KEY, next) } catch { /* storage unavailable */ }
  }

  const params = useDebounced<ConnectParams>(useMemo(() => ({
    country: country || undefined,
    state: country && state.trim() ? state.trim() : undefined,
    city: country && city.trim() ? city.trim() : undefined,
    sessid: sticky && sessid.trim() ? sessid.trim() : undefined,
  }), [country, state, city, sticky, sessid]), 250)

  const { data: info, isPlaceholderData, isError } = useQuery({
    queryKey: ['connect', selectedProjectId, params],
    queryFn: () => fetchConnectInfo(selectedProjectId!, params),
    enabled: !!selectedProjectId,
    // Keep the last answer while the place is retyped, never across projects: another
    // project's username would show (and copy) with this project's password.
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[1] === selectedProjectId ? previous : undefined),
    refetchInterval: 15000, // which connectors serve a place moves with proxy health
  })

  // Countries the project serves first, then the rest; the place pickers suggest where fixed exits are.
  const countryGroups = useMemo(() => {
    const byName = (a: string, b: string) => countryLabel(a).localeCompare(countryLabel(b))
    const served = [...(info?.countries ?? [])].sort(byName)
    const rest = COUNTRY_CODES.filter((c) => !served.includes(c)).sort(byName)
    return { served, rest }
  }, [info?.countries])
  const places = useMemo(() => (info?.places ?? []).filter((p) => p.country === country), [info?.places, country])
  const stateSuggestions = useMemo(() => [...new Set(places.map((p) => p.state).filter((s): s is string => !!s))], [places])
  const citySuggestions = useMemo(() => {
    const wanted = state.trim().toUpperCase()
    return [...new Set(places.filter((p) => !wanted || p.state === wanted).map((p) => p.city).filter((c): c is string => !!c))]
  }, [places, state])

  if (!selectedProjectId || !selectedProject) return null

  const strategy = selectedProject.routing_strategy
  const mitm = selectedProject.tls_mitm_mode !== 'off'
  const defaultEndpoint: ProxyEndpoint = {
    scheme: info?.endpoint.scheme ?? 'http',
    host: info?.endpoint.host ?? window.location.hostname,
    port: info ? info.endpoint.port : 8080,
  }
  const typedEndpoint = endpointText === null ? null : parseEndpoint(endpointText)
  const endpointInvalid = endpointText !== null && typedEndpoint === null
  const access: ProxyAccess = {
    ...(typedEndpoint ?? defaultEndpoint),
    username: info?.username ?? selectedProject.username,
    password: selectedProject.password,
    mitm,
  }
  const shown: ProxyAccess = showPassword ? access : { ...access, password: MASK }
  const passwordToggle = (
    <button type="button" onClick={() => setShowPassword((v) => !v)} title={showPassword ? 'Hide password' : 'Show password'} className="flex-none p-1 rounded-sm text-fg-subtle hover:text-fg hover:bg-surface-raised">
      {showPassword ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
    </button>
  )
  const sentCity = info?.location?.city
  const cityRewritten = !!sentCity && !!city.trim() && sentCity !== plainSlug(city)

  const changeCountry = (next: string) => {
    setCountry(next)
    // A state code means nothing in another country, and a city rarely survives the move.
    setState('')
    setCity('')
  }

  return (
    <Page
      title="Connect"
      subtitle="The proxy address, credentials and code a client needs, for any place and session this project serves."
    >
      <div className="grid grid-cols-1 @4xl:grid-cols-[minmax(0,360px)_minmax(0,1fr)] gap-4 items-start">
        {/* What the client asks for */}
        <Card className="p-4 space-y-4">
          <div className="space-y-3">
            <h2 className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle">Location</h2>
            <div>
              <Label htmlFor="connect-country" className="text-xs">Country</Label>
              <Select id="connect-country" value={country} onChange={(e) => changeCountry(e.target.value)} className={CONTROL}>
                <option value="">Any country</option>
                {countryGroups.served.length > 0 && (
                  <optgroup label="Served by this project">
                    {countryGroups.served.map((c) => <option key={c} value={c}>{countryLabel(c)} ({c.toLowerCase()})</option>)}
                  </optgroup>
                )}
                <optgroup label={info?.any_country ? 'Other countries, served on request' : 'Not listed by any connector'}>
                  {countryGroups.rest.map((c) => <option key={c} value={c}>{countryLabel(c)} ({c.toLowerCase()})</option>)}
                </optgroup>
              </Select>
            </div>
            <div>
              <Label htmlFor="connect-state" className="text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <span>State</span>
                  <InfoTip label="About states">The country's first-level subdivision, whatever it is called locally: a US state, a Canadian province, a German Land, England (eng). Most vendors name US states only; fixed exits match the state they are known to be in.</InfoTip>
                </span>
              </Label>
              {country === 'US' ? (
                <Select id="connect-state" value={state} onChange={(e) => setState(e.target.value)} className={CONTROL}>
                  <option value="">Any state</option>
                  {US_STATES.map(([code, name]) => <option key={code} value={code}>{name} ({code.toLowerCase()})</option>)}
                </Select>
              ) : (
                <>
                  <Input
                    id="connect-state" value={state} onChange={(e) => setState(e.target.value)} disabled={!country}
                    list="connect-state-options" className={CONTROL}
                    placeholder={country ? 'Any state, or a code such as eng' : 'Pick a country first'}
                  />
                  <datalist id="connect-state-options">
                    {stateSuggestions.map((s) => <option key={s} value={s.toLowerCase()} />)}
                  </datalist>
                </>
              )}
            </div>
            <div>
              <Label htmlFor="connect-city" className="text-xs">City</Label>
              <Input
                id="connect-city" value={city} onChange={(e) => setCity(e.target.value)} disabled={!country}
                list="connect-city-options" className={CONTROL}
                placeholder={country ? 'Any city, or a name such as New York' : 'Pick a country first'}
              />
              <datalist id="connect-city-options">
                {citySuggestions.map((c) => <option key={c} value={c} />)}
              </datalist>
              {cityRewritten && <p className="text-xs text-fg-subtle mt-1">Sent as <span className="font-mono text-fg">{sentCity}</span></p>}
            </div>
          </div>

          <div className="space-y-2 border-t border-line pt-4">
            <h2 className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle">Session</h2>
            <Segmented<'rotating' | 'sticky'>
              size="sm" value={sticky ? 'sticky' : 'rotating'} onChange={(v) => setSticky(v === 'sticky')}
              options={[{ value: 'rotating', label: 'Rotating' }, { value: 'sticky', label: 'Sticky session' }]}
            />
            {sticky && (
              <div className="flex gap-2">
                <Input value={sessid} onChange={(e) => setSessid(e.target.value)} className={`${CONTROL} font-mono`} placeholder="Session ID" aria-label="Session ID" />
                <Button type="button" variant="outline" size="sm" className="h-9" onClick={() => setSessid(randomSessionId())} title="New session ID">
                  <RefreshCw className="w-3.5 h-3.5" /> New
                </Button>
              </div>
            )}
            <p className="text-xs text-fg-muted leading-relaxed">
              {!sticky
                ? 'Every request is routed on its own: the strategy picks a proxy each time, and a dynamic-sessions connector gets a fresh vendor session, so the exit IP changes.'
                : strategy === 'sticky'
                  ? 'Requests with this session ID stay on one proxy while it is healthy, and a dynamic-sessions connector keeps one vendor session for them. Change the ID to get a new exit.'
                  : `The project routes with ${STRATEGY_LABELS[strategy] ?? strategy}, so only a dynamic-sessions connector holds the exit for this ID, as one vendor session. Set the project's strategy to Sticky to keep every request of a session on one proxy.`}
            </p>
          </div>

          <div className="space-y-2 border-t border-line pt-4">
            <h2 className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle flex items-center gap-1.5">
              Proxy endpoint
              <InfoTip label="About the endpoint">{info?.endpoint.host
                ? 'Set by the administrator (server.proxy_public_url). Edit it here to try another address, such as https://proxy.example.com:8443; nothing is saved.'
                : 'The host you reached this page at, over plain HTTP on the proxy port: right when one process serves both, wrong when the gateways have an address of their own. An administrator sets the address clients should use with server.proxy_public_url, for example https://proxy.example.com behind a load balancer that terminates TLS. Edits here are not saved.'}
              </InfoTip>
            </h2>
            <Input
              value={endpointText ?? formatEndpoint(defaultEndpoint)} onChange={(e) => setEndpointText(e.target.value)}
              className={`${CONTROL} font-mono`} aria-label="Proxy endpoint" aria-invalid={endpointInvalid}
              placeholder="https://proxy.example.com"
            />
            {endpointInvalid && (
              <p className="text-xs text-warning">Not an address like https://proxy.example.com:8443, so the examples use {formatEndpoint(defaultEndpoint)}.</p>
            )}
            {endpointText !== null && (
              <button type="button" onClick={() => setEndpointText(null)} className="text-xs text-primary hover:brightness-110">Reset to {formatEndpoint(defaultEndpoint)}</button>
            )}
          </div>
        </Card>

        <div className="flex flex-col gap-4 min-w-0">
          {/* What the client sends */}
          <Card className="overflow-hidden">
            <div className="px-4 pt-3.5 pb-2.5"><CardHeader title="Credentials" /></div>
            <div className="border-t border-line">
              <Field label="Protocol" value={access.scheme.toUpperCase()} />
              <Field label="Host" value={access.host} />
              <Field
                label="Port" value={String(effectivePort(access))}
                note={omitsPort(access) ? 'The https default, left out of URLs' : undefined}
              />
              <Field label="Username" value={access.username} />
              <Field label="Password" value={shown.password} copy={access.password} extra={passwordToggle} />
              <Field label="Proxy URL" value={proxyUrl(shown)} copy={proxyUrl(access)} />
              <Field label={<span className="inline-flex items-center gap-1">List format <InfoTip label="About the list format">host:port:username:password, the line proxy lists and many scraping tools import.{access.scheme === 'https' ? ' It carries no protocol, so choose HTTPS in the tool.' : ''}</InfoTip></span>} value={proxyLine(shown)} copy={proxyLine(access)} />
            </div>
            {needsEncoding(access) && (
              <p className="px-4 py-2 border-t border-line text-xs text-fg-muted">The proxy URL percent-encodes characters of the username or password that would otherwise break it; clients decode them before sending.</p>
            )}
          </Card>

          {/* What the proxy makes of it */}
          <Card className="p-4">
            <CardHeader
              title="Where this username goes"
              action={isPlaceholderData ? <span className="text-xs text-fg-subtle">Checking…</span> : undefined}
              className="mb-2.5"
            />
            {!info ? (
              isError
                ? <Alert variant="error" className="text-xs">Could not check where this username goes. Try again in a moment.</Alert>
                : <div className="h-12 rounded-lg bg-surface-raised animate-pulse" />
            ) : info.error ? (
              <Alert variant="error" className="text-xs">The proxy refuses this username with 400 Bad Request: {info.error}.</Alert>
            ) : info.routes.length === 0 ? (
              <Alert variant="warning" className="text-xs flex gap-2">
                <AlertTriangle className="w-4 h-4 flex-none mt-0.5" />
                <span>
                  Nothing in this project serves {placeText(info.location)} right now, so the proxy refuses it: 502 Bad Gateway, or 429 while the proxies that could serve it are rate limited, or the traffic-limit status (509 by default) while their connectors are over their limit.{' '}
                  {info.location?.state || info.location?.city
                    ? 'A state or city is served by dynamic-sessions connectors whose vendor can name it, and by fixed exits known to be there; a request is never widened to the country.'
                    : info.location?.country
                      ? 'List the country on a connector, or add one that serves any country.'
                      : 'No connector has a healthy proxy that can take requests.'}
                </span>
              </Alert>
            ) : (
              <>
                <ul className="divide-y divide-line">
                  {info.routes.map((route) => (
                    <li key={route.connector_id} className="flex items-center gap-3 py-2 text-[13px]">
                      <button type="button" onClick={() => navigate(`/projects/${selectedProjectId}/connectors?open=${route.connector_id}`)} className="font-medium text-fg hover:text-primary truncate text-left min-w-0">
                        {route.connector_name}
                      </button>
                      <span className="text-xs text-fg-muted flex-1 truncate">
                        {route.on_demand
                          ? 'creates sessions for the country on the first request'
                          : route.dynamic
                            ? 'dynamic sessions: the place is rendered into each vendor session'
                            : `${route.proxies} ${route.proxies === 1 ? 'proxy' : 'proxies'}`}
                      </span>
                      <span className="text-xs tabular-nums text-fg-muted flex-none" title="Expected share of requests, by connector weight">{Math.round(route.share * 100)}%</span>
                    </li>
                  ))}
                </ul>
                <p className="text-xs text-fg-subtle mt-2">Shares follow the connectors' weights. A connector with a domain filter only takes the hosts it allows.</p>
              </>
            )}
          </Card>

          {/* How to send it */}
          <Card className="overflow-hidden">
            <div className="px-4 pt-3 flex items-center justify-between gap-3 flex-wrap">
              <CardHeader title="Examples" />
              <label className="flex items-center gap-2 text-xs text-fg-muted min-w-[240px] flex-1 max-w-sm">
                <span className="flex-none">Test URL</span>
                <Input value={target} onChange={(e) => setTarget(e.target.value)} className="h-8 text-xs font-mono" aria-label="Test URL" />
              </label>
            </div>
            <Tabs<SnippetKind> tabs={SNIPPETS} active={kind} onChange={chooseKind} size="sm" className="px-3 mt-2" />
            {mitm && (
              <div className="px-4 pt-3">
                <Alert variant="info" className="text-xs flex items-center gap-2 flex-wrap">
                  <span className="flex-1 min-w-0">TLS interception is on for this project, so clients must trust the Octoprox CA. The examples expect it saved as octoprox-ca.crt.</span>
                  <Button type="button" size="sm" variant="outline" onClick={() => downloadCaCertificate().catch((e: Error) => toast.show(e.message || 'Failed to download the CA certificate', 'error'))}><Download className="w-3.5 h-3.5" /> CA certificate</Button>
                </Alert>
              </div>
            )}
            <div className="relative p-4">
              <pre className="text-[12px] leading-relaxed font-mono bg-surface-raised rounded-lg p-3 pr-10 overflow-x-auto whitespace-pre">{snippet(kind, shown, target.trim() || DEFAULT_TARGET)}</pre>
              <div className="absolute top-5 right-5">
                <CopyButton text={snippet(kind, access, target.trim() || DEFAULT_TARGET)} label="Copy example" />
              </div>
            </div>
            <p className="px-4 pb-4 -mt-1 text-xs text-fg-subtle">
              {target.trim() === DEFAULT_TARGET
                ? 'ipinfo.io answers with the exit IP and where it is, so you can see the location take effect. '
                : ''}
              {showPassword ? 'The examples include the password.' : 'The password is hidden here; copied examples include it.'}
            </p>
          </Card>
        </div>
      </div>
    </Page>
  )
}
