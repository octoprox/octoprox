import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Key, Trash2, Pencil, X } from 'lucide-react'
import {
  fetchProjectCredentials,
  fetchProjectCredential,
  deleteProjectCredential,
  Credential,
  CredentialDetail,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import AddCredentialModal, { CREDENTIAL_TYPES } from './AddCredentialModal'

export default function CredentialsConfig() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const [showAddModal, setShowAddModal] = useState(false)
  const [editingCredential, setEditingCredential] = useState<CredentialDetail | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['credentials', selectedProjectId],
    queryFn: () => fetchProjectCredentials(selectedProjectId!),
    enabled: !!selectedProjectId,
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

      <AddCredentialModal
        isOpen={editingCredential !== null}
        onClose={() => setEditingCredential(null)}
        credential={editingCredential}
      />

      <div className="grid gap-6">
        {data?.credentials.map((credential) => (
          <div key={credential.id} className="bg-white rounded-lg shadow p-6">
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

