import { useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { X, Eye, EyeOff } from 'lucide-react'
import { createProjectCredential, updateProjectCredential, CredentialType, CredentialCreate, Credential, CredentialDetail } from '../api/client'
import { useProject } from '../contexts/ProjectContext'

export const CREDENTIAL_TYPES: { value: CredentialType; label: string }[] = [
  { value: 'static_proxy_provider', label: 'Static Proxy Provider' },
  { value: 'aws', label: 'AWS' },
  { value: 'gcp', label: 'GCP' },
  { value: 'azure', label: 'Azure' },
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
      return (
        <div key={key} className="relative">
          <label className="block text-sm font-medium text-gray-700 mb-1">
            {key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())}
          </label>
          {key === 'service_account_json' ? (
            <div>
              <textarea
                value={formData.config[key] || ''}
                onChange={(e) => handleConfigChange(key, e.target.value)}
                className={`w-full px-4 py-2 border rounded-lg font-mono text-sm ${jsonError ? 'border-red-300 bg-red-50' : ''}`}
                rows={4}
                placeholder="Paste service account JSON here"
              />
              {jsonError && (
                <p className="mt-1 text-xs text-red-600">{jsonError}</p>
              )}
            </div>
          ) : (
            <div className="relative">
              <input
                type={isSecret && !showSecrets[key] ? 'password' : 'text'}
                value={formData.config[key] || ''}
                onChange={(e) => handleConfigChange(key, e.target.value)}
                className="w-full px-4 py-2 border rounded-lg pr-10"
                placeholder={key.replace(/_/g, ' ')}
              />
              {isSecret && (
                <button type="button" onClick={() => toggleSecret(key)} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-700">
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
      <div className="bg-white rounded-lg shadow-xl max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto">
        <div className="p-6 border-b flex justify-between items-center">
          <h2 className="text-xl font-semibold">
            {isEditMode ? `Edit ${typeLabel} Credential` : (fixedType ? `Create ${typeLabel} Credential` : 'Create Credential')}
          </h2>
          <button onClick={handleClose} className="p-2 hover:bg-gray-100 rounded-lg">
            <X className="w-5 h-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-6">
          {errorMessage && (
            <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg mb-4 flex justify-between items-center">
              <span className="text-sm">{errorMessage}</span>
              <button type="button" onClick={() => setErrorMessage(null)} className="text-red-500 hover:text-red-700">
                <X className="w-4 h-4" />
              </button>
            </div>
          )}
          <div className="grid gap-4">
            <div className={fixedType || isEditMode ? '' : 'grid grid-cols-2 gap-4'}>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Credential Name</label>
                <input
                  type="text"
                  placeholder="My Credential"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="w-full px-4 py-2 border rounded-lg"
                  required
                />
              </div>
              {!fixedType && !isEditMode && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Type</label>
                  <select
                    value={formData.type}
                    onChange={(e) => handleTypeChange(e.target.value as CredentialType)}
                    className="w-full px-4 py-2 border rounded-lg"
                  >
                    {CREDENTIAL_TYPES.map((type) => (
                      <option key={type.value} value={type.value}>{type.label}</option>
                    ))}
                  </select>
                </div>
              )}
            </div>
            <div className="grid grid-cols-2 gap-4">{renderConfigFields()}</div>
          </div>
          <div className="flex justify-end gap-4 mt-6">
            <button type="button" onClick={handleClose} className="px-4 py-2 border rounded-lg hover:bg-gray-50">Cancel</button>
            <button type="submit" disabled={createMutation.isPending || updateMutation.isPending} className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50">
              {isEditMode
                ? (updateMutation.isPending ? 'Saving...' : 'Save Changes')
                : (createMutation.isPending ? 'Creating...' : 'Create Credential')}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

