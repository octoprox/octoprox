// Copyright 2025 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useMemo, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Plus, Trash2, Pencil, Upload } from 'lucide-react'
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
import { formatBytes } from '../utils/format'
import { Button, Input, Select, Label, Modal, ModalHeader, Badge, Alert } from './ui'

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
        <span className="font-mono text-sm">
          {row.original.host}:{row.original.port}
          {row.original.username && <span className="ml-1 text-gray-400" title="Authenticated">🔐</span>}
        </span>
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
      accessorKey: 'status',
      header: 'Status',
      cell: ({ row }) => (
        <StatusBadge
          status={row.original.status}
          connectorEnabled={row.original.connector_enabled}
        />
      ),
    },
    {
      accessorKey: 'request_count',
      header: 'Reqs',
    },
    {
      accessorKey: 'success_rate',
      header: 'Success',
      cell: ({ getValue }) => `${getValue<number>().toFixed(1)}%`,
    },
    {
      accessorKey: 'avg_latency_ms',
      header: 'Latency',
      cell: ({ getValue }) => `${getValue<number>().toFixed(0)}ms`,
    },
    {
      id: 'traffic',
      header: 'Traffic',
      enableSorting: false,
      cell: ({ row }) => (
        <span className="text-sm whitespace-nowrap">
          <span className="text-orange-600">↑{formatBytes(row.original.bytes_sent)}</span>
          {' '}
          <span className="text-teal-600">↓{formatBytes(row.original.bytes_received)}</span>
        </span>
      ),
    },
    {
      id: 'actions',
      header: '',
      enableSorting: false,
      cell: ({ row }) => (
        <div className="flex gap-1">
          <button
            onClick={() => startEditing(row.original)}
            className="p-1 text-blue-600 hover:text-blue-800 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded"
            title="Edit"
          >
            <Pencil className="w-4 h-4" />
          </button>
          <button
            onClick={() => deleteMutation.mutate(row.original.id)}
            className="p-1 text-red-600 hover:text-red-800 hover:bg-red-50 dark:hover:bg-red-900/30 rounded"
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
            <Button
              variant="danger"
              onClick={handleBulkDelete}
              disabled={isBulkDeleting}
            >
              <Trash2 className="w-5 h-5" />
              {isBulkDeleting ? 'Deleting...' : `Delete ${selectedProxies.length} selected`}
            </Button>
          )}
          <Button
            variant="success"
            onClick={() => setShowUploadModal(true)}
            disabled={staticConnectors.length === 0}
            title={staticConnectors.length === 0 ? 'Create a Static Proxy Provider connector first' : 'Upload proxies from CSV'}
          >
            <Upload className="w-5 h-5" />
            Upload CSV
          </Button>
          <Button onClick={() => setShowForm(true)}>
            <Plus className="w-5 h-5" />
            Add Proxy
          </Button>
        </div>
      </div>

      {/* Add Proxy Modal */}
      {showForm && (
        <Modal onClose={() => setShowForm(false)} className="p-6">
          <ModalHeader title="Add Proxy" onClose={() => setShowForm(false)} />
          <form onSubmit={handleSubmit}>
            <div className="grid gap-4">
              <Select
                value={formData.connector_id}
                onChange={(e) => setFormData({ ...formData, connector_id: e.target.value })}
                required
              >
                <option value="">Select Connector</option>
                {staticConnectors.map((connector) => (
                  <option key={connector.id} value={connector.id}>
                    {connector.name}
                  </option>
                ))}
              </Select>
              <Input type="text" placeholder="Host" value={formData.host} onChange={(e) => setFormData({ ...formData, host: e.target.value })} required />
              <Input type="number" placeholder="Port" value={formData.port} onChange={(e) => setFormData({ ...formData, port: e.target.value })} required />
              <Select value={formData.protocol} onChange={(e) => setFormData({ ...formData, protocol: e.target.value })}>
                <option value="http">HTTP</option>
                <option value="https">HTTPS</option>
                <option value="socks4">SOCKS4</option>
                <option value="socks5">SOCKS5</option>
              </Select>
              <Input type="text" placeholder="Username (optional)" value={formData.username} onChange={(e) => setFormData({ ...formData, username: e.target.value })} />
              <Input type="password" placeholder="Password (optional)" value={formData.password} onChange={(e) => setFormData({ ...formData, password: e.target.value })} />
              {staticConnectors.length === 0 && (
                <p className="text-sm text-amber-600">
                  No static proxy provider connectors available. Create a connector with a Static Proxy Provider credential first.
                </p>
              )}
            </div>
            <div className="flex gap-2 mt-4">
              <Button
                type="submit"
                variant="success"
                disabled={!formData.connector_id || createMutation.isPending}
                className="flex-1"
              >
                {createMutation.isPending ? 'Creating...' : 'Create Proxy'}
              </Button>
              <Button type="button" variant="secondary" onClick={() => setShowForm(false)}>
                Cancel
              </Button>
            </div>
          </form>
        </Modal>
      )}

      {/* Edit form modal */}
      {editingProxy && (
        <Modal onClose={() => setEditingProxy(null)} className="p-6">
          <ModalHeader title="Edit Proxy" onClose={() => setEditingProxy(null)} />
          <form onSubmit={handleEditSubmit}>
            <div className="grid gap-4">
              <Input type="text" placeholder="Host" value={editFormData.host} onChange={(e) => setEditFormData({ ...editFormData, host: e.target.value })} required />
              <Input type="number" placeholder="Port" value={editFormData.port} onChange={(e) => setEditFormData({ ...editFormData, port: e.target.value })} required />
              <Select value={editFormData.protocol} onChange={(e) => setEditFormData({ ...editFormData, protocol: e.target.value })}>
                <option value="http">HTTP</option>
                <option value="https">HTTPS</option>
                <option value="socks4">SOCKS4</option>
                <option value="socks5">SOCKS5</option>
              </Select>
              <Input type="text" placeholder="Username (optional)" value={editFormData.username} onChange={(e) => setEditFormData({ ...editFormData, username: e.target.value })} />
              <Input type="password" placeholder="Password (optional)" value={editFormData.password} onChange={(e) => setEditFormData({ ...editFormData, password: e.target.value })} />
            </div>
            <div className="flex gap-2 mt-4">
              <Button
                type="submit"
                variant="success"
                disabled={updateMutation.isPending}
                className="flex-1"
              >
                {updateMutation.isPending ? 'Saving...' : 'Save'}
              </Button>
              <Button type="button" variant="secondary" onClick={() => setEditingProxy(null)}>
                Cancel
              </Button>
            </div>
          </form>
        </Modal>
      )}

      {/* Upload modal */}
      {showUploadModal && (
        <Modal onClose={closeUploadModal} className="max-w-lg p-6">
          <ModalHeader title="Upload Proxies" onClose={closeUploadModal} />

          {!uploadResult ? (
            <form onSubmit={handleUpload}>
              <div className="mb-4">
                <Label className="mb-2">Connector</Label>
                <Select
                  value={uploadConnectorId}
                  onChange={(e) => setUploadConnectorId(e.target.value)}
                  required
                >
                  <option value="">Select Connector</option>
                  {staticConnectors.map((connector) => (
                    <option key={connector.id} value={connector.id}>
                      {connector.name}
                    </option>
                  ))}
                </Select>
              </div>

              <div className="mb-4">
                <Label className="mb-2">CSV File</Label>
                <Input
                  type="file"
                  ref={fileInputRef}
                  accept=".csv,.txt"
                  required
                />
                <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
                  One proxy per line: <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">protocol://[user:pass@]host:port</code>
                </p>
              </div>

              {uploadError && (
                <Alert className="mb-4">{uploadError}</Alert>
              )}

              <div className="flex gap-2">
                <Button
                  type="submit"
                  variant="success"
                  disabled={uploadMutation.isPending}
                  className="flex-1"
                >
                  {uploadMutation.isPending ? 'Uploading...' : 'Upload'}
                </Button>
                <Button type="button" variant="secondary" onClick={closeUploadModal}>
                  Cancel
                </Button>
              </div>
            </form>
          ) : (
            <div>
              <div className="mb-4 p-4 bg-gray-50 dark:bg-gray-700 rounded-lg">
                <div className="grid grid-cols-3 gap-4 text-center">
                  <div>
                    <p className="text-2xl font-bold text-gray-800 dark:text-gray-200">{uploadResult.total_lines}</p>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Total Lines</p>
                  </div>
                  <div>
                    <p className="text-2xl font-bold text-green-600">{uploadResult.successful}</p>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Successful</p>
                  </div>
                  <div>
                    <p className="text-2xl font-bold text-red-600">{uploadResult.failed}</p>
                    <p className="text-sm text-gray-500 dark:text-gray-400">Failed</p>
                  </div>
                </div>
              </div>

              {uploadResult.errors.length > 0 && (
                <div className="mb-4">
                  <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Errors:</h3>
                  <div className="max-h-40 overflow-y-auto bg-red-50 dark:bg-red-900/20 rounded-lg p-3">
                    {uploadResult.errors.map((err, idx) => (
                      <div key={idx} className="text-sm text-red-700 dark:text-red-400 mb-1">
                        <span className="font-medium">Line {err.line_number}:</span> {err.error}
                        <br />
                        <code className="text-xs bg-red-100 dark:bg-red-900/40 px-1 rounded">{err.line}</code>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <Button onClick={closeUploadModal} className="w-full">
                Done
              </Button>
            </div>
          )}
        </Modal>
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

const STATUS_COLORS: Record<string, 'green' | 'blue' | 'yellow' | 'red' | 'gray' | 'orange' | 'purple' | 'slate'> = {
  healthy: 'green',
  initializing: 'blue',
  degraded: 'yellow',
  unhealthy: 'red',
  unknown: 'gray',
  draining: 'orange',
  terminating: 'purple',
  disabled: 'slate',
}

function StatusBadge({ status, connectorEnabled = true }: { status: string; connectorEnabled?: boolean }) {
  const displayStatus = connectorEnabled ? status : 'disabled'
  return (
    <Badge color={STATUS_COLORS[displayStatus] ?? 'gray'}>
      {displayStatus}
    </Badge>
  )
}
