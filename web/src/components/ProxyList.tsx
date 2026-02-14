import { useState, useRef, useMemo, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Plus, Trash2, Pencil, X, Upload } from 'lucide-react'
import {
  fetchProjectProxies,
  fetchProjectConnectors,
  createProjectProxy,
  updateProjectProxy,
  deleteProjectProxy,
  uploadProjectProxies,
  Proxy,
  ProxyCreate,
  ProxyUploadResponse,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { DataTable, createSelectionColumn } from './DataTable'

export default function ProxyList() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const [showForm, setShowForm] = useState(false)
  const [formData, setFormData] = useState({ host: '', port: '', protocol: 'http', connector_id: '', username: '', password: '' })
  const [editingProxy, setEditingProxy] = useState<Proxy | null>(null)
  const [editFormData, setEditFormData] = useState({ host: '', port: '', protocol: '', username: '', password: '' })

  // Upload state
  const [showUploadModal, setShowUploadModal] = useState(false)
  const [uploadConnectorId, setUploadConnectorId] = useState('')
  const [uploadResult, setUploadResult] = useState<ProxyUploadResponse | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Selection state for bulk operations
  const [selectedProxies, setSelectedProxies] = useState<Proxy[]>([])
  const [isBulkDeleting, setIsBulkDeleting] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['proxies', selectedProjectId],
    queryFn: () => fetchProjectProxies(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  const { data: connectorsData } = useQuery({
    queryKey: ['connectors', selectedProjectId],
    queryFn: () => fetchProjectConnectors(selectedProjectId!),
    enabled: !!selectedProjectId,
  })

  // Filter to only show static_proxy_provider connectors
  const staticConnectors = connectorsData?.connectors.filter(c => c.credential_type === 'static_proxy_provider') || []

  const createMutation = useMutation({
    mutationFn: (data: ProxyCreate) => createProjectProxy(selectedProjectId!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['proxies', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
      setShowForm(false)
      setFormData({ host: '', port: '', protocol: 'http', connector_id: '', username: '', password: '' })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: { host?: string; port?: number; protocol?: string; username?: string; password?: string } }) =>
      updateProjectProxy(selectedProjectId!, id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['proxies', selectedProjectId] })
      setEditingProxy(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteProjectProxy(selectedProjectId!, id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['proxies', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
    },
  })

  // Bulk delete handler
  const handleBulkDelete = useCallback(async () => {
    if (selectedProxies.length === 0) return

    const confirmed = window.confirm(
      `Are you sure you want to delete ${selectedProxies.length} proxy${selectedProxies.length > 1 ? 'ies' : ''}?`
    )
    if (!confirmed) return

    setIsBulkDeleting(true)
    try {
      await Promise.all(
        selectedProxies.map(proxy => deleteProjectProxy(selectedProjectId!, proxy.id))
      )
      queryClient.invalidateQueries({ queryKey: ['proxies', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
      setSelectedProxies([])
    } catch (error) {
      console.error('Bulk delete failed:', error)
    } finally {
      setIsBulkDeleting(false)
    }
  }, [selectedProxies, selectedProjectId, queryClient])

  // Selection change handler
  const handleSelectionChange = useCallback((rows: Proxy[]) => {
    setSelectedProxies(rows)
  }, [])

  const uploadMutation = useMutation({
    mutationFn: ({ file, connectorId }: { file: File; connectorId: string }) =>
      uploadProjectProxies(selectedProjectId!, file, connectorId),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['proxies', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
      setUploadResult(result)
      setUploadError(null)
    },
    onError: (error: Error) => {
      setUploadError(error.message || 'Upload failed')
      setUploadResult(null)
    },
  })

  const handleUpload = (e: React.FormEvent) => {
    e.preventDefault()
    const file = fileInputRef.current?.files?.[0]
    if (file && uploadConnectorId) {
      uploadMutation.mutate({ file, connectorId: uploadConnectorId })
    }
  }

  const closeUploadModal = () => {
    setShowUploadModal(false)
    setUploadConnectorId('')
    setUploadResult(null)
    setUploadError(null)
    if (fileInputRef.current) {
      fileInputRef.current.value = ''
    }
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    createMutation.mutate({
      host: formData.host,
      port: parseInt(formData.port),
      protocol: formData.protocol,
      connector_id: formData.connector_id,
      username: formData.username || undefined,
      password: formData.password || undefined,
    })
  }

  const handleEditSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (editingProxy) {
      updateMutation.mutate({
        id: editingProxy.id,
        data: {
          host: editFormData.host,
          port: parseInt(editFormData.port),
          protocol: editFormData.protocol,
          username: editFormData.username || undefined,
          password: editFormData.password || undefined,
        },
      })
    }
  }

  const startEditing = (proxy: Proxy) => {
    setEditingProxy(proxy)
    setEditFormData({
      host: proxy.host,
      port: proxy.port.toString(),
      protocol: proxy.protocol,
      username: proxy.username || '',
      password: proxy.password || '',
    })
  }

  // Column definitions for DataTable
  const columns: ColumnDef<Proxy>[] = useMemo(() => [
    createSelectionColumn<Proxy>(),
    {
      accessorFn: (row) => `${row.host}:${row.port}`,
      id: 'host',
      header: 'Host',
      cell: ({ row }) => (
        <span className="font-mono text-sm">{row.original.host}:{row.original.port}</span>
      ),
    },
    {
      accessorKey: 'protocol',
      header: 'Protocol',
      cell: ({ getValue }) => (
        <span className="uppercase text-xs font-medium">{getValue<string>()}</span>
      ),
    },
    {
      accessorKey: 'connector_name',
      header: 'Connector',
      enableSorting: false,
      cell: ({ getValue }) => getValue<string>() || '-',
    },
    {
      accessorKey: 'username',
      header: 'Auth',
      enableSorting: false,
      cell: ({ getValue }) => getValue<string>() ? '✓' : '-',
    },
    {
      accessorKey: 'status',
      header: 'Status',
      cell: ({ getValue }) => <StatusBadge status={getValue<string>()} />,
    },
    {
      accessorKey: 'request_count',
      header: 'Requests',
    },
    {
      accessorKey: 'success_rate',
      header: 'Success Rate',
      cell: ({ getValue }) => `${getValue<number>().toFixed(1)}%`,
    },
    {
      accessorKey: 'avg_latency_ms',
      header: 'Latency',
      cell: ({ getValue }) => `${getValue<number>().toFixed(0)}ms`,
    },
    {
      id: 'actions',
      header: 'Actions',
      enableSorting: false,
      cell: ({ row }) => (
        <div className="flex gap-1">
          <button
            onClick={() => startEditing(row.original)}
            className="p-1 text-blue-600 hover:text-blue-800 hover:bg-blue-50 rounded"
            title="Edit"
          >
            <Pencil className="w-4 h-4" />
          </button>
          <button
            onClick={() => deleteMutation.mutate(row.original.id)}
            className="p-1 text-red-600 hover:text-red-800 hover:bg-red-50 rounded"
            title="Delete"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    },
  ], [deleteMutation])

  if (isLoading) return <div>Loading...</div>

  return (
    <div>
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold">Proxies</h1>
        <div className="flex gap-2">
          {selectedProxies.length > 0 && (
            <button
              onClick={handleBulkDelete}
              disabled={isBulkDeleting}
              className="flex items-center gap-2 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Trash2 className="w-5 h-5" />
              {isBulkDeleting ? 'Deleting...' : `Delete ${selectedProxies.length} selected`}
            </button>
          )}
          <button
            onClick={() => setShowUploadModal(true)}
            className="flex items-center gap-2 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700"
            disabled={staticConnectors.length === 0}
            title={staticConnectors.length === 0 ? 'Create a Static Proxy Provider connector first' : 'Upload proxies from CSV'}
          >
            <Upload className="w-5 h-5" />
            Upload CSV
          </button>
          <button
            onClick={() => setShowForm(!showForm)}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
          >
            <Plus className="w-5 h-5" />
            Add Proxy
          </button>
        </div>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 mb-8">
          <div className="grid grid-cols-3 gap-4 mb-4">
            <select
              value={formData.connector_id}
              onChange={(e) => setFormData({ ...formData, connector_id: e.target.value })}
              className="px-4 py-2 border rounded-lg"
              required
            >
              <option value="">Select Connector</option>
              {staticConnectors.map((connector) => (
                <option key={connector.id} value={connector.id}>
                  {connector.name}
                </option>
              ))}
            </select>
            <input
              type="text"
              placeholder="Host"
              value={formData.host}
              onChange={(e) => setFormData({ ...formData, host: e.target.value })}
              className="px-4 py-2 border rounded-lg"
              required
            />
            <input
              type="number"
              placeholder="Port"
              value={formData.port}
              onChange={(e) => setFormData({ ...formData, port: e.target.value })}
              className="px-4 py-2 border rounded-lg"
              required
            />
          </div>
          <div className="grid grid-cols-3 gap-4">
            <select
              value={formData.protocol}
              onChange={(e) => setFormData({ ...formData, protocol: e.target.value })}
              className="px-4 py-2 border rounded-lg"
            >
              <option value="http">HTTP</option>
              <option value="https">HTTPS</option>
              <option value="socks4">SOCKS4</option>
              <option value="socks5">SOCKS5</option>
            </select>
            <input
              type="text"
              placeholder="Username (optional)"
              value={formData.username}
              onChange={(e) => setFormData({ ...formData, username: e.target.value })}
              className="px-4 py-2 border rounded-lg"
            />
            <input
              type="password"
              placeholder="Password (optional)"
              value={formData.password}
              onChange={(e) => setFormData({ ...formData, password: e.target.value })}
              className="px-4 py-2 border rounded-lg"
            />
          </div>
          {staticConnectors.length === 0 && (
            <p className="mt-2 text-sm text-amber-600">
              No static proxy provider connectors available. Create a connector with a Static Proxy Provider credential first.
            </p>
          )}
          <button
            type="submit"
            disabled={!formData.connector_id || createMutation.isPending}
            className="mt-4 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:bg-gray-400 disabled:cursor-not-allowed"
          >
            {createMutation.isPending ? 'Creating...' : 'Create Proxy'}
          </button>
        </form>
      )}

      {/* Edit form modal */}
      {editingProxy && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <form onSubmit={handleEditSubmit} className="bg-white rounded-lg shadow-lg p-6 w-full max-w-md">
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-xl font-semibold">Edit Proxy</h2>
              <button type="button" onClick={() => setEditingProxy(null)} className="text-gray-500 hover:text-gray-700">
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="grid gap-4">
              <input
                type="text"
                placeholder="Host"
                value={editFormData.host}
                onChange={(e) => setEditFormData({ ...editFormData, host: e.target.value })}
                className="px-4 py-2 border rounded-lg"
                required
              />
              <input
                type="number"
                placeholder="Port"
                value={editFormData.port}
                onChange={(e) => setEditFormData({ ...editFormData, port: e.target.value })}
                className="px-4 py-2 border rounded-lg"
                required
              />
              <select
                value={editFormData.protocol}
                onChange={(e) => setEditFormData({ ...editFormData, protocol: e.target.value })}
                className="px-4 py-2 border rounded-lg"
              >
                <option value="http">HTTP</option>
                <option value="https">HTTPS</option>
                <option value="socks4">SOCKS4</option>
                <option value="socks5">SOCKS5</option>
              </select>
              <input
                type="text"
                placeholder="Username (optional)"
                value={editFormData.username}
                onChange={(e) => setEditFormData({ ...editFormData, username: e.target.value })}
                className="px-4 py-2 border rounded-lg"
              />
              <input
                type="password"
                placeholder="Password (optional)"
                value={editFormData.password}
                onChange={(e) => setEditFormData({ ...editFormData, password: e.target.value })}
                className="px-4 py-2 border rounded-lg"
              />
            </div>
            <div className="flex gap-2 mt-4">
              <button
                type="submit"
                disabled={updateMutation.isPending}
                className="flex-1 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50"
              >
                {updateMutation.isPending ? 'Saving...' : 'Save'}
              </button>
              <button
                type="button"
                onClick={() => setEditingProxy(null)}
                className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300"
              >
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Upload modal */}
      {showUploadModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-lg p-6 w-full max-w-lg">
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-xl font-semibold">Upload Proxies</h2>
              <button type="button" onClick={closeUploadModal} className="text-gray-500 hover:text-gray-700">
                <X className="w-5 h-5" />
              </button>
            </div>

            {!uploadResult ? (
              <form onSubmit={handleUpload}>
                <div className="mb-4">
                  <label className="block text-sm font-medium text-gray-700 mb-2">Connector</label>
                  <select
                    value={uploadConnectorId}
                    onChange={(e) => setUploadConnectorId(e.target.value)}
                    className="w-full px-4 py-2 border rounded-lg"
                    required
                  >
                    <option value="">Select Connector</option>
                    {staticConnectors.map((connector) => (
                      <option key={connector.id} value={connector.id}>
                        {connector.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="mb-4">
                  <label className="block text-sm font-medium text-gray-700 mb-2">CSV File</label>
                  <input
                    type="file"
                    ref={fileInputRef}
                    accept=".csv,.txt"
                    className="w-full px-4 py-2 border rounded-lg"
                    required
                  />
                  <p className="text-sm text-gray-500 mt-2">
                    One proxy per line: <code className="bg-gray-100 px-1 rounded">protocol://[user:pass@]host:port</code>
                  </p>
                </div>

                {uploadError && (
                  <div className="mb-4 p-3 bg-red-100 text-red-700 rounded-lg">
                    {uploadError}
                  </div>
                )}

                <div className="flex gap-2">
                  <button
                    type="submit"
                    disabled={uploadMutation.isPending}
                    className="flex-1 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50"
                  >
                    {uploadMutation.isPending ? 'Uploading...' : 'Upload'}
                  </button>
                  <button
                    type="button"
                    onClick={closeUploadModal}
                    className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300"
                  >
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <div>
                <div className="mb-4 p-4 bg-gray-50 rounded-lg">
                  <div className="grid grid-cols-3 gap-4 text-center">
                    <div>
                      <p className="text-2xl font-bold text-gray-800">{uploadResult.total_lines}</p>
                      <p className="text-sm text-gray-500">Total Lines</p>
                    </div>
                    <div>
                      <p className="text-2xl font-bold text-green-600">{uploadResult.successful}</p>
                      <p className="text-sm text-gray-500">Successful</p>
                    </div>
                    <div>
                      <p className="text-2xl font-bold text-red-600">{uploadResult.failed}</p>
                      <p className="text-sm text-gray-500">Failed</p>
                    </div>
                  </div>
                </div>

                {uploadResult.errors.length > 0 && (
                  <div className="mb-4">
                    <h3 className="text-sm font-medium text-gray-700 mb-2">Errors:</h3>
                    <div className="max-h-40 overflow-y-auto bg-red-50 rounded-lg p-3">
                      {uploadResult.errors.map((err, idx) => (
                        <div key={idx} className="text-sm text-red-700 mb-1">
                          <span className="font-medium">Line {err.line_number}:</span> {err.error}
                          <br />
                          <code className="text-xs bg-red-100 px-1 rounded">{err.line}</code>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <button
                  onClick={closeUploadModal}
                  className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
                >
                  Done
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      <DataTable
        columns={columns}
        data={data?.proxies ?? []}
        defaultPageSize={10}
        emptyMessage="No proxies configured."
        enableRowSelection
        onSelectionChange={handleSelectionChange}
        getRowId={(row) => row.id}
      />
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    healthy: 'bg-green-100 text-green-800',
    degraded: 'bg-yellow-100 text-yellow-800',
    unhealthy: 'bg-red-100 text-red-800',
    unknown: 'bg-gray-100 text-gray-800',
  }
  return (
    <span className={`px-2 py-1 rounded-full text-xs font-medium ${colors[status] ?? colors.unknown}`}>
      {status}
    </span>
  )
}

