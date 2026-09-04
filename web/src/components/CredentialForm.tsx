// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Eye, EyeOff } from 'lucide-react'
import {
  createProjectCredential, updateProjectCredential,
  CredentialType, CredentialCreate, CredentialDetail, OxylabsProxyType,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useTheme } from '../contexts/ThemeContext'
import { Button, Input, Select, Textarea, Label, Alert, Inspector } from './ui'
import { CREDENTIAL_TYPES } from '../utils/credentials'

export const OXYLABS_PROXY_TYPES: { value: OxylabsProxyType; label: string }[] = [
  { value: 'residential', label: 'Residential' },
  { value: 'mobile', label: 'Mobile' },
  { value: 'isp', label: 'ISP' },
  { value: 'dedicated_isp', label: 'Dedicated ISP' },
  { value: 'datacenter', label: 'Datacenter' },
  { value: 'datacenter_dedicated', label: 'Datacenter Dedicated' },
]

export const getDefaultCredentialConfig = (type: CredentialType): Record<string, string> => {
  switch (type) {
    case 'static_proxy_provider':
      return { username: '', password: '' }
    case 'aws':
      return { access_key: '', secret_key: '' }
    case 'gcp':
      return { service_account_json: '', project_id: '' }
    case 'azure':
      return { subscription_id: '', tenant_id: '', client_id: '', client_secret: '', key_vault_name: '' }
    case 'oxylabs':
      return { proxy_type: 'residential', username: '', password: '' }
    case 'brightdata':
      return { token: '' }
    default:
      return {}
  }
}

/** Grid of provider cards for choosing a credential / connector type. */
export function TypePicker({ onPick, hint }: { onPick: (type: CredentialType) => void; hint?: string }) {
  const { isDark } = useTheme()
  return (
    <div className="space-y-3">
      {hint && <p className="text-xs text-fg-muted">{hint}</p>}
      <div className="grid grid-cols-[repeat(auto-fill,minmax(170px,1fr))] gap-2.5">
        {CREDENTIAL_TYPES.map((ct) => (
          <button
            key={ct.value}
            type="button"
            onClick={() => onPick(ct.value)}
            className="flex items-center gap-3 p-3 border border-line rounded-[10px] text-left hover:border-primary hover:bg-primary-soft transition-colors"
          >
            <img src={isDark ? ct.logoDark : ct.logo} alt="" className="w-9 h-9 object-contain flex-none" />
            <span className="min-w-0">
              <span className="block text-[13px] font-semibold leading-tight">{ct.name}</span>
              <span className="block text-[11.5px] text-fg-muted leading-snug mt-0.5">{ct.description}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}

/** Small "you picked X" card shown at the top of a typed form. */
export function TypeCard({ type, onChange }: { type: CredentialType; onChange?: () => void }) {
  const { isDark } = useTheme()
  const ct = CREDENTIAL_TYPES.find((t) => t.value === type)
  if (!ct) return null
  return (
    <div className="flex items-center gap-3 p-3 border border-line rounded-[10px]">
      <img src={isDark ? ct.logoDark : ct.logo} alt="" className="w-9 h-9 object-contain flex-none" />
      <span className="min-w-0 flex-1">
        <span className="block text-[13px] font-semibold leading-tight">{ct.name}</span>
        <span className="block text-[11.5px] text-fg-muted leading-snug mt-0.5">{ct.description}</span>
      </span>
      {onChange && <button type="button" onClick={onChange} className="text-xs text-primary hover:brightness-110">Change</button>}
    </div>
  )
}

interface CredentialFormProps {
  type: CredentialType
  credential?: CredentialDetail | null
  onSaved: (credential: CredentialDetail) => void
  formId: string
  onPending?: (pending: boolean) => void
}

/**
 * Create/edit form for one credential type. Owns its mutations so callers only
 * need to know when it succeeded.
 */
export function CredentialForm({ type, credential, onSaved, formId }: CredentialFormProps) {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const isEditMode = !!credential
  const [name, setName] = useState(credential?.name ?? '')
  const [config, setConfig] = useState<Record<string, string>>(
    credential ? (credential.config as Record<string, string>) : getDefaultCredentialConfig(type)
  )
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({})
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['credentials', selectedProjectId] })
    queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
  }

  const createMutation = useMutation({
    mutationFn: (data: CredentialCreate) => createProjectCredential(selectedProjectId!, data),
    onSuccess: (created) => { invalidate(); onSaved(created) },
    onError: (error: Error) => setErrorMessage(error.message || 'Failed to create credential'),
  })
  const updateMutation = useMutation({
    mutationFn: (data: { name: string; config: Record<string, string> }) => updateProjectCredential(selectedProjectId!, credential!.id, data),
    onSuccess: (updated) => { invalidate(); onSaved(updated) },
    onError: (error: Error) => setErrorMessage(error.message || 'Failed to update credential'),
  })

  const validateServiceAccountJson = (json: string): string | null => {
    if (!json || !json.trim()) return 'Service account JSON is required'
    try {
      const parsed = JSON.parse(json)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return 'Service account JSON must be a valid JSON object'
      return null
    } catch {
      return 'Invalid JSON format. Please paste a valid service account JSON.'
    }
  }
  const jsonError = type === 'gcp' && config.service_account_json?.trim() ? validateServiceAccountJson(config.service_account_json) : null

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setErrorMessage(null)
    if (type === 'gcp') {
      const err = validateServiceAccountJson(config.service_account_json || '')
      if (err) { setErrorMessage(err); return }
    }
    if (isEditMode) updateMutation.mutate({ name, config })
    else createMutation.mutate({ name, type, config })
  }

  const fields = Object.keys(getDefaultCredentialConfig(type))

  return (
    <form id={formId} onSubmit={handleSubmit} className="space-y-4" autoComplete="off">
      {errorMessage && <Alert>{errorMessage}</Alert>}
      <div>
        <Label>Name</Label>
        <Input type="text" placeholder="e.g. aws-prod" value={name} onChange={(e) => setName(e.target.value)} required />
      </div>
      {fields.map((key) => {
        const isSecret = ['password', 'secret_key', 'client_secret', 'service_account_json', 'token'].includes(key)
        if (key === 'proxy_type' && type === 'oxylabs') {
          return (
            <div key={key}>
              <Label>Proxy type</Label>
              <Select value={config[key] || 'residential'} onChange={(e) => setConfig({ ...config, [key]: e.target.value })}>
                {OXYLABS_PROXY_TYPES.map((pt) => <option key={pt.value} value={pt.value}>{pt.label}</option>)}
              </Select>
            </div>
          )
        }
        if (key === 'customer_id' && type === 'brightdata') return null
        const label = key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())
        return (
          <div key={key}>
            <Label>{label}</Label>
            {key === 'service_account_json' ? (
              <>
                <Textarea
                  value={config[key] || ''}
                  onChange={(e) => setConfig({ ...config, [key]: e.target.value })}
                  className={`font-mono text-xs ${jsonError ? 'border-danger/50 bg-danger-soft' : ''}`}
                  rows={5}
                  placeholder="Paste service account JSON here"
                  autoComplete="off"
                />
                {jsonError && <p className="mt-1 text-xs text-danger">{jsonError}</p>}
              </>
            ) : (
              <div className="relative">
                <Input
                  type={isSecret && !showSecrets[key] ? 'password' : 'text'}
                  name={`octoprox-cred-${key}`}
                  value={config[key] || ''}
                  onChange={(e) => setConfig({ ...config, [key]: e.target.value })}
                  className={isSecret ? 'pr-10 font-mono text-sm' : ''}
                  placeholder={isEditMode && isSecret ? 'Leave unchanged to keep the current secret' : label}
                  autoComplete={isSecret ? 'new-password' : 'off'}
                  data-1p-ignore
                  data-lpignore="true"
                />
                {isSecret && (
                  <button type="button" onClick={() => setShowSecrets({ ...showSecrets, [key]: !showSecrets[key] })} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-fg-subtle hover:text-fg-muted">
                    {showSecrets[key] ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                )}
              </div>
            )}
          </div>
        )
      })}
      <p className="text-xs text-fg-subtle">Secrets are encrypted at rest.</p>
      {/* Hidden submit so Enter works; the visible button lives in the panel footer. */}
      <button type="submit" className="hidden" disabled={createMutation.isPending || updateMutation.isPending} />
    </form>
  )
}

/**
 * Complete "new credential" flow for a docked panel: type picker, then form.
 * `fixedType` skips the picker (used when nested under a connector).
 */
export function NewCredentialPanel({ fixedType, crumb, onBack, onClose, onCreated, width }: {
  fixedType?: CredentialType
  crumb?: string
  onBack?: () => void
  onClose: () => void
  onCreated: (credential: CredentialDetail) => void
  /** Fixed width so a nested step does not resize the panel it replaces. */
  width?: number
}) {
  const [type, setType] = useState<CredentialType | null>(fixedType ?? null)
  const typeLabel = type ? CREDENTIAL_TYPES.find((t) => t.value === type)?.label : null
  return (
    <Inspector
      title={type ? `New ${typeLabel} credential` : 'Add credential'}
      subtitle={type ? 'Encrypted at rest' : 'Choose a provider'}
      crumb={crumb}
      onBack={onBack ?? (type && !fixedType ? () => setType(null) : undefined)}
      onClose={onClose}
      width={width ?? 560}
      footer={type ? (
        <>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="credential-form" size="sm">Create credential</Button>
        </>
      ) : undefined}
    >
      {!type ? (
        <TypePicker onPick={setType} hint="Where should Octoprox authenticate?" />
      ) : (
        <>
          <TypeCard type={type} onChange={fixedType ? undefined : () => setType(null)} />
          {crumb && (
            <p className="text-xs text-primary-soft-fg bg-primary-soft rounded-lg px-3 py-2">
              The connector you were editing will select this credential automatically.
            </p>
          )}
          <CredentialForm key={type} type={type} onSaved={onCreated} formId="credential-form" />
        </>
      )}
    </Inspector>
  )
}
