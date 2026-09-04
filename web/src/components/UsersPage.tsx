// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Plus, Trash2, Send, Copy, Check } from 'lucide-react'
import { Button, Input, Label, Badge, Alert, Inspector, InspectorSection, KeyValue, ConfirmDialog } from './ui'
import { DataTable } from './DataTable'
import { Page } from './layout/Page'
import { RichSelect, RichSelectOption } from './RichSelect'
import { useAuth } from '../contexts/AuthContext'
import { formatDate, formatDateTime } from '../utils/format'
import { useToast } from '../contexts/ToastContext'
import {
  fetchUsers, createUser, inviteUser, reinviteUser, updateUser, deleteUser,
  UserAccount, UserCreate, UserInviteCreate, InviteResponse, UserUpdate, UserRole,
} from '../api/client'

const roleBadgeColor: Record<UserRole, 'red' | 'blue' | 'gray'> = { admin: 'red', editor: 'blue', viewer: 'gray' }

const roleOptions: RichSelectOption[] = [
  { value: 'admin', label: 'Admin', description: 'Full access including user management' },
  { value: 'editor', label: 'Editor', description: 'Manage proxies, credentials, connectors, and projects' },
  { value: 'viewer', label: 'Viewer', description: 'Read-only access to all resources' },
]

type PanelState = { kind: 'edit'; id: string } | { kind: 'new' } | null

export default function UsersPage() {
  const queryClient = useQueryClient()
  const { authStatus } = useAuth()
  const toast = useToast()
  const [panel, setPanel] = useState<PanelState>(null)
  const [pendingDelete, setPendingDelete] = useState<UserAccount | null>(null)
  const [inviteUrl, setInviteUrl] = useState<string | null>(null)

  const { data, isLoading } = useQuery({ queryKey: ['users'], queryFn: fetchUsers })
  const users = data?.users ?? []

  const reinviteMutation = useMutation({
    mutationFn: (userId: string) => reinviteUser(userId),
    onSuccess: (resp: InviteResponse, userId) => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      setInviteUrl(resp.invite_url)
      setPanel({ kind: 'edit', id: userId })
    },
    onError: (err: any) => toast.show(err.message || 'Failed to regenerate invite', 'error'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteUser(id),
    onSuccess: (_r, id) => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      if (panel?.kind === 'edit' && panel.id === id) setPanel(null)
      setPendingDelete(null)
      toast.show('User deleted')
    },
    onError: (err: any) => { setPendingDelete(null); toast.show(err.message || 'Failed to delete user', 'error') },
  })

  const columns: ColumnDef<UserAccount, unknown>[] = useMemo(() => [
    {
      accessorKey: 'username',
      header: 'User',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-2.5 max-w-full">
          <span className="w-6 h-6 rounded-full bg-primary-soft text-primary-soft-fg flex items-center justify-center text-[10px] font-semibold uppercase flex-none">{row.original.username.slice(0, 2)}</span>
          <span className="font-medium truncate">{row.original.username}</span>
          {row.original.id === authStatus?.user_id && <span className="text-xs text-fg-subtle">you</span>}
        </span>
      ),
    },
    {
      accessorKey: 'email',
      header: 'Email',
      meta: { filterVariant: 'text' as const },
      cell: ({ getValue }) => <span className="text-fg-muted truncate block">{getValue<string>() || '—'}</span>,
    },
    {
      accessorKey: 'role',
      header: 'Role',
      size: 100,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <Badge color={roleBadgeColor[getValue<UserRole>()]}>{getValue<UserRole>()}</Badge>,
    },
    {
      accessorFn: (row: UserAccount) => (!row.has_password ? 'Invited' : row.is_active ? 'Active' : 'Disabled'),
      id: 'status',
      header: 'Status',
      size: 120,
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => !row.original.has_password
        ? <Badge color="yellow">Invite pending</Badge>
        : <Badge color={row.original.is_active ? 'green' : 'gray'}>{row.original.is_active ? 'Active' : 'Disabled'}</Badge>,
    },
    {
      accessorKey: 'created_at',
      header: 'Created',
      size: 120,
      cell: ({ getValue }) => <span className="text-fg-muted">{formatDate(getValue<string>())}</span>,
    },
    {
      id: 'actions',
      header: '',
      size: 80,
      enableSorting: false,
      cell: ({ row }) => (
        <div className="flex items-center justify-end gap-0.5">
          {!row.original.has_password && (
            <button onClick={() => reinviteMutation.mutate(row.original.id)} className="p-1 rounded text-fg-subtle hover:text-warning hover:bg-warning-soft" title="Regenerate invite link">
              <Send className="w-4 h-4" />
            </button>
          )}
          {row.original.id !== authStatus?.user_id && (
            <button onClick={() => setPendingDelete(row.original)} className="p-1 rounded text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Delete user">
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>
      ),
    },
  ], [reinviteMutation, authStatus?.user_id])

  const openUser = panel?.kind === 'edit' ? users.find((u) => u.id === panel.id) ?? null : null

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'edit' && openUser) {
    panelNode = (
      <UserPanel
        key={openUser.id}
        user={openUser}
        isSelf={openUser.id === authStatus?.user_id}
        inviteUrl={inviteUrl}
        onClose={() => { setPanel(null); setInviteUrl(null) }}
        onDelete={() => setPendingDelete(openUser)}
        onReinvite={() => reinviteMutation.mutate(openUser.id)}
        onSaved={() => toast.show('User updated')}
      />
    )
  } else if (panel?.kind === 'new') {
    panelNode = (
      <UserPanel
        key="new"
        inviteUrl={null}
        onClose={() => setPanel(null)}
        onCreated={(u, url) => {
          // An invite link must be shown once, so that case stays open; otherwise close.
          setInviteUrl(url)
          setPanel(url ? { kind: 'edit', id: u.id } : null)
          toast.show(url ? 'Invite created' : `User "${u.username}" created`)
        }}
        onSaved={() => {}}
      />
    )
  }

  return (
    <Page
      title="Users"
      count={data?.total}
      subtitle="Who can sign in to this Octoprox instance. Admins manage users and backups."
      actions={<Button size="sm" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> Add user</Button>}
      panel={panelNode}
    >
      {isLoading ? (
        <div className="text-sm text-fg-muted py-10 text-center">Loading…</div>
      ) : (
        <DataTable
          columns={columns}
          data={users}
          defaultPageSize={20}
          emptyMessage="No users found."
          enableColumnFilters
          getRowId={(row) => row.id}
          onRowClick={(row) => { setInviteUrl(null); setPanel({ kind: 'edit', id: row.id }) }}
          activeRowId={panel?.kind === 'edit' ? panel.id : null}
          columnVisibility={panel ? { created_at: false, actions: false } : {}}
        />
      )}
      {pendingDelete && (
        <ConfirmDialog
          title="Delete user?"
          message={<>Remove <b className="text-fg">{pendingDelete.username}</b>. They lose access immediately. This cannot be undone.</>}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => deleteMutation.mutate(pendingDelete.id)}
          isLoading={deleteMutation.isPending}
        />
      )}
    </Page>
  )
}

function UserPanel({ user, isSelf, inviteUrl, onClose, onDelete, onReinvite, onSaved, onCreated }: {
  user?: UserAccount
  isSelf?: boolean
  inviteUrl: string | null
  onClose: () => void
  onDelete?: () => void
  onReinvite?: () => void
  onSaved: () => void
  onCreated?: (user: UserAccount, inviteUrl: string | null) => void
}) {
  const queryClient = useQueryClient()
  const isCreate = !user
  const [username, setUsername] = useState(user?.username || '')
  const [email, setEmail] = useState(user?.email || '')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<UserRole>(user?.role || 'viewer')
  const [isActive, setIsActive] = useState(user?.is_active ?? true)
  const [useInvite, setUseInvite] = useState(isCreate)
  const [error, setError] = useState<string | null>(null)

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['users'] })
  const createMutation = useMutation({
    mutationFn: (data: UserCreate) => createUser(data),
    onSuccess: (u) => { invalidate(); onCreated?.(u, null) },
    onError: (err: any) => setError(err.message || 'Failed to create user'),
  })
  const inviteMutation = useMutation({
    mutationFn: (data: UserInviteCreate) => inviteUser(data),
    onSuccess: (resp: InviteResponse) => { invalidate(); onCreated?.(resp.user, resp.invite_url) },
    onError: (err: any) => setError(err.message || 'Failed to invite user'),
  })
  const updateMutation = useMutation({
    mutationFn: (data: UserUpdate) => updateUser(user!.id, data),
    onSuccess: () => { invalidate(); setPassword(''); onSaved() },
    onError: (err: any) => setError(err.message || 'Failed to update user'),
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (user) {
      const data: UserUpdate = {}
      if (username !== user.username) data.username = username
      if (email !== user.email) data.email = email
      if (password) data.password = password
      if (role !== user.role) data.role = role
      if (isActive !== user.is_active) data.is_active = isActive
      if (Object.keys(data).length === 0) return
      updateMutation.mutate(data)
    } else if (useInvite) {
      inviteMutation.mutate({ username, email, role })
    } else {
      createMutation.mutate({ username, email, password, role })
    }
  }

  const saving = createMutation.isPending || inviteMutation.isPending || updateMutation.isPending

  return (
    <Inspector
      title={isCreate ? 'Add user' : user!.username}
      subtitle={isCreate ? 'Invite by link or set a password' : `${user!.role} · ${!user!.has_password ? 'invite pending' : user!.is_active ? 'active' : 'disabled'}`}
      onClose={onClose}
      footer={
        <>
          {!isCreate && onDelete && !isSelf && <Button type="button" variant="danger-ghost" size="sm" onClick={onDelete}><Trash2 className="w-3.5 h-3.5" /> Delete</Button>}
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="user-form" size="sm" disabled={saving}>
            {saving ? 'Saving…' : isCreate ? (useInvite ? 'Send invite' : 'Create user') : 'Save changes'}
          </Button>
        </>
      }
    >
      {inviteUrl && <InviteLink url={inviteUrl} />}
      {!isCreate && !user!.has_password && !inviteUrl && onReinvite && (
        <Alert variant="warning" className="flex items-center justify-between gap-3 text-xs">
          <span>Invite pending. The link expires 7 days after it was created.</span>
          <Button type="button" variant="outline" size="sm" onClick={onReinvite}><Send className="w-3.5 h-3.5" /> New link</Button>
        </Alert>
      )}
      <form id="user-form" onSubmit={handleSubmit} className="space-y-3">
        {error && <Alert variant="error">{error}</Alert>}
        <div>
          <Label htmlFor="username">Username</Label>
          <Input id="username" value={username} onChange={(e) => setUsername(e.target.value)} required autoComplete="off" />
        </div>
        <div>
          <Label htmlFor="email">Email</Label>
          <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" />
        </div>
        <div>
          <Label>Role</Label>
          <RichSelect options={roleOptions} value={role} onChange={(val) => setRole(val as UserRole)} required disabled={isSelf} />
          {isSelf && <p className="text-xs text-fg-subtle mt-1">You cannot change your own role.</p>}
        </div>
        {isCreate && (
          <label className="flex items-center gap-2 text-[13px]">
            <input type="checkbox" checked={useInvite} onChange={(e) => setUseInvite(e.target.checked)} className="w-4 h-4" />
            Send an invite link instead of setting a password
          </label>
        )}
        {(!isCreate || !useInvite) && (
          <div>
            <Label htmlFor="password">{user ? 'New password' : 'Password'}</Label>
            <Input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required={isCreate && !useInvite} placeholder={user ? 'Leave blank to keep' : undefined} autoComplete="new-password" />
          </div>
        )}
        {user && !isSelf && (
          <label className="flex items-center gap-2 text-[13px]">
            <input type="checkbox" checked={isActive} onChange={(e) => setIsActive(e.target.checked)} className="w-4 h-4" />
            Active
          </label>
        )}
      </form>
      {user && (
        <InspectorSection title="Details">
          <KeyValue label="Theme" value={user.theme_preference || 'default'} />
          <KeyValue label="Created" value={formatDateTime(user.created_at)} />
          <KeyValue label="Updated" value={formatDateTime(user.updated_at)} />
        </InspectorSection>
      )}
    </Inspector>
  )
}

function InviteLink({ url }: { url: string }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(url)
    } catch {
      const input = document.createElement('input')
      input.value = url
      document.body.appendChild(input)
      input.select()
      document.execCommand('copy')
      document.body.removeChild(input)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <Alert variant="info" className="space-y-2">
      <p className="text-xs">Share this link with the user. They use it to set a password and sign in. It expires in 7 days.</p>
      <div className="flex items-center gap-2">
        <Input value={url} readOnly className="flex-1 font-mono text-xs h-8" onClick={(e) => (e.target as HTMLInputElement).select()} />
        <Button type="button" variant="outline" size="sm" onClick={handleCopy} className="flex-shrink-0">
          {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />} {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
    </Alert>
  )
}
