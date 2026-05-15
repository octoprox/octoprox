// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

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
import { useTheme } from '../contexts/ThemeContext'
import { useAuth } from '../contexts/AuthContext'
import AddCredentialModal from './AddCredentialModal'
import { CREDENTIAL_TYPES } from '../utils/credentials'
import { Button, Card, Alert } from './ui'

export default function CredentialsConfig() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { isDark } = useTheme()
  const { canMutate } = useAuth()
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
        {canMutate && (
          <Button onClick={() => setShowAddModal(true)}>
            <Key className="w-5 h-5" />
            Add Credential
          </Button>
        )}
      </div>

      {errorMessage && (
        <Alert className="mb-6 flex justify-between items-center">
          <span>{errorMessage}</span>
          <button onClick={() => setErrorMessage(null)} className="text-red-500 hover:text-red-700">
            <X className="w-5 h-5" />
          </button>
        </Alert>
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

      {data?.credentials.length === 0 ? (
        <Card className="p-8 text-center">
          <Key className="w-12 h-12 text-gray-400 mx-auto mb-4" />
          <h3 className="text-lg font-medium">No credentials configured</h3>
          <p className="text-fg-muted mt-2">
            Add credentials to connect to proxy providers or cloud services.
          </p>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-raised border-b border-line">
                <th className="px-4 py-2 text-left text-xs font-semibold text-fg-muted uppercase tracking-wider">Credential</th>
                <th className="px-4 py-2 text-left text-xs font-semibold text-fg-muted uppercase tracking-wider">Created</th>
                {canMutate && <th className="px-4 py-2 text-right text-xs font-semibold text-fg-muted uppercase tracking-wider">Actions</th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {data?.credentials.map((credential) => (
                <tr key={credential.id} className="hover:bg-surface-raised/60 transition-colors">
                  <td className="px-4 py-2">
                    <div className="flex items-center gap-3">
                      <img src={(() => { const ct = CREDENTIAL_TYPES.find(ct => ct.value === credential.type); return ct ? (isDark ? ct.logoDark : ct.logo) : undefined })()} alt="" className="w-6 h-6 object-contain" />
                      <span className="font-medium text-fg">{credential.name}</span>
                      <span className="text-fg-subtle">({CREDENTIAL_TYPES.find(t => t.value === credential.type)?.label || credential.type})</span>
                    </div>
                  </td>
                  <td className="px-4 py-2 text-fg-muted">
                    {new Date(credential.created_at).toLocaleDateString()}
                  </td>
                  {canMutate && (
                    <td className="px-4 py-2 text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button variant="ghost" size="icon" onClick={() => startEditing(credential)} className="text-gray-500 hover:text-blue-600">
                          <Pencil className="w-4 h-4" />
                        </Button>
                        <Button variant="ghost" size="icon" onClick={() => deleteMutation.mutate(credential.id)} className="text-gray-500 hover:text-red-600">
                          <Trash2 className="w-4 h-4" />
                        </Button>
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  )
}
