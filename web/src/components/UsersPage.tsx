// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { ColumnDef } from '@tanstack/react-table'
import { Plus, Trash2, Edit, ArrowLeft } from 'lucide-react'
import { Button, Input, Label, Badge, Alert, Modal, ModalHeader, ModalFooter } from './ui'
import { DataTable } from './DataTable'
import { RichSelect, RichSelectOption } from './RichSelect'
import { useTheme } from '../contexts/ThemeContext'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import {
  fetchUsers,
  createUser,
  updateUser,
  deleteUser,
  UserAccount,
  UserCreate,
  UserUpdate,
  UserRole,
} from '../api/client'

const roleBadgeColor: Record<UserRole, 'red' | 'blue' | 'gray'> = {
  admin: 'red',
  editor: 'blue',
  viewer: 'gray',
}

const roleOptions: RichSelectOption[] = [
  { value: 'admin', label: 'Admin', description: 'Full access including user management' },
  { value: 'editor', label: 'Editor', description: 'Manage proxies, credentials, connectors, and projects' },
  { value: 'viewer', label: 'Viewer', description: 'Read-only access to all resources' },
]

export default function UsersPage() {
  const navigate = useNavigate()
  const { projectId } = useParams()
  const queryClient = useQueryClient()
  const { theme } = useTheme()
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [editingUser, setEditingUser] = useState<UserAccount | null>(null)
  const [deletingUser, setDeletingUser] = useState<UserAccount | null>(null)
  const [error, setError] = useState<string | null>(null)

  // When inside a project layout, we're embedded (sidebar provides navigation)
  const isEmbedded = !!projectId

  const { data, isLoading } = useQuery({
    queryKey: ['users'],
    queryFn: fetchUsers,
  })

  const createMutation = useMutation({
    mutationFn: (data: UserCreate) => createUser(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      setShowCreateModal(false)
      setError(null)
    },
    onError: (err: any) => setError(err.message || 'Failed to create user'),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: UserUpdate }) => updateUser(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      setEditingUser(null)
      setError(null)
    },
    onError: (err: any) => setError(err.message || 'Failed to update user'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteUser(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      setDeletingUser(null)
    },
    onError: (err: any) => setError(err.message || 'Failed to delete user'),
  })

  const columns: ColumnDef<UserAccount, unknown>[] = useMemo(() => [
    {
      accessorKey: 'username',
      header: 'Username',
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => (
        <span className="font-medium">{getValue<string>()}</span>
      ),
    },
    {
      accessorKey: 'email',
      header: 'Email',
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => (
        <span className="text-gray-500 dark:text-gray-400">{getValue<string>() || '-'}</span>
      ),
    },
    {
      accessorKey: 'role',
      header: 'Role',
      size: 100,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => {
        const role = getValue<UserRole>()
        return <Badge color={roleBadgeColor[role]}>{role}</Badge>
      },
    },
    {
      accessorFn: (row: UserAccount) => row.is_active ? 'Active' : 'Disabled',
      id: 'status',
      header: 'Status',
      size: 100,
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => (
        <Badge color={row.original.is_active ? 'green' : 'gray'}>
          {row.original.is_active ? 'Active' : 'Disabled'}
        </Badge>
      ),
    },
    {
      accessorKey: 'created_at',
      header: 'Created',
      size: 120,
      cell: ({ getValue }) => (
        <span className="text-gray-500 dark:text-gray-400 text-sm">
          {new Date(getValue<string>()).toLocaleDateString()}
        </span>
      ),
    },
    {
      id: 'actions',
      header: () => <span className="sr-only">Actions</span>,
      size: 80,
      enableSorting: false,
      cell: ({ row }) => (
        <div className="flex items-center justify-end gap-2">
          <button
            onClick={() => { setError(null); setEditingUser(row.original) }}
            className="p-1.5 text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded transition-colors"
            title="Edit user"
          >
            <Edit className="w-4 h-4" />
          </button>
          <button
            onClick={() => setDeletingUser(row.original)}
            className="p-1.5 text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded transition-colors"
            title="Delete user"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    },
  ], [])

  const usersContent = (
    <>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold">User Management</h1>
        <Button onClick={() => { setError(null); setShowCreateModal(true) }}>
          <Plus className="w-4 h-4 mr-2" />
          Add User
        </Button>
      </div>

      {error && (
        <Alert variant="error" className="mb-4">
          {error}
        </Alert>
      )}

      {isLoading ? (
        <div className="text-center text-gray-500 dark:text-gray-400 py-12">Loading users...</div>
      ) : (
        <DataTable
          columns={columns}
          data={data?.users ?? []}
          defaultPageSize={20}
          emptyMessage="No users found."
          enableColumnFilters
          getRowId={(row: UserAccount) => row.id}
        />
      )}

      {/* Create User Modal */}
      {showCreateModal && (
        <UserFormModal
          title="Create User"
          onClose={() => setShowCreateModal(false)}
          onSubmit={(data) => createMutation.mutate(data as UserCreate)}
          isLoading={createMutation.isPending}
          error={error}
        />
      )}

      {/* Edit User Modal */}
      {editingUser && (
        <UserFormModal
          title="Edit User"
          user={editingUser}
          onClose={() => setEditingUser(null)}
          onSubmit={(data) => updateMutation.mutate({ id: editingUser.id, data: data as UserUpdate })}
          isLoading={updateMutation.isPending}
          error={error}
        />
      )}

      {/* Delete Confirmation Modal */}
      {deletingUser && (
        <Modal onClose={() => setDeletingUser(null)}>
          <div className="p-6">
            <ModalHeader title="Delete User" onClose={() => setDeletingUser(null)} />
            <p className="text-gray-600 dark:text-gray-400">
              Are you sure you want to delete user <strong>{deletingUser.username}</strong>? This action cannot be undone.
            </p>
            <ModalFooter>
              <Button variant="secondary" onClick={() => setDeletingUser(null)}>Cancel</Button>
              <Button
                variant="danger"
                onClick={() => deleteMutation.mutate(deletingUser.id)}
                disabled={deleteMutation.isPending}
              >
                {deleteMutation.isPending ? 'Deleting...' : 'Delete'}
              </Button>
            </ModalFooter>
          </div>
        </Modal>
      )}
    </>
  )

  // When embedded in ProjectLayout, just render the content directly
  if (isEmbedded) {
    return usersContent
  }

  // Standalone page: wrap with its own header/chrome
  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-gray-100">
      <div className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center gap-4">
          <button
            onClick={() => navigate('/')}
            className="p-2 text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
            title="Back to projects"
          >
            <ArrowLeft className="w-5 h-5" />
          </button>
          <img src={theme === 'dark' ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-8" />
        </div>
      </div>
      <div className="max-w-6xl mx-auto px-6 py-8">
        {usersContent}
      </div>
    </div>
  )
}

function UserFormModal({
  title,
  user,
  onClose,
  onSubmit,
  isLoading,
  error,
}: {
  title: string
  user?: UserAccount
  onClose: () => void
  onSubmit: (data: UserCreate | UserUpdate) => void
  isLoading: boolean
  error: string | null
}) {
  const [username, setUsername] = useState(user?.username || '')
  const [email, setEmail] = useState(user?.email || '')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<UserRole>(user?.role || 'viewer')
  const [isActive, setIsActive] = useState(user?.is_active ?? true)

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (user) {
      // Update - only send changed fields
      const data: UserUpdate = {}
      if (username !== user.username) data.username = username
      if (email !== user.email) data.email = email
      if (password) data.password = password
      if (role !== user.role) data.role = role
      if (isActive !== user.is_active) data.is_active = isActive
      onSubmit(data)
    } else {
      // Create
      onSubmit({ username, email, password, role })
    }
  }

  return (
    <Modal onClose={onClose}>
      <form onSubmit={handleSubmit} className="p-6">
        <ModalHeader title={title} onClose={onClose} />
        {error && <Alert variant="error" className="mb-4">{error}</Alert>}

        <div className="space-y-4">
          <div>
            <Label htmlFor="username">Username</Label>
            <Input
              id="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
          <div>
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div>
            <Label htmlFor="password">{user ? 'New Password (leave blank to keep)' : 'Password'}</Label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required={!user}
            />
          </div>
          <div>
            <Label>Role</Label>
            <RichSelect
              options={roleOptions}
              value={role}
              onChange={(val) => setRole(val as UserRole)}
              required
            />
          </div>
          {user && (
            <div className="flex items-center gap-2">
              <input
                id="is_active"
                type="checkbox"
                checked={isActive}
                onChange={(e) => setIsActive(e.target.checked)}
                className="rounded border-gray-300 dark:border-gray-600"
              />
              <Label htmlFor="is_active" className="mb-0">Active</Label>
            </div>
          )}
        </div>

        <ModalFooter>
          <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={isLoading}>
            {isLoading ? 'Saving...' : user ? 'Update' : 'Create'}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  )
}
