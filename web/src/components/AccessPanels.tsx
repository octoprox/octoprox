// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, KeyRound, Plus, Trash2, Unlink } from 'lucide-react'
import {
  ApiTokenCreated, ApiTokenSummary, AuthIdentity,
  createApiToken, deleteApiToken, deleteAuthIdentity, fetchApiTokens, fetchAuthIdentities,
} from '../api/client'
import { useToast } from '../contexts/ToastContext'
import { formatDate, formatDateTime, relativeTime } from '../utils/format'
import { Alert, Badge, Button, ConfirmDialog, Input, Label, Modal, ModalHeader, Select } from './ui'
import { ProviderMark } from './SignInProviderMark'

/**
 * The two "how do I get in" panels, shared by the Account page (a person's
 * own) and the user inspector (an admin looking at someone else's). With a
 * userId they talk to the admin endpoints; without one, to /me.
 */

const EXPIRY_OPTIONS: { value: string; label: string }[] = [
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
  { value: '365', label: '1 year' },
  { value: '', label: 'Never' },
]

export function ApiTokensPanel({ userId, canCreate = true, compact = false }: { userId?: string; canCreate?: boolean; compact?: boolean }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const queryKey = ['api-tokens', userId ?? 'me']
  const { data: tokens = [], isLoading } = useQuery({ queryKey, queryFn: () => fetchApiTokens(userId) })
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<ApiTokenCreated | null>(null)
  const [pendingRevoke, setPendingRevoke] = useState<ApiTokenSummary | null>(null)

  const revoke = useMutation({
    mutationFn: (id: string) => deleteApiToken(id, userId),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey }); setPendingRevoke(null); toast.show('Token revoked') },
    onError: (e: Error) => { setPendingRevoke(null); toast.show(e.message || 'Failed to revoke token', 'error') },
  })

  return (
    <div>
      <div className="flex items-center justify-between gap-3 mb-2">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle">API tokens</div>
        {canCreate && (
          <Button type="button" size="sm" variant="outline" onClick={() => setCreating(true)}>
            <Plus className="w-3.5 h-3.5" /> New token
          </Button>
        )}
      </div>
      {!compact && (
        <p className="text-xs text-fg-muted mb-3">
          A token authenticates scripts and API clients as this user, with the user's current role, until it is revoked. Send it as <span className="font-mono">Authorization: Bearer octo_…</span>.
        </p>
      )}
      {isLoading ? (
        <div className="text-xs text-fg-muted">Loading…</div>
      ) : tokens.length === 0 ? (
        <div className="text-xs text-fg-muted">No tokens.</div>
      ) : (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {tokens.map((t) => (
            <li key={t.id} className="flex items-center gap-3 px-3 py-2">
              <KeyRound className="w-4 h-4 text-fg-subtle flex-none" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-medium truncate">{t.name}</span>
                  <span className="font-mono text-[11px] text-fg-subtle">{t.token_prefix}…</span>
                  {t.expired && <Badge color="red">expired</Badge>}
                </div>
                <div className="text-[11px] text-fg-muted">
                  {t.last_used_at ? `Last used ${relativeTime(t.last_used_at)}` : 'Never used'}
                  {' · '}{t.expires_at ? `expires ${formatDate(t.expires_at)}` : 'no expiry'}
                  {t.created_by && userId ? ` · issued by ${t.created_by}` : ''}
                </div>
              </div>
              <Button type="button" size="sm" variant="danger-ghost" onClick={() => setPendingRevoke(t)} title="Revoke">
                <Trash2 className="w-3.5 h-3.5" />
              </Button>
            </li>
          ))}
        </ul>
      )}

      {creating && (
        <CreateTokenDialog
          userId={userId}
          onClose={() => setCreating(false)}
          onCreated={(result) => { setCreating(false); setCreated(result); queryClient.invalidateQueries({ queryKey }) }}
        />
      )}
      {created && <ShowTokenDialog result={created} onClose={() => setCreated(null)} />}
      {pendingRevoke && (
        <ConfirmDialog
          title={`Revoke "${pendingRevoke.name}"?`}
          message="Anything still using this token gets 401 on its next call. This cannot be undone; issue a new token instead."
          confirmLabel="Revoke"
          onCancel={() => setPendingRevoke(null)}
          onConfirm={() => revoke.mutate(pendingRevoke.id)}
          isLoading={revoke.isPending}
        />
      )}
    </div>
  )
}

function CreateTokenDialog({ userId, onClose, onCreated }: { userId?: string; onClose: () => void; onCreated: (r: ApiTokenCreated) => void }) {
  const [name, setName] = useState('')
  const [expiry, setExpiry] = useState('90')
  const [error, setError] = useState<string | null>(null)
  const create = useMutation({
    mutationFn: () => createApiToken({ name: name.trim(), expires_in_days: expiry ? Number(expiry) : null }, userId),
    onSuccess: onCreated,
    onError: (e: Error) => setError(e.message || 'Failed to create token'),
  })
  return (
    <Modal onClose={onClose}>
      <form className="p-6" onSubmit={(e) => { e.preventDefault(); if (name.trim()) create.mutate() }}>
        <ModalHeader title="New API token" onClose={onClose} />
        {error && <Alert variant="error" className="mb-4">{error}</Alert>}
        <div className="space-y-4">
          <div>
            <Label htmlFor="token-name">Name</Label>
            <Input id="token-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="What will use it, e.g. CI pipeline" autoFocus required maxLength={100} />
          </div>
          <div>
            <Label htmlFor="token-expiry">Expires</Label>
            <Select id="token-expiry" value={expiry} onChange={(e) => setExpiry(e.target.value)}>
              {EXPIRY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </Select>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-6">
          <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" size="sm" disabled={!name.trim() || create.isPending}>{create.isPending ? 'Creating…' : 'Create token'}</Button>
        </div>
      </form>
    </Modal>
  )
}

function ShowTokenDialog({ result, onClose }: { result: ApiTokenCreated; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try { await navigator.clipboard.writeText(result.token) } catch { /* shown below regardless */ }
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <Modal onClose={onClose}>
      <div className="p-6">
        <ModalHeader title={`Token "${result.api_token.name}"`} onClose={onClose} />
        <Alert variant="warning" className="text-sm mb-4">Copy it now. The token is shown once and cannot be fetched again; only a hash is kept.</Alert>
        <div className="flex items-stretch gap-2">
          <code className="flex-1 min-w-0 font-mono text-xs bg-surface-raised rounded-lg p-3 break-all select-all">{result.token}</code>
          <Button type="button" variant="outline" size="sm" onClick={copy} className="flex-none">
            {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />} {copied ? 'Copied' : 'Copy'}
          </Button>
        </div>
        <div className="flex justify-end mt-6">
          <Button type="button" size="sm" onClick={onClose}>Done</Button>
        </div>
      </div>
    </Modal>
  )
}

export function LinkedSignInsPanel({ userId, compact = false }: { userId?: string; compact?: boolean }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const queryKey = ['auth-identities', userId ?? 'me']
  const { data: identities = [], isLoading } = useQuery({ queryKey, queryFn: () => fetchAuthIdentities(userId) })
  const [pendingUnlink, setPendingUnlink] = useState<AuthIdentity | null>(null)

  const unlink = useMutation({
    mutationFn: (id: string) => deleteAuthIdentity(id, userId),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey }); setPendingUnlink(null); toast.show('Sign-in unlinked') },
    onError: (e: Error) => { setPendingUnlink(null); toast.show(e.message || 'Failed to unlink', 'error') },
  })

  return (
    <div>
      <div className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle mb-2">Linked sign-ins</div>
      {!compact && (
        <p className="text-xs text-fg-muted mb-3">
          Provider accounts this user has signed in with. A provider links itself the first time its verified email matches the user's email; nothing to set up here.
        </p>
      )}
      {isLoading ? (
        <div className="text-xs text-fg-muted">Loading…</div>
      ) : identities.length === 0 ? (
        <div className="text-xs text-fg-muted">None yet.</div>
      ) : (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {identities.map((i) => (
            <li key={i.id} className="flex items-center gap-3 px-3 py-2">
              <ProviderMark kind={i.provider} className="w-4 h-4 text-fg-subtle flex-none" />
              <div className="flex-1 min-w-0">
                <div className="text-sm"><span className="font-medium">{i.label}</span> <span className="text-fg-muted">{i.email}</span></div>
                <div className="text-[11px] text-fg-muted">
                  Linked {formatDateTime(i.created_at)}{i.last_login_at ? ` · last sign-in ${relativeTime(i.last_login_at)}` : ''}
                </div>
              </div>
              <Button type="button" size="sm" variant="danger-ghost" onClick={() => setPendingUnlink(i)} title="Unlink">
                <Unlink className="w-3.5 h-3.5" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      {pendingUnlink && (
        <ConfirmDialog
          title={`Unlink ${pendingUnlink.label}?`}
          message="Signing in with this provider again relinks it as long as the verified email still matches. Unlinking does not end sessions already open."
          confirmLabel="Unlink"
          onCancel={() => setPendingUnlink(null)}
          onConfirm={() => unlink.mutate(pendingUnlink.id)}
          isLoading={unlink.isPending}
        />
      )}
    </div>
  )
}
