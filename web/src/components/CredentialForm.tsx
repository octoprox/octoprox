// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldCheck } from 'lucide-react'
import {
  createProjectCredential, updateProjectCredential,
  CredentialType, CredentialCreate, CredentialDetail, ProviderSummary,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useProviders } from '../hooks/useProviders'
import { Button, Alert, Inspector, Badge } from './ui'
import { ProviderLogo } from './ProviderLogo'
import { SchemaForm, defaultValues, serializeValues, FormValues } from './SchemaForm'

/** Grid of provider cards for choosing a credential / connector type. */
export function TypePicker({ onPick, hint }: { onPick: (type: CredentialType) => void; hint?: string }) {
  const { providers, isLoading } = useProviders()
  return (
    <div className="space-y-3">
      {hint && <p className="text-xs text-fg-muted">{hint}</p>}
      {isLoading && <p className="text-xs text-fg-muted">Loading providers…</p>}
      <div className="grid grid-cols-[repeat(auto-fill,minmax(170px,1fr))] gap-2.5">
        {providers.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => onPick(p.id)}
            className="flex items-center gap-3 p-3 border border-line rounded-[10px] text-left hover:border-primary hover:bg-primary-soft transition-colors"
          >
            <ProviderLogo type={p.id} name={p.name} className="w-9 h-9 text-[36px]" />
            <span className="min-w-0">
              <span className="block text-[13px] font-semibold leading-tight">
                {p.name}
                {p.beta && <span className="ml-1.5 text-[10px] font-medium uppercase text-warning">beta</span>}
              </span>
              <span className="block text-[11.5px] text-fg-muted leading-snug mt-0.5">{p.description}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}

/** Small "you picked X" card shown at the top of a typed form. */
export function TypeCard({ type, onChange }: { type: CredentialType; onChange?: () => void }) {
  const { get } = useProviders()
  const p = get(type)
  if (!p) return null
  return (
    <div className="flex items-center gap-3 p-3 border border-line rounded-[10px]">
      <ProviderLogo type={p.id} name={p.name} className="w-9 h-9 text-[36px]" />
      <span className="min-w-0 flex-1">
        <span className="block text-[13px] font-semibold leading-tight">
          {p.name}
          {p.source === 'custom' && <Badge color="purple" className="ml-2 py-0 text-[10px]">custom</Badge>}
        </span>
        <span className="block text-[11.5px] text-fg-muted leading-snug mt-0.5">{p.description}</span>
      </span>
      {onChange && <button type="button" onClick={onChange} className="text-xs text-primary hover:brightness-110">Change</button>}
    </div>
  )
}

/** Tells the user which vendor hosts will receive the secrets they are about to enter. */
export function EgressNotice({ provider }: { provider: ProviderSummary }) {
  if (!provider.egress_hosts.length) return null
  return (
    <p className="text-xs text-fg-muted bg-surface-raised rounded-lg px-3 py-2 flex items-start gap-2">
      <ShieldCheck className="w-3.5 h-3.5 flex-none mt-0.5 text-fg-subtle" />
      <span>
        This credential is sent to <span className="font-mono text-fg">{provider.egress_hosts.join(', ')}</span>
        {provider.has_validation ? ' and is verified there when you save.' : ' when options are fetched or proxies are provisioned.'}
      </span>
    </p>
  )
}

interface CredentialFormProps {
  type: CredentialType
  credential?: CredentialDetail | null
  onSaved: (credential: CredentialDetail) => void
  formId: string
}

/**
 * Create/edit form for one credential type, rendered from the provider's
 * credential field schema. Owns its mutations so callers only need to know
 * when it succeeded.
 */
export function CredentialForm({ type, credential, onSaved, formId }: CredentialFormProps) {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { get, presets } = useProviders()
  const provider = get(type)
  const fields = provider?.credential_fields ?? []
  const isEditMode = !!credential
  const [name, setName] = useState(credential?.name ?? '')
  const [values, setValues] = useState<FormValues>(() => {
    const base = defaultValues(fields)
    if (credential) for (const [k, v] of Object.entries(credential.config)) base[k] = v == null ? '' : String(v)
    return base
  })
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  // Fields may arrive after the first render (catalog still loading): seed defaults once.
  useEffect(() => {
    if (!isEditMode && fields.length > 0) setValues((prev) => (Object.keys(prev).length ? prev : defaultValues(fields)))
  }, [fields, isEditMode])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['credentials', selectedProjectId] })
    queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
    queryClient.invalidateQueries({ queryKey: ['providers'] })
  }

  const createMutation = useMutation({
    mutationFn: (data: CredentialCreate) => createProjectCredential(selectedProjectId!, data),
    onSuccess: (created) => { invalidate(); onSaved(created) },
    onError: (error: Error) => setErrorMessage(error.message || 'Failed to create credential'),
  })
  const updateMutation = useMutation({
    mutationFn: (data: { name: string; config: Record<string, unknown> }) => updateProjectCredential(selectedProjectId!, credential!.id, data),
    onSuccess: (updated) => { invalidate(); onSaved(updated) },
    onError: (error: Error) => setErrorMessage(error.message || 'Failed to update credential'),
  })

  const scopes = { credential: values, connector: {} }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setErrorMessage(null)
    if (!provider) return
    const config = serializeValues(fields, values, scopes)
    // Carry over server-captured values (e.g. a customer id) that are not user fields.
    if (credential) for (const [k, v] of Object.entries(credential.config)) if (!(k in config) && !fields.some((f) => f.key === k)) config[k] = v
    if (isEditMode) updateMutation.mutate({ name, config })
    else createMutation.mutate({ name, type, config })
  }

  if (!provider) return <p className="text-sm text-fg-muted">Unknown provider type “{type}”.</p>

  return (
    <form id={formId} onSubmit={handleSubmit} className="space-y-4" autoComplete="off">
      {errorMessage && <Alert>{errorMessage}</Alert>}
      <div>
        <label className="block text-sm font-medium text-fg-muted mb-1">Name</label>
        <input
          type="text"
          placeholder="e.g. aws-prod"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          className="w-full px-4 py-2 border border-line-strong rounded-lg bg-surface text-fg focus:ring-1 focus:ring-ring focus:border-ring placeholder:text-fg-subtle"
        />
      </div>
      <SchemaForm
        provider={provider}
        fields={fields}
        values={values}
        onChange={(key, value, fill) => setValues((prev) => ({ ...prev, [key]: value, ...(fill ?? {}) }))}
        scopes={scopes}
        presets={presets}
        credentialConfig={serializeValues(fields, values, scopes)}
        isEdit={isEditMode}
      />
      <EgressNotice provider={provider} />
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
  const { labelFor } = useProviders()
  const [type, setType] = useState<CredentialType | null>(fixedType ?? null)
  return (
    <Inspector
      title={type ? `New ${labelFor(type)} credential` : 'Add credential'}
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
