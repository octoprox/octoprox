// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Eye, EyeOff, Info, AlertTriangle, Shield, MapPin } from 'lucide-react'
import { ProjectCreate, ProjectUpdate, ProjectSummary, downloadCaCertificate } from '../api/client'
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

const locationPolicyOptions: RichSelectOption[] = [
  { value: 'off', label: 'Ignore', description: 'Route on the resolved country; contradictions are only recorded' },
  { value: 'warn', label: 'Warn', description: 'Flag contradicted proxies in the UI, keep routing to them' },
  { value: 'strict', label: 'Strict', description: 'Contradicted proxies are not eligible for this project\'s requests' },
]

const preflightOptions: RichSelectOption[] = [
  { value: 'off', label: 'Off', description: 'No check before forwarding' },
  { value: 'report', label: 'Report', description: 'Verify the exit once per session, record the result, forward anyway' },
  { value: 'retry', label: 'Retry', description: 'On a mismatch try other eligible proxies first; an already bound explicit session is rejected instead' },
  { value: 'reject', label: 'Reject', description: 'Verify once per session; answer 502 when the exit is not where the client asked for' },
]

/** Named source orders, so the form stays a select rather than a drag list. */
const sourcePresets: { value: string; label: string; description: string; sources: string[] | null }[] = [
  { value: 'inherit', label: 'Install default', description: 'Follow Settings → IP attribution', sources: null },
  { value: 'database', label: 'Databases only', description: 'Only local IP databases decide; vendor claims and echo answers are evidence, never the answer', sources: ['database'] },
  { value: 'database,endpoint', label: 'Databases, then echo', description: 'Ignore vendor claims for routing. The echo endpoint can only decide when it reports a country; a plain IP echo never does.', sources: ['database', 'endpoint'] },
  { value: 'database,vendor,endpoint', label: 'Databases, vendor, echo', description: 'Databases first, the vendor when they have no answer, the echo endpoint last and only if it reports a country', sources: ['database', 'vendor', 'endpoint'] },
  { value: 'vendor,database,endpoint', label: 'Vendor first', description: 'Trust the vendor; databases fill the gaps and still flag contradictions', sources: ['vendor', 'database', 'endpoint'] },
]

const conflictOptions: RichSelectOption[] = [
  { value: 'inherit', label: 'Install default', description: 'Follow Settings → IP attribution' },
  { value: 'consensus', label: 'Consensus', description: 'Every independent source agrees with each other and disagrees with the vendor' },
  { value: 'first', label: 'First', description: 'The top-ranked independent source disagrees with the vendor' },
]

function presetFor(sources: string[] | null | undefined): string {
  if (!sources || sources.length === 0) return 'inherit'
  const key = sources.join(',')
  return sourcePresets.some((p) => p.value === key) ? key : 'custom'
}

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
    location_policy: project?.location_policy ?? 'off',
    location_preflight: project?.location_preflight ?? 'off',
    location_sources: project?.location_sources ?? undefined,
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
            <Label className="text-xs">Contradicted vendor locations</Label>
            <RichSelect options={locationPolicyOptions} value={formData.location_policy ?? 'off'} onChange={(v) => setFormData({ ...formData, location_policy: v as ProjectCreate['location_policy'] })} />
          </div>
          <div>
            <Label className="text-xs">Preflight check</Label>
            <RichSelect options={preflightOptions} value={formData.location_preflight ?? 'off'} onChange={(v) => setFormData({ ...formData, location_preflight: v as ProjectCreate['location_preflight'] })} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="text-xs">Which source decides</Label>
              <RichSelect
                options={[
                  ...sourcePresets.map((p) => ({ value: p.value, label: p.label, description: p.description })),
                  ...(presetFor(formData.location_sources) === 'custom' ? [{ value: 'custom', label: 'Custom', description: (formData.location_sources ?? []).join(', ') }] : []),
                ]}
                value={presetFor(formData.location_sources)}
                onChange={(v) => {
                  const preset = sourcePresets.find((p) => p.value === v)
                  if (preset) setFormData({ ...formData, location_sources: (preset.sources ?? []) as ProjectCreate['location_sources'] })
                }}
              />
            </div>
            <div>
              <Label className="text-xs">Vendor contradicted when</Label>
              <RichSelect options={conflictOptions} value={formData.location_conflict_rule || 'inherit'} onChange={(v) => setFormData({ ...formData, location_conflict_rule: (v === 'inherit' ? (project ? '' : null) : v) as ProjectCreate['location_conflict_rule'] })} />
            </div>
          </div>
          <div className="flex gap-2 p-3 rounded-lg border border-line bg-surface-raised/50 text-xs leading-relaxed text-fg-muted">
            <MapPin className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <span>
              Attribution compares each exit IP against the local IP databases and the echo endpoint (Settings → IP attribution). A proxy whose vendor-declared country is contradicted is flagged; <strong>strict</strong> keeps it out of this project's routing. Preflight verifies a session's exit before its first request is forwarded, at the cost of one extra round trip per session; <strong>retry</strong> moves to another proxy on a mismatch and <strong>reject</strong> answers 502. A misplaced vendor session is rotated either way.
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
