// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Plus, Trash2, Send, Copy, Check, KeyRound, Bot } from 'lucide-react'
import { Button, Input, Label, Badge, Alert, Inspector, InspectorSection, KeyValue, ConfirmDialog, QueryContent, Tabs, Segmented } from './ui'
import { DataTable, type DataTableFeatures } from './DataTable'
import { EmptyState, Page } from './layout/Page'
import { RichSelect, RichSelectOption } from './RichSelect'
import { ApiTokensPanel, LinkedSignInsPanel } from './AccessPanels'
import { EntityHistory } from './audit/EntityHistory'
import { ProviderMark } from './SignInProviderMark'
import { useAuth } from '../contexts/AuthContext'
import { formatDate, formatDateTime, relativeTime } from '../utils/format'
import { useToast } from '../contexts/ToastContext'
import {
  fetchUsers, createUser, inviteUser, reinviteUser, updateUser, deleteUser, createServiceAccount,
  UserAccount, UserCreate, UserInviteCreate, InviteResponse, UserUpdate, UserRole, AuthProviderKind,
  ServiceAccountCreate, UserListResponse,
} from '../api/client'

/** No way in yet: neither a password nor a linked provider account. */
const invitePending = (u: UserAccount) => !u.has_password && u.sign_in_providers.length === 0

const roleBadgeColor: Record<UserRole, 'red' | 'blue' | 'gray'> = { admin: 'red', editor: 'blue', viewer: 'gray' }

const roleOptions: RichSelectOption[] = [
  { value: 'admin', label: 'Admin', description: 'Full access including user management' },
  { value: 'editor', label: 'Editor', description: 'Manage proxies, credentials, connectors, and projects' },
  { value: 'viewer', label: 'Viewer', description: 'Read-only, except exit reports, which steer routing where a project avoids' },
]

type PanelState = { kind: 'edit'; id: string } | { kind: 'new' } | { kind: 'new-service' } | null

/** What an open user shows: their details, or their History in their place. */
type UserView = 'details' | 'history'

/** Which accounts the page lists: the people who sign in, or the service accounts programs use through API tokens. */
type AccountsView = 'people' | 'service'

const ACCOUNTS_VIEWS: { value: AccountsView; label: string }[] = [
  { value: 'people', label: 'People' },
  { value: 'service', label: 'Service accounts' },
]

export default function UsersPage() {
  const queryClient = useQueryClient()
  const { authStatus } = useAuth()
  const toast = useToast()
  const [accounts, setAccounts] = useState<AccountsView>('people')
  const [panel, setPanel] = useState<PanelState>(null)
  const [pendingDelete, setPendingDelete] = useState<UserAccount | null>(null)
  const [inviteUrl, setInviteUrl] = useState<string | null>(null)

  const usersQuery = useQuery({ queryKey: ['users'], queryFn: fetchUsers })
  const { data } = usersQuery
  const [people, serviceAccounts] = useMemo(() => {
    const all = data?.users ?? []
    return [all.filter((u) => u.kind !== 'service'), all.filter((u) => u.kind === 'service')]
  }, [data])
  const showingService = accounts === 'service'

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
    mutationFn: (user: UserAccount) => deleteUser(user.id),
    onSuccess: (_r, user) => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
      if (panel?.kind === 'edit' && panel.id === user.id) setPanel(null)
      setPendingDelete(null)
      toast.show(user.kind === 'service' ? 'Service account deleted' : 'User deleted')
    },
    onError: (err: any, user) => {
      setPendingDelete(null)
      toast.show(err.message || (user.kind === 'service' ? 'Failed to delete service account' : 'Failed to delete user'), 'error')
    },
  })

  const columns: ColumnDef<DataTableFeatures, UserAccount, unknown>[] = useMemo(() => [
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
      cell: ({ getValue }) => <span className="text-fg-muted truncate block">{getValue<string>() || '-'}</span>,
    },
    {
      accessorKey: 'role',
      header: 'Role',
      size: 100,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <Badge color={roleBadgeColor[getValue<UserRole>()]}>{getValue<UserRole>()}</Badge>,
    },
    {
      accessorFn: (row: UserAccount) => (!row.is_active ? 'Disabled' : invitePending(row) ? 'Invited' : 'Active'),
      id: 'status',
      header: 'Status',
      size: 120,
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => !row.original.is_active
        ? <Badge color="gray">Disabled</Badge>
        : invitePending(row.original)
          ? <Badge color="yellow">Invite pending</Badge>
          : <Badge color="green">Active</Badge>,
    },
    {
      accessorFn: (row: UserAccount) => (row.has_password ? ['password', ...row.sign_in_providers] : row.sign_in_providers).join(', ') || 'none',
      id: 'sign_in',
      header: 'Signs in with',
      size: 140,
      cell: ({ row }) => (
        <span className="flex items-center gap-1.5 text-fg-muted">
          {row.original.has_password && <KeyRound className="w-3.5 h-3.5" aria-label="Password" />}
          {row.original.sign_in_providers.map((p) => <ProviderMark key={p} kind={p as AuthProviderKind} className="w-3.5 h-3.5" />)}
          {!row.original.has_password && row.original.sign_in_providers.length === 0 && <span className="text-fg-subtle">none yet</span>}
        </span>
      ),
    },
    {
      accessorKey: 'last_login_at',
      header: 'Last login',
      size: 120,
      cell: ({ getValue }) => {
        const v = getValue<string | null>()
        return v
          ? <span className="text-fg-muted" title={formatDateTime(v)}>{relativeTime(v)}</span>
          : <span className="text-fg-subtle">never</span>
      },
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
            <button onClick={() => reinviteMutation.mutate(row.original.id)} className="p-1 rounded-sm text-fg-subtle hover:text-warning hover:bg-warning-soft" title={invitePending(row.original) ? 'Regenerate invite link' : 'Link to set a password'}>
              <Send className="w-4 h-4" />
            </button>
          )}
          {row.original.id !== authStatus?.user_id && (
            <button onClick={() => setPendingDelete(row.original)} className="p-1 rounded-sm text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Delete user">
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>
      ),
    },
  ], [reinviteMutation, authStatus?.user_id])

  const serviceColumns: ColumnDef<DataTableFeatures, UserAccount, unknown>[] = useMemo(() => [
    {
      accessorKey: 'username',
      header: 'Name',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-2.5 max-w-full">
          <span className="w-6 h-6 rounded-full bg-surface-raised text-fg-muted flex items-center justify-center flex-none"><Bot className="w-3.5 h-3.5" /></span>
          <span className="font-medium truncate">{row.original.username}</span>
        </span>
      ),
    },
    {
      accessorKey: 'role',
      header: 'Role',
      size: 100,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <Badge color={roleBadgeColor[getValue<UserRole>()]}>{getValue<UserRole>()}</Badge>,
    },
    {
      accessorFn: (row: UserAccount) => (row.is_active ? 'Active' : 'Disabled'),
      id: 'status',
      header: 'Status',
      size: 110,
      meta: { filterVariant: 'select' as const },
      cell: ({ row }) => row.original.is_active ? <Badge color="green">Active</Badge> : <Badge color="gray">Disabled</Badge>,
    },
    {
      accessorKey: 'token_count',
      header: 'Tokens',
      size: 90,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => {
        const n = getValue<number>()
        return n > 0 ? <span className="tabular-nums">{n}</span> : <span className="text-fg-subtle">none</span>
      },
    },
    {
      accessorKey: 'token_last_used_at',
      header: 'Last used',
      size: 120,
      cell: ({ getValue }) => {
        const v = getValue<string | null>()
        return v
          ? <span className="text-fg-muted" title={formatDateTime(v)}>{relativeTime(v)}</span>
          : <span className="text-fg-subtle">never</span>
      },
    },
    {
      accessorKey: 'created_at',
      header: 'Created',
      size: 120,
      cell: ({ getValue }) => <span className="text-fg-muted">{formatDate(getValue<string>())}</span>,
    },
  ], [])

  const openUser = panel?.kind === 'edit' ? data?.users.find((u) => u.id === panel.id) ?? null : null

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'edit' && openUser?.kind === 'service') {
    panelNode = (
      <ServiceAccountPanel
        key={openUser.id}
        account={openUser}
        onClose={() => setPanel(null)}
        onDelete={() => setPendingDelete(openUser)}
        onSaved={() => toast.show('Service account updated')}
      />
    )
  } else if (panel?.kind === 'edit' && openUser) {
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
  } else if (panel?.kind === 'new-service') {
    panelNode = (
      <ServiceAccountPanel
        key="new-service"
        onClose={() => setPanel(null)}
        onCreated={(account) => {
          // Its inspector next, where its first token is issued.
          setPanel({ kind: 'edit', id: account.id })
          toast.show(`Service account "${account.username}" created`)
        }}
        onSaved={() => {}}
      />
    )
  }

  const addServiceAccount = () => setPanel({ kind: 'new-service' })

  return (
    <Page
      title="Users"
      count={data ? (showingService ? serviceAccounts.length : people.length) : undefined}
      subtitle={showingService
        ? 'Accounts for scripts and services: a role and the API tokens you issue them, never a password or an email.'
        : 'Who can sign in to this Octoprox instance. Admins manage users and backups.'}
      belowTitle={
        <Segmented
          size="sm"
          options={ACCOUNTS_VIEWS}
          value={accounts}
          onChange={(next) => { setAccounts(next); setPanel(null); setInviteUrl(null) }}
          className="pointer-coarse:*:min-h-11"
        />
      }
      actions={showingService
        ? <Button size="sm" onClick={addServiceAccount} className="pointer-coarse:h-11"><Plus className="w-3.5 h-3.5" /> Add service account</Button>
        : <Button size="sm" onClick={() => setPanel({ kind: 'new' })} className="pointer-coarse:h-11"><Plus className="w-3.5 h-3.5" /> Add user</Button>}
      panel={panelNode}
    >
      <QueryContent query={usersQuery} what="the users">
        {!showingService ? (
          <DataTable
            key="people"
            columns={columns}
            data={people}
            defaultPageSize={20}
            emptyMessage="No users found."
            enableColumnFilters
            getRowId={(row) => row.id}
            onRowClick={(row) => { setInviteUrl(null); setPanel({ kind: 'edit', id: row.id }) }}
            activeRowId={panel?.kind === 'edit' ? panel.id : null}
            columnVisibility={panel ? { last_login_at: false, created_at: false, actions: false } : {}}
          />
        ) : serviceAccounts.length === 0 ? (
          <EmptyState
            icon={<Bot />}
            title="No service accounts yet"
            description="A service account lets a script or a service call the API with tokens of its own, under no person's name. It has a role and never signs in."
            action={<Button size="sm" onClick={addServiceAccount} className="pointer-coarse:h-11"><Plus className="w-3.5 h-3.5" /> Add service account</Button>}
          />
        ) : (
          <DataTable
            key="service"
            columns={serviceColumns}
            data={serviceAccounts}
            defaultPageSize={20}
            emptyMessage="No service accounts found."
            enableColumnFilters
            getRowId={(row) => row.id}
            onRowClick={(row) => setPanel({ kind: 'edit', id: row.id })}
            activeRowId={panel?.kind === 'edit' ? panel.id : null}
            columnVisibility={panel ? { created_at: false } : {}}
          />
        )}
      </QueryContent>
      {pendingDelete && (
        <ConfirmDialog
          title={pendingDelete.kind === 'service' ? 'Delete service account?' : 'Delete user?'}
          message={pendingDelete.kind === 'service'
            ? <>Remove <b className="text-fg">{pendingDelete.username}</b> and its API tokens. Anything still using them gets 401 on its next call. This cannot be undone.</>
            : <>Remove <b className="text-fg">{pendingDelete.username}</b>. They lose access immediately. This cannot be undone.</>}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => deleteMutation.mutate(pendingDelete)}
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
  const [view, setView] = useState<UserView>('details')

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

  const handleSubmit = (e: React.SubmitEvent) => {
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
      subtitle={isCreate ? 'Invite by link, set a password, or let them sign in with a provider' : `${user!.role} · ${!user!.is_active ? 'disabled' : invitePending(user!) ? 'invite pending' : 'active'}`}
      onClose={onClose}
      footer={view === 'history' ? undefined : (
        <>
          {!isCreate && onDelete && !isSelf && <Button type="button" variant="danger-ghost" size="sm" onClick={onDelete}><Trash2 className="w-3.5 h-3.5" /> Delete</Button>}
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="user-form" size="sm" disabled={saving}>
            {saving ? 'Saving…' : isCreate ? (useInvite ? 'Send invite' : 'Create user') : 'Save changes'}
          </Button>
        </>
      )}
    >
      {user && (
        <Tabs<UserView>
          tabs={[{ id: 'details', label: 'Details' }, { id: 'history', label: 'History' }]}
          active={view}
          onChange={setView}
          className="pointer-coarse:*:min-h-11"
        />
      )}
      {/* Hidden rather than unmounted while History shows, so an edit not saved yet is still there afterwards. */}
      <div hidden={view === 'history'} className="space-y-4">
      {inviteUrl && <InviteLink url={inviteUrl} />}
      {!isCreate && !user!.has_password && !inviteUrl && onReinvite && (
        <Alert variant={invitePending(user!) ? 'warning' : 'info'} className="flex items-center justify-between gap-3 text-xs">
          <span>{invitePending(user!) ? 'Invite pending. The link expires 7 days after it was created.' : 'Signs in through a provider only. A link lets them add a password.'}</span>
          <Button type="button" variant="outline" size="sm" onClick={onReinvite}><Send className="w-3.5 h-3.5" /> {invitePending(user!) ? 'New link' : 'Set-password link'}</Button>
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
        {isCreate && (
          <p className="text-xs text-fg-subtle">
            With sign-in through Google, Microsoft or GitHub enabled, a user whose email matches can sign in straight away; neither link nor password is needed.
          </p>
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
        <InspectorSection title="Access">
          <div className="space-y-4">
            <LinkedSignInsPanel userId={user.id} compact />
            <ApiTokensPanel userId={user.id} compact />
          </div>
        </InspectorSection>
      )}
      {user && (
        <InspectorSection title="Details">
          <KeyValue label="Theme" value={user.theme_preference || 'default'} />
          <KeyValue label="Last login" value={user.last_login_at ? formatDateTime(user.last_login_at) : 'never'} />
          <KeyValue label="Created" value={formatDateTime(user.created_at)} />
          <KeyValue label="Updated" value={formatDateTime(user.updated_at)} />
        </InspectorSection>
      )}
      </div>

      {view === 'history' && user && <EntityHistory entityType="user" entityId={user.id} />}
    </Inspector>
  )
}

/** A service account's inspector, or the form that adds one: a name, a role, the active flag and its API tokens. */
function ServiceAccountPanel({ account, onClose, onDelete, onSaved, onCreated }: {
  account?: UserAccount
  onClose: () => void
  onDelete?: () => void
  onSaved: () => void
  onCreated?: (account: UserAccount) => void
}) {
  const queryClient = useQueryClient()
  const [username, setUsername] = useState(account?.username || '')
  const [role, setRole] = useState<UserRole>(account?.role || 'viewer')
  const [isActive, setIsActive] = useState(account?.is_active ?? true)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<UserView>('details')

  const createMutation = useMutation({
    mutationFn: (data: ServiceAccountCreate) => createServiceAccount(data),
    onSuccess: (created) => {
      // In the list at once, so its inspector opens without waiting for the refetch.
      queryClient.setQueryData<UserListResponse>(['users'], (old) => old && { total: old.total + 1, users: [...old.users, created] })
      queryClient.invalidateQueries({ queryKey: ['users'] })
      onCreated?.(created)
    },
    onError: (err: any) => setError(err.message || 'Failed to create service account'),
  })
  const updateMutation = useMutation({
    mutationFn: (data: UserUpdate) => updateUser(account!.id, data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['users'] }); onSaved() },
    onError: (err: any) => setError(err.message || 'Failed to update service account'),
  })

  const handleSubmit = (e: React.SubmitEvent) => {
    e.preventDefault()
    setError(null)
    if (!account) {
      createMutation.mutate({ username, role })
      return
    }
    const data: UserUpdate = {}
    if (username !== account.username) data.username = username
    if (role !== account.role) data.role = role
    if (isActive !== account.is_active) data.is_active = isActive
    if (Object.keys(data).length === 0) return
    updateMutation.mutate(data)
  }

  const saving = createMutation.isPending || updateMutation.isPending

  return (
    <Inspector
      title={account ? account.username : 'Add service account'}
      subtitle={account
        ? `service account · ${account.role} · ${account.is_active ? 'active' : 'disabled'}`
        : 'A name and a role, then its API tokens'}
      onClose={onClose}
      footer={view === 'history' ? undefined : (
        <>
          {account && onDelete && <Button type="button" variant="danger-ghost" size="sm" onClick={onDelete} className="pointer-coarse:h-11"><Trash2 className="w-3.5 h-3.5" /> Delete</Button>}
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={onClose} className="pointer-coarse:h-11">Cancel</Button>
          <Button type="submit" form="service-account-form" size="sm" disabled={saving} className="pointer-coarse:h-11">
            {saving ? (account ? 'Saving…' : 'Creating…') : account ? 'Save changes' : 'Create service account'}
          </Button>
        </>
      )}
    >
      {account && (
        <Tabs<UserView>
          tabs={[{ id: 'details', label: 'Details' }, { id: 'history', label: 'History' }]}
          active={view}
          onChange={setView}
          className="pointer-coarse:*:min-h-11"
        />
      )}
      {/* Hidden rather than unmounted while History shows, so an edit not saved yet is still there afterwards. */}
      <div hidden={view === 'history'} className="space-y-4">
      <form id="service-account-form" onSubmit={handleSubmit} className="space-y-3">
        {error && <Alert variant="error">{error}</Alert>}
        <div>
          <Label htmlFor="service-account-name">Name</Label>
          <Input id="service-account-name" value={username} onChange={(e) => setUsername(e.target.value)} required autoComplete="off" placeholder={account ? undefined : 'What uses it, e.g. scraper-eu'} />
        </div>
        <div>
          <Label>Role</Label>
          <RichSelect options={roleOptions} value={role} onChange={(val) => setRole(val as UserRole)} required />
          <p className="text-xs text-fg-subtle mt-1">Its tokens act with this role. Give it the lowest that does the job: a viewer reads everything and changes nothing, and can send exit reports.</p>
        </div>
        {account && (
          <div>
            <label className="flex items-center gap-2 text-[13px] pointer-coarse:min-h-11">
              <input type="checkbox" checked={isActive} onChange={(e) => setIsActive(e.target.checked)} className="w-4 h-4" />
              Active
            </label>
            <p className="text-xs text-fg-subtle">While it is off, its tokens are refused.</p>
          </div>
        )}
        {!account && (
          <p className="text-xs text-fg-subtle">
            It never signs in: no password, no email, no invite link. Once it is added, issue it an API token here and send that token with each call.
          </p>
        )}
      </form>
      {account && <ApiTokensPanel userId={account.id} />}
      {account && (
        <InspectorSection title="Details">
          <KeyValue label="Created" value={formatDateTime(account.created_at)} />
          <KeyValue label="Updated" value={formatDateTime(account.updated_at)} />
        </InspectorSection>
      )}
      </div>

      {view === 'history' && account && <EntityHistory entityType="user" entityId={account.id} />}
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
        <Button type="button" variant="outline" size="sm" onClick={handleCopy} className="shrink-0">
          {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />} {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
    </Alert>
  )
}
