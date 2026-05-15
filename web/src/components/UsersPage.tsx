// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { ColumnDef } from '@tanstack/react-table'
import { Plus, Trash2, Edit, ArrowLeft, Send, Copy, Check } from 'lucide-react'
import { Button, Input, Label, Badge, Alert, Modal, ModalHeader, ModalFooter } from './ui'
import { DataTable } from './DataTable'
import { RichSelect, RichSelectOption } from './RichSelect'
import { useTheme } from '../contexts/ThemeContext'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import {
  fetchUsers,
  createUser,
  inviteUser,
  reinviteUser,
  updateUser,
  deleteUser,
  UserAccount,
  UserCreate,
  UserInviteCreate,
  InviteResponse,
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
  const { isDark } = useTheme()
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [editingUser, setEditingUser] = useState<UserAccount | null>(null)
  const [deletingUser, setDeletingUser] = useState<UserAccount | null>(null)
  const [inviteUrl, setInviteUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const location = useLocation()
  // Embedded when the parent layout already provides chrome (project sidebar or settings page).
  const isEmbedded = !!projectId || location.pathname.startsWith('/settings')

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

  const inviteMutation = useMutation({
    mutationFn: (data: UserInviteCreate) => inviteUser(data),
    onSuccess: (resp: InviteResponse) => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      setShowCreateModal(false)
      setError(null)
      setInviteUrl(resp.invite_url)
    },
    onError: (err: any) => setError(err.message || 'Failed to invite user'),
  })

  const reinviteMutation = useMutation({
    mutationFn: (userId: string) => reinviteUser(userId),
    onSuccess: (resp: InviteResponse) => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      setInviteUrl(resp.invite_url)
    },
    onError: (err: any) => setError(err.message || 'Failed to regenerate invite'),
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
      cell: ({ getValue }) => {
        const v = getValue<string>()
        return <div className="font-medium truncate" title={v}>{v}</div>
      },
    },
    {
      accessorKey: 'email',
      header: 'Email',
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => {
        const v = getValue<string>() || ''
        return <div className="text-fg-muted truncate" title={v}>{v || '-'}</div>
      },
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
      accessorFn: (row: UserAccount) => {
        if (!row.has_password) return 'Invited'
        return row.is_active ? 'Active' : 'Disabled'
      },
      id: 'status',
      header: 'Status',
      size: 100,
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => {
        if (!row.original.has_password) {
          return <Badge color="yellow">Invited</Badge>
        }
        return (
          <Badge color={row.original.is_active ? 'green' : 'gray'}>
            {row.original.is_active ? 'Active' : 'Disabled'}
          </Badge>
        )
      },
    },
    {
      accessorKey: 'created_at',
      header: 'Created',
      size: 120,
      cell: ({ getValue }) => (
        <span className="text-fg-muted text-sm">
          {new Date(getValue<string>()).toLocaleDateString()}
        </span>
      ),
    },
    {
      id: 'actions',
      header: () => <span className="sr-only">Actions</span>,
      size: 110,
      enableSorting: false,
      cell: ({ row }) => (
        <div className="flex items-center justify-end gap-2">
          {!row.original.has_password && (
            <button
              onClick={() => reinviteMutation.mutate(row.original.id)}
              className="p-1.5 text-fg-subtle hover:text-warning hover:bg-warning-soft rounded transition-colors"
              title="Resend invite"
            >
              <Send className="w-4 h-4" />
            </button>
          )}
          <button
            onClick={() => { setError(null); setEditingUser(row.original) }}
            className="p-1.5 text-fg-subtle hover:text-primary hover:bg-primary-soft rounded transition-colors"
            title="Edit user"
          >
            <Edit className="w-4 h-4" />
          </button>
          <button
            onClick={() => setDeletingUser(row.original)}
            className="p-1.5 text-fg-subtle hover:text-danger hover:bg-danger-soft rounded transition-colors"
            title="Delete user"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    },
  ], [reinviteMutation])

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
        <div className="text-center text-fg-muted py-12">Loading users...</div>
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
          onInvite={(data) => inviteMutation.mutate(data)}
          isLoading={createMutation.isPending || inviteMutation.isPending}
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

      {/* Invite URL Modal */}
      {inviteUrl && (
        <InviteUrlModal
          url={inviteUrl}
          onClose={() => setInviteUrl(null)}
        />
      )}

      {/* Delete Confirmation Modal */}
      {deletingUser && (
        <Modal onClose={() => setDeletingUser(null)}>
          <div className="p-6">
            <ModalHeader title="Delete User" onClose={() => setDeletingUser(null)} />
            <p className="text-fg-muted">
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
    <div className="min-h-screen bg-bg text-fg">
      <div className="bg-surface border-b border-line">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center gap-4">
          <button
            onClick={() => navigate('/')}
            className="p-2 text-fg-muted hover:text-fg hover:bg-surface-raised rounded-lg transition-colors"
            title="Back to projects"
          >
            <ArrowLeft className="w-5 h-5" />
          </button>
          <img src={isDark ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-8" />
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
  onInvite,
  isLoading,
  error,
}: {
  title: string
  user?: UserAccount
  onClose: () => void
  onSubmit: (data: UserCreate | UserUpdate) => void
  onInvite?: (data: UserInviteCreate) => void
  isLoading: boolean
  error: string | null
}) {
  const isCreate = !user
  const [username, setUsername] = useState(user?.username || '')
  const [email, setEmail] = useState(user?.email || '')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<UserRole>(user?.role || 'viewer')
  const [isActive, setIsActive] = useState(user?.is_active ?? true)
  const [useInvite, setUseInvite] = useState(isCreate)

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
    } else if (useInvite && onInvite) {
      // Create via invite
      onInvite({ username, email, role })
    } else {
      // Create with password
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

          {/* Invite toggle — only shown on create */}
          {isCreate && (
            <div className="flex items-center gap-2">
              <input
                id="use_invite"
                type="checkbox"
                checked={useInvite}
                onChange={(e) => setUseInvite(e.target.checked)}
                className="rounded border-line-strong"
              />
              <Label htmlFor="use_invite" className="mb-0">Send invite link instead of setting password</Label>
            </div>
          )}

          {/* Password field — hidden when invite is on during create */}
          {(!isCreate || !useInvite) && (
            <div>
              <Label htmlFor="password">{user ? 'New Password (leave blank to keep)' : 'Password'}</Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required={isCreate && !useInvite}
              />
            </div>
          )}

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
                className="rounded border-line-strong"
              />
              <Label htmlFor="is_active" className="mb-0">Active</Label>
            </div>
          )}
        </div>

        <ModalFooter>
          <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={isLoading}>
            {isLoading ? 'Saving...' : isCreate && useInvite ? 'Send Invite' : user ? 'Update' : 'Create'}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  )
}

function InviteUrlModal({ url, onClose }: { url: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Fallback for older browsers
      const input = document.createElement('input')
      input.value = url
      document.body.appendChild(input)
      input.select()
      document.execCommand('copy')
      document.body.removeChild(input)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  return (
    <Modal onClose={onClose}>
      <div className="p-6">
        <ModalHeader title="Invite Link Created" onClose={onClose} />
        <p className="text-fg-muted mb-4">
          Share this link with the user. They will use it to set their password and log in. The link expires in 7 days.
        </p>
        <div className="flex items-center gap-2">
          <Input
            value={url}
            readOnly
            className="flex-1 font-mono text-sm"
            onClick={(e) => (e.target as HTMLInputElement).select()}
          />
          <Button type="button" variant="secondary" onClick={handleCopy} className="flex-shrink-0">
            {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
          </Button>
        </div>
        <ModalFooter>
          <Button onClick={onClose}>Done</Button>
        </ModalFooter>
      </div>
    </Modal>
  )
}
