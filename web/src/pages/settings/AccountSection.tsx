// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { fetchCurrentUser, updateSelf, UserSelfUpdate } from '../../api/client'
import { Page } from '../../components/layout/Page'
import { Alert, Button, Card, Input, Label, Badge } from '../../components/ui'

export default function AccountSection() {
  const { data: currentUser, isLoading, refetch } = useQuery({
    queryKey: ['currentUser'],
    queryFn: fetchCurrentUser,
    refetchInterval: false,
    staleTime: Infinity,
  })

  const [email, setEmail] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  useEffect(() => {
    if (currentUser) setEmail(currentUser.email || '')
  }, [currentUser])

  const mutation = useMutation({
    mutationFn: (data: UserSelfUpdate) => updateSelf(data),
    onSuccess: () => {
      setSuccess(true)
      setError(null)
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      refetch()
    },
    onError: (err: any) => {
      setError(err.message || 'Failed to update account')
      setSuccess(false)
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setSuccess(false)
    if (newPassword && newPassword !== confirmPassword) { setError('Passwords do not match'); return }
    if (newPassword && !currentPassword) { setError('Current password is required to set a new password'); return }
    const data: UserSelfUpdate = {}
    if (currentUser && email !== currentUser.email) data.email = email
    if (newPassword) { data.password = newPassword; data.current_password = currentPassword }
    if (Object.keys(data).length === 0) return
    mutation.mutate(data)
  }

  return (
    <Page title="Account" subtitle="Your sign-in details. Settings here apply to your user, not to a project.">
      {isLoading || !currentUser ? (
        <div className="text-sm text-fg-muted">Loading…</div>
      ) : (
        <Card className="p-5 max-w-xl">
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && <Alert variant="error">{error}</Alert>}
            {success && <Alert variant="success">Account updated</Alert>}

            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label htmlFor="profile-username">Username</Label>
                <Input id="profile-username" value={currentUser.username} disabled />
              </div>
              <div>
                <Label>Role</Label>
                <div className="h-9 flex items-center"><Badge color={currentUser.role === 'admin' ? 'blue' : 'gray'}>{currentUser.role}</Badge></div>
              </div>
            </div>
            <div>
              <Label htmlFor="profile-email">Email</Label>
              <Input id="profile-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
            </div>

            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle pt-2">Change password</h3>
            <div>
              <Label htmlFor="current-password">Current password</Label>
              <Input id="current-password" type="password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} placeholder="Required to change password" autoComplete="current-password" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label htmlFor="new-password">New password</Label>
                <Input id="new-password" type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder="Leave blank to keep" autoComplete="new-password" />
              </div>
              <div>
                <Label htmlFor="confirm-password">Confirm</Label>
                <Input id="confirm-password" type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} placeholder="Repeat new password" autoComplete="new-password" />
              </div>
            </div>

            <div className="flex justify-end">
              <Button type="submit" size="sm" disabled={mutation.isPending}>{mutation.isPending ? 'Saving…' : 'Save changes'}</Button>
            </div>
          </form>
        </Card>
      )}
    </Page>
  )
}
