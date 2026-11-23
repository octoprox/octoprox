// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Eye, EyeOff, Info, AlertTriangle, Shield, ShieldCheck, MapPin } from 'lucide-react'
import { ProjectCreate, ProjectUpdate, ProjectSummary, downloadCaCertificate, fetchAlertSettings, fetchGeoDatabases, fetchGeoSettings, getExitReputationStrengthPreview } from '../api/client'
import type { AlertSettingsOverrides, ConflictRule, ContradictedAction, ExitReputationMode, UnknownAction } from '../api/client'
import { AlertSettingsForm, draftErrors, draftOf, overridesOf, sameOverrides, type AlertDraft } from './AlertSettingsForm'
import { locationExamples, scenarioSteps, ROW_KIND_FACTS } from './locationExamples'
import type { RowKind, Ask } from './locationExamples'
import { RichSelect, RichSelectOption } from './RichSelect'
import { AdminLink } from './AdminLink'
import { EntityHistory } from './audit/EntityHistory'
import { factorText } from './ConnectorReputation'
import { formatShare } from './TrafficSplit'
import { Button, Input, Select, Textarea, Label, Alert, Inspector, Tabs, Segmented, InfoTip, QueryContent } from './ui'
import { isInUse } from '../utils/geo'
import { DOCS_URL } from '../utils/docs'
import { plural } from '../utils/format'
import { useDebounced } from '../hooks/useDebounced'
import { useToast } from '../contexts/ToastContext'
import { useProjectSettingsSave } from '../hooks/useProjectSettingsSave'

const engineOptions: RichSelectOption[] = [
  { value: 'curl_cffi', label: 'curl_cffi', description: 'C/libcurl - mature, Chrome-grade fingerprinting' },
  { value: 'rnet', label: 'rnet', description: 'Rust/BoringSSL - fast, 113+ browser profiles' },
]

const browserOptions: RichSelectOption[] = [
  { value: 'chrome', label: 'Chrome', description: 'Most common - lowest detection risk' },
  { value: 'firefox', label: 'Firefox', description: 'Alternative fingerprint' },
  { value: 'safari', label: 'Safari', description: 'macOS/iOS fingerprint' },
  { value: 'edge', label: 'Edge', description: 'Chromium-based, Windows-like' },
  { value: 'random', label: 'Random', description: 'Rotate browser per request' },
]

const mitmModeOptions: RichSelectOption[] = [
  { value: 'off', label: 'Disabled', description: 'Traffic tunneled as-is. You handle anti-detection.' },
  { value: 'plain', label: 'Plain', description: 'Inspect headers. Python TLS fingerprint - detectable.', badge: 'DEBUG' },
  { value: 'match_ua', label: 'Browser Match', description: 'Inspect headers. TLS fingerprint matches client User-Agent.', badge: 'PROD' },
  { value: 'override_ua', label: 'Browser Override', description: 'Inspect + override User-Agent. Full fingerprint control.', badge: 'PROD' },
]

const mitmModeInfo: Record<string, { color: string; icon: typeof Info; text: string }> = {
  off: {
    color: 'bg-surface-raised/60 border-line text-fg-muted',
    icon: Info,
    text: 'Traffic is forwarded through an encrypted tunnel as-is. The proxy cannot inspect HTTP headers or content. Your client\'s TLS fingerprint, User-Agent, and all headers reach the target server unchanged. You are responsible for configuring anti-detection measures in your client.',
  },
  plain: {
    color: 'bg-warning-soft border-warning/30 text-warning',
    icon: AlertTriangle,
    text: 'Debug mode. Decrypts HTTPS traffic to inspect HTTP headers, then re-encrypts using Python\'s standard TLS library. The target server sees a Python/OpenSSL TLS fingerprint, which is easily detectable by anti-bot systems. Best for development and debugging - not suitable for production scraping against protected targets.',
  },
  match_ua: {
    color: 'bg-primary-soft border-primary/30 text-primary-soft-fg',
    icon: Shield,
    text: 'Browser-grade TLS fingerprint matching your client\'s User-Agent. If your client sends a Chrome User-Agent, the target sees a Chrome TLS fingerprint (JA3/JA4). Note: this replaces your client\'s original TLS fingerprint with the engine\'s impersonation - the target sees the engine\'s fingerprint, not your client\'s.',
  },
  override_ua: {
    color: 'bg-primary-soft border-primary/30 text-primary-soft-fg',
    icon: Shield,
    text: 'Full fingerprint control. The TLS fingerprint and User-Agent are guaranteed consistent - both match the selected browser profile. Your client\'s original User-Agent is overridden. Best for maximum anti-detection when you don\'t need to control the User-Agent yourself.',
  },
}

/** A tab a link elsewhere can open the project settings on. */
export type ProjectSettingsTab = 'general' | 'tls' | 'location' | 'reputation' | 'alerts'
type SettingsTab = ProjectSettingsTab | 'history'

const contradictedOptions: RichSelectOption[] = [
  { value: 'evidence', label: 'Route on the evidence', description: 'Route the proxy on where the databases or the echo put the exit; the vendor\'s claim is shown as contradicted' },
  { value: 'vendor', label: 'Route on the vendor\'s claim', description: 'Route the proxy on the claimed place anyway and only show the contradiction' },
  { value: 'refuse', label: 'Refuse the proxy', description: 'Skip the proxy; a pooled session slot is replaced with a fresh session' },
]

const unknownOptions: RichSelectOption[] = [
  { value: 'vendor', label: 'Trust the vendor', description: 'Route the proxy on the vendor\'s word while nothing independent places the exit' },
  { value: 'refuse', label: 'Refuse the proxy', description: 'Skip the proxy until a later sighting places the exit. Needs an IP database loaded' },
]

const conflictOptions: RichSelectOption[] = [
  { value: 'inherit', label: 'Install default', description: 'Follow Administration → IP attribution' },
  { value: 'consensus', label: 'Consensus', description: 'Every independent source agrees with each other and disagrees with the vendor. While they disagree among themselves the claim is open: no flag, and the row is routed on the vendor\'s word when unknown exits are trusted, on the top-ranked database when they are refused' },
  { value: 'first', label: 'First', description: 'The top-ranked independent source disagrees with the vendor; the others are not consulted for the flag' },
  { value: 'any', label: 'Any', description: 'One independent source agreeing with the vendor confirms the claim, and routing uses that source\'s answer; none agreeing is a contradiction. Never leaves a claim open while a database answers' },
]

const reputationModeOptions: RichSelectOption[] = [
  { value: 'off', label: 'Off', description: 'Nothing is recorded and exit reports are refused' },
  { value: 'record', label: 'Record', description: 'Reports and the requests they are measured against are kept and shown; routing is unchanged' },
  { value: 'avoid', label: 'Avoid', description: 'Routing for a site skips the IPs avoided there and weighs each connector by its reputation there' },
]

/** What a sticky session whose row's IP is avoided for a site does: the Location tab's Move refused sessions, as a choice. */
const sessionMoveOptions = [
  { value: 'move', label: 'Move', description: 'its next request to the site moves it to another row, which it keeps from then on, for every site' },
  { value: 'refuse', label: 'Refuse', description: 'its requests to the site get 502 until the avoid ends; its requests to other sites keep the row' },
] as const

/** Minutes as the tab's summary reads them: whole hours as hours. */
const minutesText = (minutes: number): string =>
  minutes % 60 === 0 ? `${minutes / 60} ${plural(minutes / 60, 'hour', 'hours')}` : `${minutes} ${plural(minutes, 'minute', 'minutes')}`

/** A whole number held within its bounds as it is typed; empty while it is retyped, which the input's required check catches. */
const boundedInt = (text: string, min: number, max: number): number | undefined =>
  text === '' ? undefined : Math.min(max, Math.max(min, parseInt(text) || min))

/** Whether a field differs from what was loaded; an empty override and none are the same. */
const projectChanged = (loaded: ProjectCreate, now: ProjectCreate): boolean =>
  (Object.keys(now) as (keyof ProjectCreate)[]).some((key) => (now[key] ?? '') !== (loaded[key] ?? ''))

interface ProjectFormProps {
  project?: ProjectSummary
  /**
   * The project's fields, null when an edit changed none of them, and the alert overrides, null when they are as
   * stored (always null for a new project, which starts on the defaults).
   */
  onSave: (data: ProjectCreate | ProjectUpdate | null, alertOverrides: AlertSettingsOverrides | null) => void
  /** Told whether the draft can be saved: the Alert settings tab checks its values as they are typed. */
  onValidityChange?: (valid: boolean) => void
  error?: string
  /** id used by the footer submit button living outside the form */
  formId: string
  /** Told whether the History tab is showing, so the panel can hide its Save and Delete meanwhile. */
  onHistoryChange?: (showing: boolean) => void
  /** A tab a link asked for: shown at once, and again for each new request (a new object). */
  tabRequest?: { tab: ProjectSettingsTab } | null
}

/**
 * Project settings form body. Rendered inside the docked Inspector opened from
 * the project switcher and on the project selection page (create and edit).
 */
export function ProjectForm({ project, onSave, onValidityChange, error, formId, onHistoryChange, tabRequest }: ProjectFormProps) {
  const isEdit = !!project
  const toast = useToast()
  // The install default the Location tab inherits, for the worked example; the form works without it.
  const { data: geo } = useQuery({ queryKey: ['geo-settings'], queryFn: fetchGeoSettings, staleTime: 60_000, retry: false })
  // Which databases are open where requests are judged (the gateways), for the warnings about refusing unknown exits.
  const { data: databases } = useQuery({ queryKey: ['geo-databases'], queryFn: fetchGeoDatabases, staleTime: 60_000, retry: false })
  // The thresholds an edit's Alert settings tab shows, read once the tab is, and what was typed there; null until
  // something is.
  const [activeTab, setActiveTab] = useState<SettingsTab>(tabRequest?.tab ?? 'general')
  const alertQuery = useQuery({
    queryKey: ['alert-settings', project?.id],
    queryFn: () => fetchAlertSettings(project!.id),
    enabled: isEdit && activeTab === 'alerts',
  })
  const [alertDraft, setAlertDraft] = useState<AlertDraft | null>(null)
  const alertResponse = alertQuery.data
  const shownDraft = alertDraft ?? (alertResponse ? draftOf(alertResponse) : null)
  const alertErrors = shownDraft && alertResponse ? draftErrors(shownDraft, alertResponse.defaults) : {}
  const valid = Object.keys(alertErrors).length === 0
  useEffect(() => { onValidityChange?.(valid) }, [valid])
  // What the edit started from, so a save sends the project's fields only when one of them changed.
  const [loaded] = useState<ProjectCreate>(() => ({
    name: project?.name ?? '',
    description: project?.description ?? '',
    username: project?.username ?? '',
    password: project?.password ?? '',
    routing_strategy: project?.routing_strategy ?? 'round_robin',
    tls_mitm_mode: project?.tls_mitm_mode ?? 'off',
    tls_mitm_engine: project?.tls_mitm_engine ?? null,
    tls_mitm_browser: project?.tls_mitm_browser ?? null,
    metrics_retention_days: project?.metrics_retention_days ?? 90,
    location_contradicted: project?.location_contradicted ?? 'evidence',
    location_unknown: project?.location_unknown ?? 'vendor',
    location_preflight: project?.location_preflight ?? false,
    location_session_move: project?.location_session_move ?? false,
    location_conflict_rule: project?.location_conflict_rule ?? undefined,
    location_city_radius_km: project?.location_city_radius_km ?? undefined,
    exit_headers: project?.exit_headers ?? false,
    observation_log: project?.observation_log ?? true,
    observation_retention_days: project?.observation_retention_days ?? undefined,
    exit_reputation: project?.exit_reputation ?? 'off',
    exit_reputation_avoid_minutes: project?.exit_reputation_avoid_minutes ?? 60,
    exit_reputation_half_life_hours: project?.exit_reputation_half_life_hours ?? 24,
    exit_reputation_connector_half_life_requests: project?.exit_reputation_connector_half_life_requests ?? 100,
    exit_reputation_bad_reports: project?.exit_reputation_bad_reports ?? 1,
    exit_reputation_bad_share: project?.exit_reputation_bad_share ?? 50,
    exit_reputation_strength: project?.exit_reputation_strength ?? 1,
    exit_reputation_floor: project?.exit_reputation_floor ?? 10,
  }))
  const [formData, setFormData] = useState<ProjectCreate>(loaded)
  const [showPassword, setShowPassword] = useState(false)
  const changeTab = (tab: SettingsTab) => {
    setActiveTab(tab)
    onHistoryChange?.(tab === 'history')
  }
  // A link asking again while the form is open switches it there, keeping what was typed.
  useEffect(() => { if (tabRequest) changeTab(tabRequest.tab) }, [tabRequest])
  // A refused save says why at the top of the form: bring it into view from however far down the form was scrolled.
  const errorRef = useRef<HTMLDivElement>(null)
  useEffect(() => { if (error) errorRef.current?.scrollIntoView({ block: 'nearest' }) }, [error])
  // The worked example: what the operator has and what a client asks for.
  const [exampleKind, setExampleKind] = useState<RowKind>('pooled')
  const [exampleAsk, setExampleAsk] = useState<Ask>('country')
  const [exampleSession, setExampleSession] = useState(true)
  const [exampleDeclared, setExampleDeclared] = useState(true)

  const handleModeChange = (mode: string) => {
    const updates: Partial<ProjectCreate> = { tls_mitm_mode: mode }
    if (mode === 'off' || mode === 'plain') {
      updates.tls_mitm_engine = null
      updates.tls_mitm_browser = null
    } else if (mode === 'match_ua') {
      updates.tls_mitm_engine = formData.tls_mitm_engine || 'curl_cffi'
      updates.tls_mitm_browser = null
    } else if (mode === 'override_ua') {
      updates.tls_mitm_engine = formData.tls_mitm_engine || 'curl_cffi'
      updates.tls_mitm_browser = formData.tls_mitm_browser || 'chrome'
    }
    setFormData({ ...formData, ...updates })
  }

  const currentMode = formData.tls_mitm_mode || 'off'
  const showEngine = currentMode === 'match_ua' || currentMode === 'override_ua'
  const showBrowser = currentMode === 'override_ua'
  const modeInfo = mitmModeInfo[currentMode] || mitmModeInfo.off
  const ModeIcon = modeInfo.icon

  const reputationMode: ExitReputationMode = formData.exit_reputation ?? 'off'
  const reputationAvoids = reputationMode === 'avoid'
  // The Strength and Lowest factor the preview asks about: the pair once typing pauses, so a first keystroke or a
  // field emptied on the way to a new value asks nothing, and the two never pair up with a stale other.
  const [previewStrength, previewFloor] = useDebounced(`${formData.exit_reputation_strength ?? ''} ${formData.exit_reputation_floor ?? ''}`, 400)
    .split(' ')
    .map((value) => (value ? Number(value) : undefined))

  const submit = () => {
    if (!valid) return
    const data = !isEdit || projectChanged(loaded, formData) ? formData : null
    // Thresholds go only when the draft keeps other overrides than the stored ones.
    let overrides: AlertSettingsOverrides | null = null
    if (alertDraft && alertResponse) {
      const typed = overridesOf(alertDraft, alertResponse.defaults)
      if (!sameOverrides(typed, overridesOf(draftOf(alertResponse), alertResponse.defaults))) overrides = typed
    }
    onSave(data, overrides)
  }

  return (
    <form id={formId} onSubmit={(e) => { e.preventDefault(); submit() }} className="space-y-4">
      <Tabs<SettingsTab>
        tabs={[
          { id: 'general', label: 'General' }, { id: 'tls', label: 'TLS' }, { id: 'location', label: 'Location' },
          { id: 'reputation', label: 'Exit reputation' },
          ...(isEdit ? [{ id: 'alerts' as const, label: 'Alert settings' }, { id: 'history' as const, label: 'History' }] : []),
        ]}
        active={activeTab}
        onChange={changeTab}
        reveal={tabRequest}
        className="pointer-coarse:*:min-h-11"
      />

      {error && activeTab !== 'history' && <Alert ref={errorRef}>{error}</Alert>}

      {activeTab === 'general' && (
        <div className="space-y-4">
          <div>
            <Label>Name</Label>
            <Input type="text" required value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })} placeholder={isEdit ? undefined : 'My Project'} />
          </div>
          <div>
            <Label>Description</Label>
            <Textarea value={formData.description} onChange={(e) => setFormData({ ...formData, description: e.target.value })} rows={2} placeholder={isEdit ? undefined : 'Optional description'} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Routing strategy</Label>
              <Select value={formData.routing_strategy} onChange={(e) => setFormData({ ...formData, routing_strategy: e.target.value })}>
                <option value="round_robin">Round Robin</option>
                <option value="least_used">Least Used</option>
                <option value="random">Random</option>
                <option value="sticky">Sticky</option>
                <option value="health_based">Health Based</option>
              </Select>
            </div>
            <div>
              <Label>Metrics retention (days)</Label>
              <Input type="number" min={0} value={formData.metrics_retention_days ?? 90} onChange={(e) => setFormData({ ...formData, metrics_retention_days: parseInt(e.target.value) || 0 })} />
              <p className="text-xs text-fg-subtle mt-1">0 = keep forever</p>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Observation log</Label>
              <label className="inline-flex items-center gap-2 text-[13px] h-11">
                <input type="checkbox" checked={formData.observation_log ?? true} onChange={(e) => setFormData({ ...formData, observation_log: e.target.checked })} className="w-4 h-4" />
                <span>Keep the log</span>
                <InfoTip label="About the observation log">Every sighting of an exit IP as a row under Exit locations, Observation log. Off, the project's sightings still count in its exit IPs and provider accuracy; only the rows are not kept.</InfoTip>
              </label>
            </div>
            <div>
              <Label>Observation log (days)</Label>
              <Input
                type="number" min={0} max={3650} step={1} className="disabled:bg-surface-raised disabled:cursor-not-allowed"
                disabled={!(formData.observation_log ?? true)}
                value={formData.observation_retention_days ?? ''}
                placeholder={geo ? `Install default (${geo.settings.observation_retention_days || 'forever'})` : 'Install default'}
                onChange={(e) => {
                  // Empty inherits: an edit sends '' to clear the override, a new project sends null.
                  const value = e.target.value === '' ? (project ? '' : null) : Math.min(3650, Math.max(0, parseInt(e.target.value) || 0))
                  setFormData({ ...formData, observation_retention_days: value as ProjectCreate['observation_retention_days'] })
                }}
              />
              <p className="text-xs text-fg-subtle mt-1">Empty follows the install default in <AdminLink page="geo" newTab>IP attribution</AdminLink>; 0 = keep forever</p>
            </div>
          </div>

          <div className="border border-line rounded-lg p-3.5 bg-surface-raised/50">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle mb-2.5">Proxy credentials</h3>
            <p className="text-xs text-fg-muted mb-3">What clients send to authenticate against this project's proxy endpoint.</p>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs">Username</Label>
                <Input type="text" required value={formData.username} onChange={(e) => setFormData({ ...formData, username: e.target.value })} className="font-mono text-sm" placeholder={isEdit ? undefined : 'proxy_user'} />
              </div>
              <div>
                <Label className="text-xs">Password</Label>
                <div className="relative">
                  <Input type={showPassword ? 'text' : 'password'} required value={formData.password} onChange={(e) => setFormData({ ...formData, password: e.target.value })} className="pr-10 font-mono text-sm" placeholder={isEdit ? undefined : '••••••••'} />
                  <button type="button" onClick={() => setShowPassword(!showPassword)} className="absolute right-2 top-1/2 -translate-y-1/2 text-fg-subtle hover:text-fg-muted">
                    {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {activeTab === 'tls' && (
        <div className="space-y-3">
          <div>
            <Label className="text-xs">Mode</Label>
            <RichSelect options={mitmModeOptions} value={currentMode} onChange={handleModeChange} />
          </div>
          {showEngine && (
            <div>
              <Label className="text-xs">TLS engine</Label>
              <RichSelect options={engineOptions} value={formData.tls_mitm_engine || 'curl_cffi'} onChange={(v) => setFormData({ ...formData, tls_mitm_engine: v })} />
            </div>
          )}
          {showBrowser && (
            <div>
              <Label className="text-xs">Browser profile</Label>
              <RichSelect options={browserOptions} value={formData.tls_mitm_browser || 'chrome'} onChange={(v) => setFormData({ ...formData, tls_mitm_browser: v })} />
            </div>
          )}
          <div className={`flex gap-2 p-3 rounded-lg border text-xs leading-relaxed ${modeInfo.color}`}>
            <ModeIcon className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{modeInfo.text}</span>
          </div>
          {currentMode !== 'off' && (
            <p className="text-xs text-warning">
              Clients must install the proxy CA certificate.{' '}
              <button type="button" onClick={() => downloadCaCertificate().catch((e: Error) => toast.show(e.message || 'Failed to download the CA certificate', 'error'))} className="underline hover:no-underline font-medium">
                Download CA certificate
              </button>
            </p>
          )}
        </div>
      )}

      {activeTab === 'location' && (
        <div className="space-y-3">
          <div>
            <Label className="text-xs">
              <span className="inline-flex items-center gap-1.5">
                <span>Databases contradict the vendor</span>
                <InfoTip label="About contradicted claims">The independent sources (IP databases, echo endpoint) agree with each other and put the exit somewhere other than the vendor claims. Applies to the verdict on a proxy row and to a preflight verdict alike.</InfoTip>
              </span>
            </Label>
            <RichSelect options={contradictedOptions} value={formData.location_contradicted ?? 'evidence'} onChange={(v) => setFormData({ ...formData, location_contradicted: v as ContradictedAction })} />
          </div>
          <div>
            <Label className="text-xs">
              <span className="inline-flex items-center gap-1.5">
                <span>No database knows the exit</span>
                <InfoTip label="About unknown exits">Nothing independent places the exit: no database knows the IP, or the databases disagree under the consensus rule. Fixed exits and pooled slots are judged by health checks; a dynamic-sessions gateway only by preflight, so for dynamic connectors this acts only with preflight on.</InfoTip>
              </span>
            </Label>
            <RichSelect options={unknownOptions} value={formData.location_unknown ?? 'vendor'} onChange={(v) => setFormData({ ...formData, location_unknown: v as UnknownAction })} />
          </div>
          <div>
            <label className="inline-flex items-center gap-2 text-[13px]">
              <input type="checkbox" checked={formData.location_preflight ?? false} onChange={(e) => setFormData({ ...formData, location_preflight: e.target.checked })} className="w-4 h-4" />
              <span>Preflight check</span>
              <InfoTip label="About preflight">When on, one echo through the selected upstream before a session's first request: on a pooled slot when the client names a place, on a dynamic-sessions gateway for every session. Fixed exits are never preflighted. When off, dynamic-sessions exits are not observed at all, and the decisions above do not reach dynamic connectors. Also catches a pooled session that moved between health checks.</InfoTip>
            </label>
          </div>
          <div>
            <label className="inline-flex items-center gap-2 text-[13px]">
              <input type="checkbox" checked={formData.location_session_move ?? false} onChange={(e) => setFormData({ ...formData, location_session_move: e.target.checked })} className="w-4 h-4" />
              <span>Move refused sessions</span>
              <InfoTip label="About moving refused sessions">When on, a client session (-sessid-) whose exit preflight refuses gets a new exit instead of a 502, under every strategy: a new vendor session on the same dynamic-sessions connector, which its later requests keep, or another pooled slot, which sticky binds it to. Each move costs one more echo, within the install's preflight attempts. When off, a refused session keeps getting 502 until its verdict expires, unless the strategy moves it to another proxy. Needs preflight on.</InfoTip>
            </label>
          </div>
          <div>
            <label className="inline-flex items-center gap-2 text-[13px]">
              <input type="checkbox" checked={formData.exit_headers ?? false} onChange={(e) => setFormData({ ...formData, exit_headers: e.target.checked })} className="w-4 h-4" />
              <span>Exit headers</span>
              <InfoTip label="About exit headers">When on, clients are told which exit IP served each request, in X-Octoprox-Exit-Ip, -Exit-Source (preflight, cached or last-seen), -Exit-Checked-At, -Proxy-Id and -Connector-Id headers: on the 200 answering a CONNECT, and on a plain HTTP response. A dynamic-sessions exit is known only when preflight echoed the request. See Connecting in the docs.</InfoTip>
            </label>
          </div>
          <div>
            <Label className="text-xs">
              <span className="inline-flex items-center gap-1.5">
                <span>Vendor contradicted when</span>
                <InfoTip label="About the conflict rule">What a disagreement between databases means for the verdict. The routing value is always the databases in their priority, then the echo endpoint, then the vendor filling what nothing independent answered; a claim confirmed under Any routes on the agreeing database.</InfoTip>
              </span>
            </Label>
            <RichSelect options={conflictOptions} value={formData.location_conflict_rule || 'inherit'} onChange={(v) => setFormData({ ...formData, location_conflict_rule: (v === 'inherit' ? (project ? '' : null) : v) as ProjectCreate['location_conflict_rule'] })} />
          </div>
          <div>
            <Label className="text-xs">
              <span className="inline-flex items-center gap-1.5">
                <span>City radius (km)</span>
                <InfoTip label="About the city radius">How far from the claimed city an independent answer may place the exit and still confirm a city claim, so a database naming a neighbouring municipality (Diadema for São Paulo) is not a contradiction. 0 compares city names only. Needs a GeoNames file loaded under Administration → IP attribution; without one cities are compared by name.</InfoTip>
              </span>
            </Label>
            <Input
              type="number" min={0} max={500} step={1} className="w-40"
              value={formData.location_city_radius_km ?? ''}
              placeholder={`Install default (${geo?.settings.default_city_radius_km ?? 25})`}
              onChange={(e) => {
                // Empty inherits: an edit sends '' to clear the override, a new project sends null.
                const value = e.target.value === '' ? (project ? '' : null) : Math.min(500, Math.max(0, parseFloat(e.target.value) || 0))
                setFormData({ ...formData, location_city_radius_km: value as ProjectCreate['location_city_radius_km'] })
              }}
            />
            <p className="text-xs text-fg-subtle mt-1">Empty follows the install default in <AdminLink page="geo" newTab>IP attribution</AdminLink>; a smaller radius is stricter.</p>
          </div>
          {(() => {
            const defaults = geo?.settings
            const rule: ConflictRule = formData.location_conflict_rule || defaults?.default_conflict_rule || 'consensus'
            const contradicted: ContradictedAction = formData.location_contradicted ?? 'evidence'
            const unknown: UnknownAction = formData.location_unknown ?? 'vendor'
            const inherited = !formData.location_conflict_rule
            // A GeoNames file ('places') answers no IP, so it does not count as a database here.
            const inUse = (databases ?? []).filter(isInUse)
            const noDatabases = databases !== undefined && !inUse.some((d) => d.kind !== 'places')
            const noCityDatabase = databases !== undefined && !noDatabases && !inUse.some((d) => d.kind === 'city')
            const rows = locationExamples(rule, contradicted, unknown)
            const refuses = contradicted === 'refuse' || unknown === 'refuse'
            return (
              <div className="rounded-lg border border-line bg-surface-raised/50 p-3 text-xs leading-relaxed text-fg-muted space-y-2">
                <div className="font-medium text-fg">How this behaves</div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                  <span className="inline-flex items-center gap-2"><span className="text-fg-subtle">Proxies</span>
                    <Segmented<RowKind> size="sm" value={exampleKind} onChange={setExampleKind} options={[{ value: 'fixed', label: 'Fixed exits' }, { value: 'pooled', label: 'Pooled slots' }, { value: 'dynamic', label: 'Dynamic sessions' }]} />
                  </span>
                  <span className="inline-flex items-center gap-2"><span className="text-fg-subtle">Client asks for</span>
                    <Segmented<Ask> size="sm" value={exampleAsk} onChange={setExampleAsk} options={[{ value: 'none', label: 'Nothing' }, { value: 'country', label: 'A country' }, { value: 'place', label: 'A state or city' }]} />
                  </span>
                  <label className="inline-flex items-center gap-1.5"><input type="checkbox" className="w-3.5 h-3.5" checked={exampleSession} onChange={(e) => setExampleSession(e.target.checked)} />Client session (-sessid-)</label>
                  <label className="inline-flex items-center gap-1.5"><input type="checkbox" className="w-3.5 h-3.5" checked={exampleDeclared} onChange={(e) => setExampleDeclared(e.target.checked)} />Connector declares countries</label>
                </div>
                <p>
                  Settings in play: rule <span className="text-fg">{rule}</span>{inherited && defaults && <span> (inherited)</span>}, contradicted <span className="text-fg">{contradicted}</span>, unknown <span className="text-fg">{unknown}</span>, preflight <span className="text-fg">{formData.location_preflight ? 'on' : 'off'}</span>, refused sessions <span className="text-fg">{formData.location_session_move ? 'move' : 'stay'}</span>, strategy <span className="text-fg">{formData.routing_strategy ?? 'round_robin'}</span>.
                </p>
                <dl className="space-y-1.5">
                  {scenarioSteps({
                    kind: exampleKind, ask: exampleAsk, session: exampleSession, declared: exampleDeclared,
                    preflight: formData.location_preflight ?? false, contradicted, unknown,
                    strategy: formData.routing_strategy ?? 'round_robin', sessionMove: formData.location_session_move ?? false,
                    maxAttempts: defaults?.preflight_max_attempts ?? 3,
                  }).map((step) => (
                    <div key={step.title} className={step.warn ? 'text-warning' : undefined}>
                      <dt className="inline font-medium text-fg">{step.title}: </dt>
                      <dd className="inline">{step.text}</dd>
                    </div>
                  ))}
                </dl>
                {exampleKind !== 'dynamic' && (
                <>
                <p className="text-fg-subtle">
                  The verdict on a row, for a {exampleKind === 'pooled' ? 'slot provisioned for Germany' : 'fixed exit the vendor lists in Germany'} judged by two databases (MaxMind ranked above DB-IP) and a plain IP echo:
                </p>
                <table className="w-full">
                  <thead>
                    <tr className="text-left text-fg-subtle">
                      <th className="pr-2 font-medium">Evidence</th>
                      <th className="pr-2 font-medium">Verdict</th>
                      <th className="font-medium">Outcome</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.case} className="align-top border-t border-line/60">
                        <td className="pr-2 py-1"><div className="text-fg">{row.case}</div><div className="text-fg-subtle">{row.evidence}</div></td>
                        <td className="pr-2 py-1">{row.verdict}</td>
                        <td className="py-1">{row.outcome}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                </>
                )}
                <details className="pt-1">
                  <summary className="cursor-pointer text-fg-subtle">What matters for which proxies</summary>
                  <table className="w-full mt-1.5">
                    <thead>
                      <tr className="text-left text-fg-subtle">
                        <th className="pr-2 font-medium"></th>
                        <th className="pr-2 font-medium">Fixed exits</th>
                        <th className="pr-2 font-medium">Pooled slots</th>
                        <th className="font-medium">Dynamic sessions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ROW_KIND_FACTS.map((fact) => (
                        <tr key={fact.topic} className="align-top border-t border-line/60">
                          <td className="pr-2 py-1 text-fg">{fact.topic}</td>
                          <td className="pr-2 py-1">{fact.fixed}</td>
                          <td className="pr-2 py-1">{fact.pooled}</td>
                          <td className="py-1">{fact.dynamic}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
                {refuses && !formData.location_preflight && (
                  <p className="text-warning">Preflight is off, so refusing the proxy acts on pooled residential and mobile slots only through the health check verdict on the row, and does nothing for dynamic-sessions connectors, whose requests carry no row verdict. Turn preflight on to verify a session's exit before its first request.</p>
                )}
                {unknown === 'refuse' && noCityDatabase && (
                  <p className="text-warning">No loaded database knows states or cities, so every -st- or -city- request is refused as not confirmed under this setting. Load a city-level database under <AdminLink page="geo" newTab>IP attribution</AdminLink>, or trust the vendor for unknown exits.</p>
                )}
                {unknown === 'refuse' && noDatabases && (
                  <p className="text-warning">No IP database is loaded, so nothing independent places any exit: every judged row is skipped under this setting, whether the vendor claimed a place for it or not. Load a database under <AdminLink page="geo" newTab>IP attribution</AdminLink> first.</p>
                )}
              </div>
            )
          })()}
          <div className="flex gap-2 p-3 rounded-lg border border-line bg-surface-raised/50 text-xs leading-relaxed text-fg-muted">
            <MapPin className="w-4 h-4 shrink-0 mt-0.5" />
            <span>
              The routing value is the databases in their priority, then the echo endpoint, then the vendor's claim where nothing independent answers; the two decisions above say what a contradicted or an open claim does, on the row and at preflight alike. A proxy outside its connector's declared countries is never routed to, a row not judged yet always is, and a hand-pinned country is kept unless you <strong>refuse the proxy</strong> on a contradiction. Refusing unknown exits needs an IP database loaded. Whether a refused request moves is the routing strategy's call: a sticky session fails fast with 502, the other strategies try another proxy up to the install-wide attempt limit. With <strong>Move refused sessions</strong> on, a refused session gets a new exit under every strategy, which a dynamic session keeps and sticky binds a pooled one to. Only <strong>refuse the proxy</strong> on a contradiction replaces a misplaced pooled vendor session.
            </span>
          </div>
        </div>
      )}

      {activeTab === 'reputation' && (
        <div className="space-y-3">
          <p className="text-xs text-fg-muted leading-relaxed">
            Clients tell Octoprox when a site blocked a request by sending an exit report through the API, naming the request by a -tag- in its proxy username or by its exit headers. A site is the exact host a request went to: www.google.com and google.com are two sites.
          </p>
          <div>
            <Label className="text-xs">Mode</Label>
            <RichSelect options={reputationModeOptions} value={reputationMode} onChange={(v) => setFormData({ ...formData, exit_reputation: v as ExitReputationMode })} />
          </div>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(10rem,1fr))] gap-3">
            <div>
              <Label className="text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <span>Avoid for (minutes)</span>
                  <InfoTip label="About Avoid for">How long a reported exit IP stays away from a site. Each further avoid while earlier ones are remembered doubles it, never past 7 days.</InfoTip>
                </span>
              </Label>
              <Input
                type="number" required min={1} max={10080} step={1} className="pointer-coarse:h-11 disabled:bg-surface-raised disabled:cursor-not-allowed"
                disabled={reputationMode === 'off'}
                value={formData.exit_reputation_avoid_minutes ?? ''}
                onChange={(e) => setFormData({ ...formData, exit_reputation_avoid_minutes: boundedInt(e.target.value, 1, 10080) })}
              />
            </div>
            <div>
              <Label className="text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <span>Bad reports to avoid</span>
                  <InfoTip label="About bad reports to avoid">Fresh bad reports an exit IP needs before it is avoided for a site.</InfoTip>
                </span>
              </Label>
              <Input
                type="number" required min={1} max={10} step={1} className="pointer-coarse:h-11 disabled:bg-surface-raised disabled:cursor-not-allowed"
                disabled={reputationMode === 'off'}
                value={formData.exit_reputation_bad_reports ?? ''}
                onChange={(e) => setFormData({ ...formData, exit_reputation_bad_reports: boundedInt(e.target.value, 1, 10) })}
              />
            </div>
            <div>
              <Label className="text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <span>Minimum bad share (%)</span>
                  <InfoTip label="About the minimum bad share">At least this share of the site's recent reports about an IP must be bad. At 50, a bad report after two good ones avoids nothing; clients that report only failures are always at 100.</InfoTip>
                </span>
              </Label>
              <Input
                type="number" required min={1} max={99} step={1} className="pointer-coarse:h-11 disabled:bg-surface-raised disabled:cursor-not-allowed"
                disabled={reputationMode === 'off'}
                value={formData.exit_reputation_bad_share ?? ''}
                onChange={(e) => setFormData({ ...formData, exit_reputation_bad_share: boundedInt(e.target.value, 1, 99) })}
              />
            </div>
          </div>
          {/* The two memories side by side, stacked on a phone, where Connector memory's label would take two lines. */}
          <div className="grid grid-cols-[repeat(auto-fit,minmax(12rem,1fr))] gap-3">
            <div>
              <Label className="text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <span>Remember for (hours)</span>
                  <InfoTip label="About Remember for">For exit IPs: their reports, and the avoids a repeat avoid doubles by, count half after this many hours, so an IP that recovered comes back on its own.</InfoTip>
                </span>
              </Label>
              <Input
                type="number" required min={1} max={168} step={1} className="pointer-coarse:h-11 disabled:bg-surface-raised disabled:cursor-not-allowed"
                disabled={reputationMode === 'off'}
                value={formData.exit_reputation_half_life_hours ?? ''}
                onChange={(e) => setFormData({ ...formData, exit_reputation_half_life_hours: boundedInt(e.target.value, 1, 168) })}
              />
            </div>
            <div>
              <Label className="text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <span>Connector memory (requests)</span>
                  <InfoTip label="About Connector memory">A dynamic connector's block rate for a site and place counts its recent requests there: evidence counts half after this many more requests, and at least half every hour.</InfoTip>
                </span>
              </Label>
              <Input
                type="number" required min={20} max={10000} step={1} className="pointer-coarse:h-11 disabled:bg-surface-raised disabled:cursor-not-allowed"
                disabled={reputationMode === 'off'}
                value={formData.exit_reputation_connector_half_life_requests ?? ''}
                // Raised to 20 only on leaving the field, so 100 can be typed digit by digit.
                onChange={(e) => setFormData({ ...formData, exit_reputation_connector_half_life_requests: boundedInt(e.target.value, 1, 10000) })}
                onBlur={(e) => setFormData({ ...formData, exit_reputation_connector_half_life_requests: boundedInt(e.target.value, 20, 10000) })}
              />
            </div>
          </div>
          {/* How hard dynamic connectors are weighed against the best one: side by side, stacked on a phone. */}
          <div className="grid grid-cols-[repeat(auto-fit,minmax(12rem,1fr))] gap-3">
            <div>
              <Label className="text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <span>Strength</span>
                  <InfoTip label="About Strength">How hard a dynamic connector is weighed against the best one at a place: its success rate is compared with the best's, to this power. 1 is the gentle trim Octoprox always made: a connector blocked 12% of the time keeps 88% of its weight. At 10 it keeps about a third, while the best keeps nearly all of its.</InfoTip>
                </span>
              </Label>
              <Input
                type="number" required min={1} max={20} step={1} className="pointer-coarse:h-11 disabled:bg-surface-raised disabled:cursor-not-allowed"
                disabled={reputationMode === 'off'}
                value={formData.exit_reputation_strength ?? ''}
                onChange={(e) => setFormData({ ...formData, exit_reputation_strength: boundedInt(e.target.value, 1, 20) })}
              />
            </div>
            <div>
              <Label className="text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <span>Lowest factor (%)</span>
                  <InfoTip label="About the lowest factor">A dynamic connector never drops under this share of its weight for a site, so some requests still reach it and show when it recovers. 10 keeps at least ×0.1.</InfoTip>
                </span>
              </Label>
              <Input
                type="number" required min={1} max={100} step={1} className="pointer-coarse:h-11 disabled:bg-surface-raised disabled:cursor-not-allowed"
                disabled={reputationMode === 'off'}
                value={formData.exit_reputation_floor ?? ''}
                onChange={(e) => setFormData({ ...formData, exit_reputation_floor: boundedInt(e.target.value, 1, 100) })}
              />
            </div>
          </div>
          {reputationMode !== 'off' && (formData.exit_reputation_strength ?? 1) > 5 && (formData.exit_reputation_connector_half_life_requests ?? 100) < 1000 && (
            <p className="text-xs text-warning">At this strength a few unlucky requests move a connector's weight a lot. A Connector memory of 1,000 or more steadies it.</p>
          )}
          {project && reputationMode !== 'off' && previewStrength !== undefined && previewFloor !== undefined
            && (previewStrength !== loaded.exit_reputation_strength || previewFloor !== loaded.exit_reputation_floor) && (
            <StrengthPreview
              projectId={project.id} strength={previewStrength} floor={previewFloor}
              saved={`${loaded.exit_reputation_strength} ${loaded.exit_reputation_floor}`}
            />
          )}
          {(() => {
            const minutes = formData.exit_reputation_avoid_minutes ?? 60
            const hours = formData.exit_reputation_half_life_hours ?? 24
            const memory = formData.exit_reputation_connector_half_life_requests ?? 100
            const reports = formData.exit_reputation_bad_reports ?? 1
            const share = formData.exit_reputation_bad_share ?? 50
            const strength = formData.exit_reputation_strength ?? 1
            const floor = (formData.exit_reputation_floor ?? 10) / 100
            return (
              <div className="flex gap-2 p-3 rounded-lg border border-line bg-surface-raised/50 text-xs leading-relaxed text-fg-muted">
                <ShieldCheck className="w-4 h-4 shrink-0 mt-0.5" />
                <span>
                  {reputationAvoids ? '' : 'Once the mode avoids: '}{reports} fresh bad {plural(reports, 'report keeps', 'reports keep')} an exit IP away from a site for {minutesText(minutes)}. Each further avoid doubles that while the earlier ones are remembered, counting half after {hours} {plural(hours, 'hour', 'hours')}, never past 7 days. At least {share}% of the IP's recent reports for the site must be bad, and good reports can end an avoid early. A dynamic connector is weighed for a site by the share of its recent requests there reported blocked: evidence counts half after {memory.toLocaleString()} more {plural(memory, 'request', 'requests')}, and at least half every hour.
                  {strength > 1 && ` At strength ${strength}, dynamic connectors are compared with the best one at each place, never under the lowest factor, ${factorText(floor)}.`}
                </span>
              </div>
            )
          })()}
          <fieldset>
            <legend className="text-xs font-medium text-fg-muted mb-1">
              <span className="inline-flex items-center gap-1.5">
                <span>Move refused sessions</span>
                <InfoTip label="About moving refused sessions">For a sticky session (-sessid-) whose row's IP becomes avoided for a site; other strategies simply use another row. The same setting as on the Location tab.</InfoTip>
              </span>
            </legend>
            {/* Both choices with what each does, a whole line to tap on touch. */}
            <div className="space-y-1">
              {sessionMoveOptions.map((option) => (
                <label key={option.value} className="flex items-start gap-2 py-0.5 text-xs leading-relaxed text-fg-muted cursor-pointer pointer-coarse:min-h-11 pointer-coarse:items-center">
                  <input
                    type="radio" name="exit-reputation-session-move" value={option.value} className="w-4 h-4 mt-0.5 flex-none pointer-coarse:mt-0"
                    checked={(formData.location_session_move ?? false) === (option.value === 'move')}
                    onChange={() => setFormData({ ...formData, location_session_move: option.value === 'move' })}
                  />
                  <span><span className="font-medium text-fg">{option.label}</span>: {option.description}.</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="rounded-lg border border-line p-3 text-xs leading-relaxed text-fg-muted space-y-1.5">
            <div className="font-medium text-fg">What Avoid acts on</div>
            <p><span className="text-fg">Connectors with IPs</span> (static lists, ISP and datacenter ports, cloud instances, pooled slots): an IP avoided for a site is not routed to for that site, and the connector's weight there follows its usable IPs: 12 of 20 usable gives ×0.6.</p>
            <p><span className="text-fg">Dynamic connectors</span>: weighted for a site by their block rate there, per requested location, decided once a minute: 30% of their recent requests reported blocked gives ×0.7{(formData.exit_reputation_strength ?? 1) > 1 ? `, then weighed against the best one at the place at strength ${formData.exit_reputation_strength}` : ''}, never under {factorText((formData.exit_reputation_floor ?? 10) / 100)}. Their IPs are not avoided one by one.</p>
            <p>No preflight is needed: reports name the exit themselves.</p>
          </div>
          <a href={`${DOCS_URL}/exit-reputation`} target="_blank" rel="noreferrer" className="inline-flex items-center text-xs text-primary hover:brightness-110 pointer-coarse:min-h-11">
            How exit reputation works →
          </a>
        </div>
      )}

      {activeTab === 'alerts' && isEdit && (
        <QueryContent query={alertQuery} what="the alert settings">
          {(response) => (
            <AlertSettingsForm
              response={response}
              draft={shownDraft ?? draftOf(response)}
              errors={alertErrors}
              onChange={setAlertDraft}
              metricsRetentionDays={formData.metrics_retention_days ?? 90}
            />
          )}
        </QueryContent>
      )}

      {activeTab === 'history' && project && <EntityHistory entityType="project" entityId={project.id} />}
    </form>
  )
}

/** A share's move as the preview lists it, held on one line from the name's last word: "geonode 47% → 24%". */
const moveText = (name: string, from: number, to: number) => `${name}\u00a0${formatShare(from)}\u00a0→\u00a0${formatShare(to)}`

/**
 * What a draft Strength and Lowest factor change for the site the Reputation tab opens on, at its busiest places, as
 * Avoid would route: only the connectors whose share moves by a point or more. The form passes the pair once typing
 * pauses; ``saved`` keys the answer to the settings it was worked out beside, so one from before a save is not reused.
 */
function StrengthPreview({ projectId, strength, floor, saved }: { projectId: string; strength: number; floor: number; saved: string }) {
  const query = useQuery({
    queryKey: ['exit-reputation-strength-preview', projectId, saved, strength, floor],
    queryFn: () => getExitReputationStrengthPreview(projectId, { strength, floor }),
    // The last draft's answer stays up, dimmed, while the next is worked out.
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
  const data = query.data
  const site = data?.site
  const places = (data?.places ?? []).map((place) => ({
    ...place,
    moved: place.connectors.filter((c) => Math.abs(c.draft_share - c.current_share) >= 1),
  }))
  const still = places.length > 0 && places.every((place) => place.moved.length === 0)
  return (
    <div className={`rounded-lg border border-line p-3 text-xs leading-relaxed text-fg-muted space-y-1.5 ${query.isPlaceholderData ? 'opacity-60' : ''}`} aria-live="polite">
      <div className="font-medium text-fg wrap-anywhere">What this changes{site ? ` for ${site}` : ''}, as Avoid would route</div>
      {query.isError && !data ? (
        <p className="text-danger">Could not work out the preview{query.error instanceof Error && query.error.message ? `: ${query.error.message}` : ''}.</p>
      ) : !data ? (
        <p className="text-fg-subtle">Working out the shares…</p>
      ) : !site || places.length === 0 ? (
        <p>No place has reports{site ? ` for ${site}` : ''} yet, so there is nothing to compare.</p>
      ) : still ? (
        <p>No connector's share moves by a point or more at the {plural(places.length, 'place', `${places.length} busiest places`)} for {site}.</p>
      ) : (
        <ul className="space-y-1">
          {places.map((place) => (
            // Breaks between words, a word only when it cannot fit; a share's move never splits from its name's end.
            <li key={place.place} className="break-words">
              <span className="text-fg">{place.label}</span>:{' '}
              {place.moved.length > 0
                ? place.moved.map((c) => moveText(c.name, c.current_share, c.draft_share)).join(', ')
                : <span className="text-fg-subtle">no share moves by a point</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** Settings of the open project in a docked panel; the shell shows it beside any project page. */
export function ProjectPanel({ project, onClose, tabRequest }: { project: ProjectSummary; onClose: () => void; tabRequest?: { tab: ProjectSettingsTab } | null }) {
  // Which project's History is showing: the form starts on General again for another project.
  const [historyOf, setHistoryOf] = useState<string | null>(null)
  const [valid, setValid] = useState(true)
  const save = useProjectSettingsSave(project, { onSaved: onClose })
  return (
    <Inspector
      title="Project settings"
      subtitle={`${project.name} · only this project`}
      onClose={onClose}
      footer={historyOf === project.id ? undefined : (
        <>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="project-form" size="sm" disabled={save.isPending || !valid}>{save.isPending ? 'Saving…' : 'Save changes'}</Button>
        </>
      )}
    >
      <ProjectForm
        key={project.id}
        project={project}
        onSave={(data, alerts) => save.mutate({ data, alerts })}
        onValidityChange={setValid}
        error={save.error?.message}
        formId="project-form"
        onHistoryChange={(showing) => setHistoryOf(showing ? project.id : null)}
        tabRequest={tabRequest}
      />
    </Inspector>
  )
}
