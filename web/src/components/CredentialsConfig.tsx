import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Key, Trash2, Pencil, X, Eye, EyeOff } from 'lucide-react'
import {
  fetchProjectCredentials,
  fetchProjectCredential,
  updateProjectCredential,
  deleteProjectCredential,
  Credential,
  CredentialDetail,
  CredentialType,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import AddCredentialModal, { CREDENTIAL_TYPES, getDefaultCredentialConfig } from './AddCredentialModal'

export default function CredentialsConfig() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const [showAddModal, setShowAddModal] = useState(false)
  const [editingCredential, setEditingCredential] = useState<CredentialDetail | null>(null)
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({})
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['credentials', selectedProjectId],
    queryFn: () => fetchProjectCredentials(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: { name?: string; config?: Record<string, unknown> } }) =>
      updateProjectCredential(selectedProjectId!, id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['credentials', selectedProjectId] })
      setEditingCredential(null)
      setErrorMessage(null)
    },
    onError: (error: Error) => {
      setErrorMessage(error.message || 'Failed to update credential')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProjectCredential(selectedProjectId!, id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['credentials', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['project', selectedProjectId] })
      setErrorMessage(null)
    },
    onError: (error: Error) => {
      setErrorMessage(error.message || 'Failed to delete credential')
    },
  })

  const startEditing = async (credential: Credential) => {
    const detail = await fetchProjectCredential(selectedProjectId!, credential.id)
    setEditingCredential(detail)
  }

  const toggleSecret = (key: string) => {
    setShowSecrets({ ...showSecrets, [key]: !showSecrets[key] })
  }

  const renderConfigFields = (type: CredentialType, config: Record<string, string>, onChange: (key: string, value: string) => void, isEdit = false) => {
    const fields = getDefaultCredentialConfig(type)
    const prefix = isEdit ? 'edit-' : ''
    return Object.keys(fields).map((key) => {
      const isSecret = ['password', 'secret_key', 'client_secret', 'service_account_json'].includes(key)
      const showKey = `${prefix}${key}`
      return (
        <div key={key} className="relative">
          <label className="block text-sm font-medium text-gray-700 mb-1">
            {key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())}
          </label>
          {key === 'service_account_json' ? (
            <textarea
              value={config[key] || ''}
              onChange={(e) => onChange(key, e.target.value)}
              className="w-full px-4 py-2 border rounded-lg font-mono text-sm"
              rows={4}
              placeholder="Paste service account JSON here"
            />
          ) : (
            <div className="relative">
              <input
                type={isSecret && !showSecrets[showKey] ? 'password' : 'text'}
                value={config[key] || ''}
                onChange={(e) => onChange(key, e.target.value)}
                className="w-full px-4 py-2 border rounded-lg pr-10"
                placeholder={key.replace(/_/g, ' ')}
              />
              {isSecret && (
                <button type="button" onClick={() => toggleSecret(showKey)} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-700">
                  {showSecrets[showKey] ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                </button>
              )}
            </div>
          )}
        </div>
      )
    })
  }

  if (isLoading) return <div>Loading...</div>

  return (
    <div>
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold">Credentials</h1>
        <button
          onClick={() => setShowAddModal(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          <Key className="w-5 h-5" />
          Add Credential
        </button>
      </div>

      {errorMessage && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg mb-6 flex justify-between items-center">
          <span>{errorMessage}</span>
          <button onClick={() => setErrorMessage(null)} className="text-red-500 hover:text-red-700">
            <X className="w-5 h-5" />
          </button>
        </div>
      )}

      <AddCredentialModal
        isOpen={showAddModal}
        onClose={() => setShowAddModal(false)}
      />

      <div className="grid gap-6">
        {data?.credentials.map((credential) => (
          <div key={credential.id} className="bg-white rounded-lg shadow p-6">
            {editingCredential?.id === credential.id ? (
              <form onSubmit={(e) => {
                e.preventDefault()
                updateMutation.mutate({ id: credential.id, data: { name: editingCredential.name, config: editingCredential.config } })
              }}>
                <div className="grid gap-4">
                  <div className="flex gap-4 items-center">
                    <input
                      type="text"
                      value={editingCredential.name}
                      onChange={(e) => setEditingCredential({ ...editingCredential, name: e.target.value })}
                      className="flex-1 px-4 py-2 border rounded-lg"
                      required
                    />
                    <button type="submit" className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700">
                      Save
                    </button>
                    <button type="button" onClick={() => setEditingCredential(null)} className="p-2 text-gray-500 hover:text-gray-700">
                      <X className="w-5 h-5" />
                    </button>
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    {renderConfigFields(editingCredential.type, editingCredential.config as Record<string, string>, (key, value) => {
                      setEditingCredential({ ...editingCredential, config: { ...editingCredential.config, [key]: value } })
                    }, true)}
                  </div>
                </div>
              </form>
            ) : (
              <div className="flex justify-between items-start">
                <div>
                  <h3 className="text-xl font-semibold">{credential.name}</h3>
                  <p className="text-gray-500 mt-1">
                    Type: {CREDENTIAL_TYPES.find(t => t.value === credential.type)?.label || credential.type}
                  </p>
                  <p className="text-gray-400 text-sm mt-1">
                    Created: {new Date(credential.created_at).toLocaleDateString()}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => startEditing(credential)} className="p-2 text-gray-500 hover:text-blue-600">
                    <Pencil className="w-5 h-5" />
                  </button>
                  <button onClick={() => deleteMutation.mutate(credential.id)} className="p-2 text-gray-500 hover:text-red-600">
                    <Trash2 className="w-5 h-5" />
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}

        {data?.credentials.length === 0 && (
          <div className="bg-white rounded-lg shadow p-8 text-center">
            <Key className="w-12 h-12 text-gray-400 mx-auto mb-4" />
            <h3 className="text-lg font-medium text-gray-900">No credentials configured</h3>
            <p className="text-gray-500 mt-2">
              Add credentials to connect to proxy providers or cloud services.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

