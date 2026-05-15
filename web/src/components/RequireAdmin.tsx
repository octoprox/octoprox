// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { ReactNode } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

export default function RequireAdmin({ children }: { children: ReactNode }) {
  const { isAdmin } = useAuth()
  if (!isAdmin) {
    return <Navigate to="/settings/account" replace />
  }
  return <>{children}</>
}
