// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { updateSelf, UserSelfUpdate } from '../api/client'
import { Button, Input, Label, Alert, Modal, ModalHeader, ModalFooter } from './ui'

export default function ProfileModal({
  username,
  email,
  onClose,
  onSuccess,
}: {
  username: string
  email: string
  onClose: () => void
  onSuccess: () => void
}) {
  const [newEmail, setNewEmail] = useState(email)
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  const mutation = useMutation({
    mutationFn: (data: UserSelfUpdate) => updateSelf(data),
    onSuccess: () => {
      setSuccess(true)
      setError(null)
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      onSuccess()
    },
    onError: (err: any) => {
      setError(err.message || 'Failed to update profile')
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
    if (newEmail !== email) data.email = newEmail
    if (newPassword) {
      data.password = newPassword
      data.current_password = currentPassword
    }

    if (Object.keys(data).length === 0) {
      onClose()
      return
    }

    mutation.mutate(data)
  }

  return (
    <Modal onClose={onClose}>
      <form onSubmit={handleSubmit} className="p-6">
        <ModalHeader title="Profile" onClose={onClose} />

        {error && <Alert variant="error" className="mb-4">{error}</Alert>}
        {success && <Alert variant="success" className="mb-4">Profile updated successfully</Alert>}

        <div className="space-y-4">
          <div>
            <Label htmlFor="profile-username">Username</Label>
            <Input id="profile-username" value={username} disabled />
          </div>
          <div>
            <Label htmlFor="profile-email">Email</Label>
            <Input
              id="profile-email"
              type="email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
            />
          </div>

          <hr className="border-gray-200 dark:border-gray-700" />

          <div>
            <Label htmlFor="current-password">Current Password</Label>
            <Input
              id="current-password"
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              placeholder="Required to change password"
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
            />
          </div>
        </div>

        <ModalFooter>
          <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? 'Saving...' : 'Save'}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  )
}
