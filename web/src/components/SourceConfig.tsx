import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Database, RefreshCw, Trash2, Pencil, X } from 'lucide-react'
import { fetchProjectSources, createProjectSource, updateSource, deleteSource, Source } from '../api/client'
import { useProject } from '../contexts/ProjectContext'

const SOURCE_TYPES = ['static', 'api', 'aws', 'gcp', 'azure']

export default function SourceConfig() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const [showForm, setShowForm] = useState(false)
  const [formData, setFormData] = useState({ name: '', type: 'static' })
  const [editingSource, setEditingSource] = useState<Source | null>(null)
  const [editFormData, setEditFormData] = useState({ name: '', enabled: true })

  const { data, isLoading } = useQuery({
    queryKey: ['sources', selectedProjectId],
    queryFn: () => fetchProjectSources(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  const createMutation = useMutation({
    mutationFn: (data: { name: string; type: string }) =>
      createProjectSource(selectedProjectId!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources', selectedProjectId] })
      setShowForm(false)
      setFormData({ name: '', type: 'static' })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: { name?: string; enabled?: boolean } }) =>
      updateSource(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources', selectedProjectId] })
      setEditingSource(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: deleteSource,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['proxies', selectedProjectId] })
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    createMutation.mutate({
      name: formData.name,
      type: formData.type,
    })
  }

  const handleEditSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (editingSource) {
      updateMutation.mutate({
        id: editingSource.id,
        data: { name: editFormData.name, enabled: editFormData.enabled },
      })
    }
  }

  const startEditing = (source: Source) => {
    setEditingSource(source)
    setEditFormData({ name: source.name, enabled: source.enabled })
  }

  if (isLoading) return <div>Loading...</div>

  return (
    <div>
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold">Proxy Sources</h1>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          <Database className="w-5 h-5" />
          Add Source
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 mb-8">
          <div className="grid grid-cols-2 gap-4">
            <input
              type="text"
              placeholder="Source Name"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              className="px-4 py-2 border rounded-lg"
              required
            />
            <select
              value={formData.type}
              onChange={(e) => setFormData({ ...formData, type: e.target.value })}
              className="px-4 py-2 border rounded-lg"
            >
              {SOURCE_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type.toUpperCase()}
                </option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            className="mt-4 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700"
          >
            Create Source
          </button>
        </form>
      )}

      <div className="grid gap-6">
        {data?.sources.map((source) => (
          <div key={source.id} className="bg-white rounded-lg shadow p-6">
            {editingSource?.id === source.id ? (
              <form onSubmit={handleEditSubmit}>
                <div className="flex gap-4 items-center">
                  <input
                    type="text"
                    value={editFormData.name}
                    onChange={(e) => setEditFormData({ ...editFormData, name: e.target.value })}
                    className="flex-1 px-4 py-2 border rounded-lg"
                    required
                  />
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={editFormData.enabled}
                      onChange={(e) => setEditFormData({ ...editFormData, enabled: e.target.checked })}
                      className="w-4 h-4"
                    />
                    Enabled
                  </label>
                  <button
                    type="submit"
                    className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700"
                  >
                    Save
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditingSource(null)}
                    className="p-2 text-gray-500 hover:text-gray-700"
                  >
                    <X className="w-5 h-5" />
                  </button>
                </div>
              </form>
            ) : (
              <>
                <div className="flex justify-between items-start">
                  <div>
                    <h3 className="text-xl font-semibold">{source.name}</h3>
                    <p className="text-gray-500 mt-1">Type: {source.type}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span
                      className={`px-3 py-1 rounded-full text-sm font-medium ${
                        source.enabled
                          ? 'bg-green-100 text-green-800'
                          : 'bg-gray-100 text-gray-800'
                      }`}
                    >
                      {source.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                    <button className="p-2 text-gray-500 hover:text-blue-600">
                      <RefreshCw className="w-5 h-5" />
                    </button>
                    <button
                      onClick={() => startEditing(source)}
                      className="p-2 text-gray-500 hover:text-blue-600"
                    >
                      <Pencil className="w-5 h-5" />
                    </button>
                    <button
                      onClick={() => deleteMutation.mutate(source.id)}
                      className="p-2 text-gray-500 hover:text-red-600"
                    >
                      <Trash2 className="w-5 h-5" />
                    </button>
                  </div>
                </div>

                <div className="mt-4 grid grid-cols-3 gap-4 text-sm">
                  <div>
                    <span className="text-gray-500">Proxies:</span>
                    <span className="ml-2 font-medium">{source.proxy_count}</span>
                  </div>
                  <div>
                    <span className="text-gray-500">Refresh Interval:</span>
                    <span className="ml-2 font-medium">{source.refresh_interval_seconds}s</span>
                  </div>
                  <div>
                    <span className="text-gray-500">Last Refresh:</span>
                    <span className="ml-2 font-medium">
                      {source.last_refresh
                        ? new Date(source.last_refresh).toLocaleString()
                        : 'Never'}
                    </span>
                  </div>
                </div>
              </>
            )}
          </div>
        ))}

        {data?.sources.length === 0 && (
          <div className="bg-white rounded-lg shadow p-8 text-center">
            <Database className="w-12 h-12 text-gray-400 mx-auto mb-4" />
            <h3 className="text-lg font-medium text-gray-900">No sources configured</h3>
            <p className="text-gray-500 mt-2">
              Add a proxy source to start populating your proxy pool.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

