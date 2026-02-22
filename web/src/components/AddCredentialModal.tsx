// Copyright 2025 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { X, Eye, EyeOff } from 'lucide-react'
import { createProjectCredential, updateProjectCredential, CredentialType, CredentialCreate, Credential, CredentialDetail, OxylabsProxyType } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { Button, Input, Select, Textarea, Label, Alert, ModalFooter } from './ui'

export const CREDENTIAL_TYPES: { value: CredentialType; label: string }[] = [
  { value: 'static_proxy_provider', label: 'Static Proxy Provider' },
  { value: 'aws', label: 'AWS' },
  { value: 'gcp', label: 'GCP' },
  { value: 'azure', label: 'Azure' },
  { value: 'oxylabs', label: 'Oxylabs' },
]

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
    default:
      return {}
  }
}

interface AddCredentialModalProps {
  isOpen: boolean
  onClose: () => void
  onSuccess?: (credential: Credential) => void
  fixedType?: CredentialType // If provided, type selector is hidden and this type is used
  credential?: CredentialDetail | null // If provided, modal is in edit mode
}

export default function AddCredentialModal({ isOpen, onClose, onSuccess, fixedType, credential }: AddCredentialModalProps) {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const isEditMode = !!credential
  const [formData, setFormData] = useState({
    name: '',
    type: fixedType || ('static_proxy_provider' as CredentialType),
    config: getDefaultCredentialConfig(fixedType || 'static_proxy_provider'),
  })
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({})
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  // Reset form when modal opens, fixedType changes, or credential changes
  useEffect(() => {
    if (isOpen) {
      if (credential) {
        // Edit mode: populate with existing credential data
        setFormData({
          name: credential.name,
          type: credential.type,
          config: credential.config as Record<string, string>,
        })
      } else {
        // Create mode: reset to defaults
        const type = fixedType || 'static_proxy_provider'
        setFormData({ name: '', type, config: getDefaultCredentialConfig(type) })
      }
      setErrorMessage(null)
      setShowSecrets({})
    }
  }, [isOpen, fixedType, credential])

  const createMutation = useMutation({
    mutationFn: (data: CredentialCreate) => createProjectCredential(selectedProjectId!, data),
    onSuccess: (newCredential) => {
      queryClient.invalidateQueries({ queryKey: ['credentials', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
      resetForm()
      onSuccess?.(newCredential)
      onClose()
    },
    onError: (error: Error) => {
      setErrorMessage(error.message || 'Failed to create credential')
    },
  })

  const updateMutation = useMutation({
    mutationFn: (data: { name: string; config: Record<string, string> }) =>
      updateProjectCredential(selectedProjectId!, credential!.id, data),
    onSuccess: (updatedCredential) => {
      queryClient.invalidateQueries({ queryKey: ['credentials', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
      resetForm()
      onSuccess?.(updatedCredential)
      onClose()
    },
    onError: (error: Error) => {
      setErrorMessage(error.message || 'Failed to update credential')
    },
  })

  const resetForm = () => {
    const type = fixedType || 'static_proxy_provider'
    setFormData({ name: '', type, config: getDefaultCredentialConfig(type) })
    setErrorMessage(null)
    setShowSecrets({})
  }

  const handleClose = () => {
    resetForm()
    onClose()
  }

  const handleTypeChange = (type: CredentialType) => {
    setFormData({ ...formData, type, config: getDefaultCredentialConfig(type) })
  }

  const handleConfigChange = (key: string, value: string) => {
    setFormData({ ...formData, config: { ...formData.config, [key]: value } })
  }

  // Validate service account JSON for GCP credentials
  const validateServiceAccountJson = (json: string): string | null => {
    if (!json || !json.trim()) {
      return 'Service account JSON is required'
    }
    try {
      const parsed = JSON.parse(json)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        return 'Service account JSON must be a valid JSON object'
      }
      return null
    } catch {
      return 'Invalid JSON format. Please paste a valid service account JSON.'
    }
  }

  const getServiceAccountJsonError = (): string | null => {
    if (formData.type !== 'gcp') return null
    const json = formData.config.service_account_json
    if (!json || !json.trim()) return null // Don't show error for empty field until submit
    return validateServiceAccountJson(json)
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    // Validate GCP service account JSON before submitting
    if (formData.type === 'gcp') {
      const jsonError = validateServiceAccountJson(formData.config.service_account_json || '')
      if (jsonError) {
        setErrorMessage(jsonError)
        return
      }
    }
    if (isEditMode) {
      updateMutation.mutate({ name: formData.name, config: formData.config })
    } else {
      createMutation.mutate({ name: formData.name, type: formData.type, config: formData.config })
    }
  }

  const toggleSecret = (key: string) => {
    setShowSecrets({ ...showSecrets, [key]: !showSecrets[key] })
  }

  const renderConfigFields = () => {
    const fields = getDefaultCredentialConfig(formData.type)
    const jsonError = getServiceAccountJsonError()
    return Object.keys(fields).map((key) => {
      const isSecret = ['password', 'secret_key', 'client_secret', 'service_account_json'].includes(key)

      // Special handling for Oxylabs proxy_type field
      if (key === 'proxy_type' && formData.type === 'oxylabs') {
        return (
          <div key={key} className="relative">
            <Label>Proxy Type</Label>
            <Select
              value={formData.config[key] || 'residential'}
              onChange={(e) => handleConfigChange(key, e.target.value)}
            >
              {OXYLABS_PROXY_TYPES.map((pt) => (
                <option key={pt.value} value={pt.value}>{pt.label}</option>
              ))}
            </Select>
          </div>
        )
      }

      return (
        <div key={key} className="relative">
          <Label>
            {key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())}
          </Label>
          {key === 'service_account_json' ? (
            <div>
              <Textarea
                value={formData.config[key] || ''}
                onChange={(e) => handleConfigChange(key, e.target.value)}
                className={`font-mono text-sm ${jsonError ? 'border-red-300 dark:border-red-600 bg-red-50 dark:bg-red-900/20' : ''}`}
                rows={4}
                placeholder="Paste service account JSON here"
              />
              {jsonError && (
                <p className="mt-1 text-xs text-red-600 dark:text-red-400">{jsonError}</p>
              )}
            </div>
          ) : (
            <div className="relative">
              <Input
                type={isSecret && !showSecrets[key] ? 'password' : 'text'}
                value={formData.config[key] || ''}
                onChange={(e) => handleConfigChange(key, e.target.value)}
                className="pr-10"
                placeholder={key.replace(/_/g, ' ')}
              />
              {isSecret && (
                <button type="button" onClick={() => toggleSecret(key)} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-700 dark:hover:text-gray-300">
                  {showSecrets[key] ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                </button>
              )}
            </div>
          )}
        </div>
      )
    })
  }

  if (!isOpen) return null

  const typeLabel = CREDENTIAL_TYPES.find((t) => t.value === formData.type)?.label || formData.type

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60]">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto">
        <div className="p-6 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center">
          <h2 className="text-xl font-semibold">
            {isEditMode ? `Edit ${typeLabel} Credential` : (fixedType ? `Create ${typeLabel} Credential` : 'Create Credential')}
          </h2>
          <button onClick={handleClose} className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg">
            <X className="w-5 h-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-6">
          {errorMessage && (
            <Alert className="mb-4 flex justify-between items-center">
              <span className="text-sm">{errorMessage}</span>
              <button type="button" onClick={() => setErrorMessage(null)} className="text-red-500 hover:text-red-700">
                <X className="w-4 h-4" />
              </button>
            </Alert>
          )}
          <div className="grid gap-4">
            <div className={fixedType || isEditMode ? '' : 'grid grid-cols-2 gap-4'}>
              <div>
                <Label>Credential Name</Label>
                <Input
                  type="text"
                  placeholder="My Credential"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  required
                />
              </div>
              {!fixedType && !isEditMode && (
                <div>
                  <Label>Type</Label>
                  <Select
                    value={formData.type}
                    onChange={(e) => handleTypeChange(e.target.value as CredentialType)}
                  >
                    {CREDENTIAL_TYPES.map((type) => (
                      <option key={type.value} value={type.value}>{type.label}</option>
                    ))}
                  </Select>
                </div>
              )}
            </div>
            <div className="grid grid-cols-2 gap-4">{renderConfigFields()}</div>
          </div>
          <ModalFooter>
            <Button type="button" variant="outline" onClick={handleClose}>Cancel</Button>
            <Button type="submit" variant="success" disabled={createMutation.isPending || updateMutation.isPending}>
              {isEditMode
                ? (updateMutation.isPending ? 'Saving...' : 'Save Changes')
                : (createMutation.isPending ? 'Creating...' : 'Create Credential')}
            </Button>
          </ModalFooter>
        </form>
      </div>
    </div>
  )
}
