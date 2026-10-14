// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Eye, EyeOff, Info, AlertTriangle, Shield, MapPin } from 'lucide-react'
import { ProjectCreate, ProjectUpdate, ProjectSummary, downloadCaCertificate, fetchGeoSettings } from '../api/client'
import type { ConflictRule, ContradictedAction, UnknownAction } from '../api/client'
import { locationExamples, preflightExample } from './locationExamples'
import { RichSelect, RichSelectOption } from './RichSelect'
import { Button, Input, Select, Textarea, Label, Alert, Inspector, Tabs } from './ui'

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

type SettingsTab = 'general' | 'tls' | 'location'

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
  { value: 'inherit', label: 'Install default', description: 'Follow Settings → IP attribution' },
  { value: 'consensus', label: 'Consensus', description: 'Every independent source agrees with each other and disagrees with the vendor. While they disagree among themselves the claim is open: no flag, and the row is routed on the vendor\'s word when unknown exits are trusted, on the top-ranked database when they are refused' },
  { value: 'first', label: 'First', description: 'The top-ranked independent source disagrees with the vendor; the others are not consulted for the flag' },
  { value: 'any', label: 'Any', description: 'One independent source agreeing with the vendor confirms the claim, and routing uses that source\'s answer; none agreeing is a contradiction. Never leaves a claim open while a database answers' },
]

interface ProjectFormProps {
  project?: ProjectSummary
  onSave: (data: ProjectCreate | ProjectUpdate) => void
  error?: string
  /** id used by the footer submit button living outside the form */
  formId: string
}

/**
 * Project settings form body. Rendered inside the docked Inspector on the
 * Overview page and inside a modal on the project selection page (create).
 */
export function ProjectForm({ project, onSave, error, formId }: ProjectFormProps) {
  const isEdit = !!project
  // The install default the Location tab inherits, for the worked example; the form works without it.
  const { data: geo } = useQuery({ queryKey: ['geo-settings'], queryFn: fetchGeoSettings, staleTime: 60_000, retry: false })
  const [formData, setFormData] = useState<ProjectCreate>({
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
    location_conflict_rule: project?.location_conflict_rule ?? undefined,
  })
  const [showPassword, setShowPassword] = useState(false)
  const [activeTab, setActiveTab] = useState<SettingsTab>('general')

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

  return (
    <form id={formId} onSubmit={(e) => { e.preventDefault(); onSave(formData) }} className="space-y-4">
      <Tabs<SettingsTab>
        tabs={[{ id: 'general', label: 'General' }, { id: 'tls', label: 'TLS Interception' }, { id: 'location', label: 'Location' }]}
        active={activeTab}
        onChange={setActiveTab}
      />

      {error && <Alert>{error}</Alert>}

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
            <ModeIcon className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <span>{modeInfo.text}</span>
          </div>
          {currentMode !== 'off' && (
            <p className="text-xs text-warning">
              Clients must install the proxy CA certificate.{' '}
              <button type="button" onClick={() => downloadCaCertificate()} className="underline hover:no-underline font-medium">
                Download CA certificate
              </button>
            </p>
          )}
        </div>
      )}

      {activeTab === 'location' && (
        <div className="space-y-3">
          <div>
            <Label className="text-xs">Databases contradict the vendor</Label>
            <RichSelect options={contradictedOptions} value={formData.location_contradicted ?? 'evidence'} onChange={(v) => setFormData({ ...formData, location_contradicted: v as ContradictedAction })} />
            <p className="mt-1 text-xs text-fg-subtle">
              The independent sources (the IP databases, the echo endpoint) agree with each other and say the exit is somewhere other than the vendor claims.
            </p>
          </div>
          <div>
            <Label className="text-xs">No database knows the exit</Label>
            <RichSelect options={unknownOptions} value={formData.location_unknown ?? 'vendor'} onChange={(v) => setFormData({ ...formData, location_unknown: v as UnknownAction })} />
            <p className="mt-1 text-xs text-fg-subtle">
              Nothing independent places the exit: no database knows the IP, or the databases disagree with each other under the consensus rule. Both decisions apply to every row kind. Fixed exits (static, ISP, datacenter, list and cloud proxies) and pooled residential and mobile slots are judged by health checks and discovery, and the verdict is written on the row. Dynamic-sessions gateways carry no row verdict, since each request gets its own vendor session, so only preflight judges them: for a dynamic connector a refusal does nothing unless preflight is on.
            </p>
          </div>
          <div>
            <label className="flex items-center gap-2 text-[13px]">
              <input type="checkbox" checked={formData.location_preflight ?? false} onChange={(e) => setFormData({ ...formData, location_preflight: e.target.checked })} className="w-4 h-4" />
              Preflight check: verify a session's exit before its first request (one echo per session)
            </label>
            <p className="mt-1 text-xs text-fg-subtle">
              Session rows only: pooled residential and mobile slots, and dynamic-sessions gateways. Fixed exits (static, ISP, datacenter, list and cloud proxies) are placed by discovery and re-read by every health check, so they are never preflighted. Must be on for a refusal to reach dynamic-sessions connectors, and to verify a pooled session's exit per request rather than per health check.
            </p>
          </div>
          <div>
            <Label className="text-xs">Vendor contradicted when</Label>
            <RichSelect options={conflictOptions} value={formData.location_conflict_rule || 'inherit'} onChange={(v) => setFormData({ ...formData, location_conflict_rule: (v === 'inherit' ? (project ? '' : null) : v) as ProjectCreate['location_conflict_rule'] })} />
            <p className="mt-1 text-xs text-fg-subtle">
              The rule decides what a disagreement between databases is. The routing value is the databases in their priority, then the echo endpoint, then the vendor filling only what nothing independent answered; a claim confirmed under any is routed on the agreeing database's answer, and a contradicted one carries the vendor's claim only when you route on the vendor's claim above.
            </p>
          </div>
          {(() => {
            const defaults = geo?.settings
            const rule: ConflictRule = formData.location_conflict_rule || defaults?.default_conflict_rule || 'consensus'
            const contradicted: ContradictedAction = formData.location_contradicted ?? 'evidence'
            const unknown: UnknownAction = formData.location_unknown ?? 'vendor'
            const inherited = !formData.location_conflict_rule
            const noDatabases = geo !== undefined && geo.databases_loaded.length === 0
            const noCityDatabase = geo !== undefined && !noDatabases && !geo.databases_loaded.some((d) => d.kind === 'city')
            const rows = locationExamples(rule, contradicted, unknown)
            const refuses = contradicted === 'refuse' || unknown === 'refuse'
            return (
              <div className="rounded-lg border border-line bg-surface-raised/50 p-3 text-xs leading-relaxed text-fg-muted space-y-2">
                <div className="font-medium text-fg">How this behaves</div>
                <p>
                  A residential slot provisioned for Germany, so the vendor claims DE, judged by two databases (MaxMind ranked above DB-IP) and a plain IP echo.
                  The databases decide the country where they know the IP; the vendor fills in where they do not.
                  Rule: <span className="text-fg">{rule}</span>{inherited && defaults && <span> (inherited from Settings → IP attribution)</span>}; contradicted: <span className="text-fg">{contradicted}</span>; unknown: <span className="text-fg">{unknown}</span>.
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
                <p>
                  <span className="text-fg">Preflight ({formData.location_preflight ? 'on' : 'off'}):</span>{' '}
                  {preflightExample(formData.location_preflight ?? false, contradicted, unknown, formData.routing_strategy ?? 'round_robin', defaults?.preflight_max_attempts ?? 3)}
                </p>
                {refuses && !formData.location_preflight && (
                  <p className="text-warning">Preflight is off, so refusing the proxy acts on pooled residential and mobile slots only through the health check verdict on the row, and does nothing for dynamic-sessions connectors, whose requests carry no row verdict. Turn preflight on to verify a session's exit before its first request.</p>
                )}
                {unknown === 'refuse' && noCityDatabase && (
                  <p className="text-warning">No loaded database knows states or cities, so every -st- or -city- request is refused as not confirmed under this setting. Load a city-level database under Settings → IP attribution, or trust the vendor for unknown exits.</p>
                )}
                {unknown === 'refuse' && noDatabases && (
                  <p className="text-warning">No IP database is loaded, so nothing independent places any exit: every judged row is skipped under this setting, whether the vendor claimed a place for it or not. Load a database under Settings → IP attribution first.</p>
                )}
              </div>
            )
          })()}
          <div className="flex gap-2 p-3 rounded-lg border border-line bg-surface-raised/50 text-xs leading-relaxed text-fg-muted">
            <MapPin className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <span>
              Attribution compares each exit IP against the local IP databases and the echo endpoint (Settings → IP attribution). The routing value is the databases in their priority, then the echo, then the vendor filling only what nothing independent answered; a contradicted level carries the vendor's claim only under <strong>route on the vendor's claim</strong>. Whatever the decisions, a proxy whose resolved country lies outside its connector's declared countries is never routed to, and a row not judged yet is always eligible. The two decisions above say what happens to the verdicts that remain, and apply the same way to the verdict written on a proxy row and to the one preflight reaches for a request. <strong>Refuse the proxy</strong> for an unknown exit needs an IP database loaded, since without one every claim is open. A country pinned by hand is the operator's word: trusted while open, shown as contradicted and kept unless you <strong>refuse the proxy</strong>, since attribution never overwrites a pin. Preflight is the switch that verifies a session's exit before its first request, at the cost of one echo per session; it is the only check that reaches a dynamic-sessions gateway. At preflight, when the client named a place, a contradicted exit is refused unless you route on the vendor's claim, and an exit nobody can place is refused when unknown exits are refused. When the client named no place, a pooled slot is not checked at all: the client has no expectation, and the slot's own claim is judged on the row by every sighting of its exit. A dynamic gateway is: <strong>refuse the proxy</strong> holds the vendor to the exact country it rendered, <strong>route on the evidence</strong> holds the exit to the connector's declared countries, and <strong>route on the vendor's claim</strong> checks placement only. Whether a refused request moves to another proxy is the routing strategy's call: every strategy but sticky tries other eligible proxies, up to the install-wide attempt limit, then 502; a sticky session was promised one exit, so it fails fast with 502. Only <strong>refuse the proxy</strong> on a contradiction replaces a misplaced pooled vendor session; the other two keep it.
            </span>
          </div>
        </div>
      )}
    </form>
  )
}

/** Docked-panel wrapper used by the Overview page. */
export function ProjectPanel({ project, onClose, onSave, isLoading, error }: {
  project: ProjectSummary
  onClose: () => void
  onSave: (data: ProjectUpdate) => void
  isLoading: boolean
  error?: string
}) {
  return (
    <Inspector
      title="Project settings"
      subtitle={project.name}
      onClose={onClose}
      footer={
        <>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="project-form" size="sm" disabled={isLoading}>{isLoading ? 'Saving…' : 'Save changes'}</Button>
        </>
      }
    >
      <ProjectForm key={project.id} project={project} onSave={onSave} error={error} formId="project-form" />
    </Inspector>
  )
}
