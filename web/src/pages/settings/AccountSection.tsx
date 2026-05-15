// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { fetchCurrentUser, updateSelf, UserSelfUpdate } from '../../api/client'
import { Alert, Button, Card, Input, Label } from '../../components/ui'

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
    if (currentUser) {
      setEmail(currentUser.email || '')
    }
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

    if (newPassword && newPassword !== confirmPassword) {
      setError('Passwords do not match')
      return
    }

    if (newPassword && !currentPassword) {
      setError('Current password is required to set a new password')
      return
    }

    const data: UserSelfUpdate = {}
    if (currentUser && email !== currentUser.email) data.email = email
    if (newPassword) {
      data.password = newPassword
      data.current_password = currentPassword
    }

    if (Object.keys(data).length === 0) {
      return
    }

    mutation.mutate(data)
  }

  if (isLoading || !currentUser) {
    return <div className="text-fg-muted">Loading…</div>
  }

  return (
    <div className="space-y-6">
      <header>
        <h2 className="text-xl font-semibold text-fg">Account</h2>
        <p className="text-sm text-fg-muted mt-1">Manage your email and password.</p>
      </header>

      <Card className="p-6">
        <form onSubmit={handleSubmit} className="space-y-4">
          {error && <Alert variant="error">{error}</Alert>}
          {success && <Alert variant="success">Account updated successfully</Alert>}

          <div>
            <Label htmlFor="profile-username">Username</Label>
            <Input id="profile-username" value={currentUser.username} disabled />
          </div>
          <div>
            <Label htmlFor="profile-email">Email</Label>
            <Input
              id="profile-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
            />
          </div>

          <hr className="border-line" />

          <div>
            <Label htmlFor="current-password">Current Password</Label>
            <Input
              id="current-password"
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              placeholder="Required to change password"
              autoComplete="current-password"
            />
          </div>
          <div>
            <Label htmlFor="new-password">New Password</Label>
            <Input
              id="new-password"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="Leave blank to keep current"
              autoComplete="new-password"
            />
          </div>
          <div>
            <Label htmlFor="confirm-password">Confirm New Password</Label>
            <Input
              id="confirm-password"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="Repeat new password"
              autoComplete="new-password"
            />
          </div>

          <div className="flex justify-end">
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      </Card>
    </div>
  )
}
