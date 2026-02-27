// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Eye, EyeOff, Info, AlertTriangle, Shield } from 'lucide-react'
import { ProjectUpdate, ProjectSummary } from '../api/client'
import { RichSelect, RichSelectOption } from './RichSelect'
import { Button, Input, Select, Textarea, Label, Modal, ModalHeader, ModalFooter, Alert } from './ui'

interface EditProjectModalProps {
  project: ProjectSummary
  onClose: () => void
  onSave: (data: ProjectUpdate) => void
  isLoading: boolean
  error?: string
}

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

export default function EditProjectModal({
  project,
  onClose,
  onSave,
  isLoading,
  error,
}: EditProjectModalProps) {
  const [formData, setFormData] = useState<ProjectUpdate>({
    name: project.name,
    description: project.description,
    username: project.username,
    password: project.password,
    routing_strategy: project.routing_strategy,
    tls_mitm_mode: project.tls_mitm_mode || 'off',
    tls_mitm_engine: project.tls_mitm_engine,
    tls_mitm_browser: project.tls_mitm_browser,
  })
  const [showPassword, setShowPassword] = useState(false)

  const handleModeChange = (mode: string) => {
    const updates: Partial<ProjectUpdate> = { tls_mitm_mode: mode }
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

  return (
    <Modal onClose={onClose} className="max-h-[90vh] overflow-y-auto p-6">
      <ModalHeader title="Edit Project" onClose={onClose} />
      <form onSubmit={handleSubmit}>
        <div className="space-y-4">
          <div>
            <Label>Name</Label>
            <Input
              type="text"
              required
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
            />
          </div>
          <div>
            <Label>Description</Label>
            <Textarea
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              rows={2}
            />
          </div>
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

          {/* TLS Interception Section */}
          <div className="border border-gray-200 dark:border-gray-600 rounded-lg p-4 bg-gray-50 dark:bg-gray-700/50">
            <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">TLS Interception</h3>
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
                  <Select
                    value={formData.tls_mitm_engine || 'curl_cffi'}
                    onChange={(e) => setFormData({ ...formData, tls_mitm_engine: e.target.value })}
                  >
                    <option value="curl_cffi">curl_cffi (C/libcurl, mature, Chrome-grade)</option>
                    <option value="rnet">rnet (Rust/BoringSSL, fast, 113+ profiles)</option>
                  </Select>
                </div>
              )}

              {showBrowser && (
                <div>
                  <Label className="text-xs text-gray-500 dark:text-gray-400">Browser Profile</Label>
                  <Select
                    value={formData.tls_mitm_browser || 'chrome'}
                    onChange={(e) => setFormData({ ...formData, tls_mitm_browser: e.target.value })}
                  >
                    <option value="chrome">Chrome (most common, lowest detection risk)</option>
                    <option value="firefox">Firefox (alternative fingerprint)</option>
                    <option value="safari">Safari (macOS/iOS fingerprint)</option>
                    <option value="edge">Edge (Chromium-based, Windows-like)</option>
                  </Select>
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
                  <a
                    href="/api/v1/projects/ca-certificate"
                    download="octoprox-ca.crt"
                    className="underline hover:no-underline font-medium"
                  >
                    Download CA Certificate
                  </a>
                </p>
              )}
            </div>
          </div>
        </div>
        {error && <Alert className="mt-4">{error}</Alert>}
        <ModalFooter>
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={isLoading}>
            {isLoading ? 'Saving...' : 'Save Changes'}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  )
}
