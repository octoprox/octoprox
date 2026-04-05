// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Eye, EyeOff, Info, AlertTriangle, Shield } from 'lucide-react'
import { ProjectCreate, ProjectUpdate, ProjectSummary, downloadCaCertificate } from '../api/client'
import { RichSelect, RichSelectOption } from './RichSelect'
import { Button, Input, Select, Textarea, Label, Modal, ModalHeader, ModalFooter, Alert } from './ui'

interface ProjectModalProps {
  project?: ProjectSummary
  onClose: () => void
  onSave: (data: ProjectCreate | ProjectUpdate) => void
  isLoading: boolean
  error?: string
}

const engineOptions: RichSelectOption[] = [
  { value: 'curl_cffi', label: 'curl_cffi', description: 'C/libcurl — mature, Chrome-grade fingerprinting' },
  { value: 'rnet', label: 'rnet', description: 'Rust/BoringSSL — fast, 113+ browser profiles' },
]

const browserOptions: RichSelectOption[] = [
  { value: 'chrome', label: 'Chrome', description: 'Most common — lowest detection risk' },
  { value: 'firefox', label: 'Firefox', description: 'Alternative fingerprint' },
  { value: 'safari', label: 'Safari', description: 'macOS/iOS fingerprint' },
  { value: 'edge', label: 'Edge', description: 'Chromium-based, Windows-like' },
  { value: 'random', label: 'Random', description: 'Rotate browser per request' },
]

const mitmModeOptions: RichSelectOption[] = [
  { value: 'off', label: 'Disabled', description: 'Traffic tunneled as-is. You handle anti-detection.' },
  { value: 'plain', label: 'Plain', description: 'Inspect headers. Python TLS fingerprint — detectable.', badge: 'DEBUG' },
  { value: 'match_ua', label: 'Browser Match', description: 'Inspect headers. TLS fingerprint matches client User-Agent.', badge: 'PROD' },
  { value: 'override_ua', label: 'Browser Override', description: 'Inspect + override User-Agent. Full fingerprint control.', badge: 'PROD' },
]

const mitmModeInfo: Record<string, { color: string; icon: typeof Info; text: string }> = {
  off: {
    color: 'bg-gray-50 dark:bg-gray-700/30 border-gray-200 dark:border-gray-600 text-gray-600 dark:text-gray-400',
    icon: Info,
    text: 'Traffic is forwarded through an encrypted tunnel as-is. The proxy cannot inspect HTTP headers or content. Your client\'s TLS fingerprint, User-Agent, and all headers reach the target server unchanged. You are responsible for configuring anti-detection measures in your client.',
  },
  plain: {
    color: 'bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-700 text-amber-700 dark:text-amber-300',
    icon: AlertTriangle,
    text: 'Debug mode. Decrypts HTTPS traffic to inspect HTTP headers, then re-encrypts using Python\'s standard TLS library. The target server sees a Python/OpenSSL TLS fingerprint, which is easily detectable by anti-bot systems. Best for development and debugging — not suitable for production scraping against protected targets.',
  },
  match_ua: {
    color: 'bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-700 text-blue-700 dark:text-blue-300',
    icon: Shield,
    text: 'Browser-grade TLS fingerprint matching your client\'s User-Agent. If your client sends a Chrome User-Agent, the target sees a Chrome TLS fingerprint (JA3/JA4). Note: this replaces your client\'s original TLS fingerprint with the engine\'s impersonation — the target sees the engine\'s fingerprint, not your client\'s.',
  },
  override_ua: {
    color: 'bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-700 text-blue-700 dark:text-blue-300',
    icon: Shield,
    text: 'Full fingerprint control. The TLS fingerprint and User-Agent are guaranteed consistent — both match the selected browser profile. Your client\'s original User-Agent is overridden. Best for maximum anti-detection when you don\'t need to control the User-Agent yourself.',
  },
}

type SettingsTab = 'general' | 'tls'

export default function ProjectModal({
  project,
  onClose,
  onSave,
  isLoading,
  error,
}: ProjectModalProps) {
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

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    onSave(formData)
  }

  const currentMode = formData.tls_mitm_mode || 'off'
  const showEngine = currentMode === 'match_ua' || currentMode === 'override_ua'
  const showBrowser = currentMode === 'override_ua'
  const showCaWarning = currentMode !== 'off'
  const modeInfo = mitmModeInfo[currentMode] || mitmModeInfo.off
  const ModeIcon = modeInfo.icon

  const tabs: { id: SettingsTab; label: string }[] = [
    { id: 'general', label: 'General' },
    { id: 'tls', label: 'TLS Interception' },
  ]

  return (
    <Modal onClose={onClose} className="max-w-2xl max-h-[90vh] overflow-y-auto p-6">
      <ModalHeader title={isEdit ? 'Edit Project' : 'Create Project'} onClose={onClose} />

      {/* Tab Navigation */}
      <div className="flex border-b border-gray-200 dark:border-gray-600 mb-4">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab.id
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:border-gray-300 dark:hover:border-gray-500'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <form onSubmit={handleSubmit}>
        <div className="min-h-[330px]">
          {/* General Tab */}
          {activeTab === 'general' && (
            <div className="grid grid-cols-2 gap-4">
              {/* Left column: Name, Description, Routing */}
              <div className="space-y-4">
                <div>
                  <Label>Name</Label>
                  <Input
                    type="text"
                    required
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    placeholder={isEdit ? undefined : 'My Project'}
                  />
                </div>
                <div>
                  <Label>Description</Label>
                  <Textarea
                    value={formData.description}
                    onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                    rows={2}
                    placeholder={isEdit ? undefined : 'Optional description'}
                  />
                </div>
                <div>
                  <Label>Routing Strategy</Label>
                  <Select
                    value={formData.routing_strategy}
                    onChange={(e) => setFormData({ ...formData, routing_strategy: e.target.value })}
                  >
                    <option value="round_robin">Round Robin</option>
                    <option value="least_used">Least Used</option>
                    <option value="random">Random</option>
                    <option value="sticky">Sticky</option>
                    <option value="health_based">Health Based</option>
                  </Select>
                </div>
                <div>
                  <Label>Metrics Retention (days)</Label>
                  <Input
                    type="number"
                    min={0}
                    value={formData.metrics_retention_days ?? 90}
                    onChange={(e) => setFormData({ ...formData, metrics_retention_days: parseInt(e.target.value) || 0 })}
                  />
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">0 = keep forever</p>
                </div>
              </div>

              {/* Right column: Proxy Credentials */}
              <div className="border border-gray-200 dark:border-gray-600 rounded-lg p-4 bg-gray-50 dark:bg-gray-700/50">
                <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Proxy Credentials</h3>
                <div className="space-y-3">
                  <div>
                    <Label className="text-xs text-gray-500 dark:text-gray-400">Username</Label>
                    <Input
                      type="text"
                      required
                      value={formData.username}
                      onChange={(e) => setFormData({ ...formData, username: e.target.value })}
                      className="font-mono text-sm"
                      placeholder={isEdit ? undefined : 'proxy_user'}
                    />
                  </div>
                  <div>
                    <Label className="text-xs text-gray-500 dark:text-gray-400">Password</Label>
                    <div className="relative">
                      <Input
                        type={showPassword ? 'text' : 'password'}
                        required
                        value={formData.password}
                        onChange={(e) => setFormData({ ...formData, password: e.target.value })}
                        className="pr-10 font-mono text-sm"
                        placeholder={isEdit ? undefined : '••••••••'}
                      />
                      <button
                        type="button"
                        onClick={() => setShowPassword(!showPassword)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                      >
                        {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* TLS Interception Tab */}
          {activeTab === 'tls' && (
            <div className="space-y-3">
              <div>
                <Label className="text-xs text-gray-500 dark:text-gray-400">Mode</Label>
                <RichSelect
                  options={mitmModeOptions}
                  value={currentMode}
                  onChange={handleModeChange}
                />
              </div>

              {showEngine && (
                <div>
                  <Label className="text-xs text-gray-500 dark:text-gray-400">TLS Engine</Label>
                  <RichSelect
                    options={engineOptions}
                    value={formData.tls_mitm_engine || 'curl_cffi'}
                    onChange={(v) => setFormData({ ...formData, tls_mitm_engine: v })}
                  />
                </div>
              )}

              {showBrowser && (
                <div>
                  <Label className="text-xs text-gray-500 dark:text-gray-400">Browser Profile</Label>
                  <RichSelect
                    options={browserOptions}
                    value={formData.tls_mitm_browser || 'chrome'}
                    onChange={(v) => setFormData({ ...formData, tls_mitm_browser: v })}
                  />
                </div>
              )}

              {/* Mode info panel */}
              <div className={`flex gap-2 p-3 rounded-lg border text-xs leading-relaxed ${modeInfo.color}`}>
                <ModeIcon className="w-4 h-4 flex-shrink-0 mt-0.5" />
                <span>{modeInfo.text}</span>
              </div>

              {showCaWarning && (
                <p className="text-xs text-amber-600 dark:text-amber-400">
                  Clients must install the proxy CA certificate.{' '}
                  <button
                    type="button"
                    onClick={() => downloadCaCertificate()}
                    className="underline hover:no-underline font-medium"
                  >
                    Download CA Certificate
                  </button>
                </p>
              )}
            </div>
          )}
        </div>

        {error && <Alert className="mt-4">{error}</Alert>}
        <ModalFooter>
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={isLoading}>
            {isLoading ? (isEdit ? 'Saving...' : 'Creating...') : (isEdit ? 'Save Changes' : 'Create Project')}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  )
}
