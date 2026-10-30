// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { useAuth } from '../contexts/AuthContext'
import { useNavigationGuardContext } from '../contexts/NavigationGuardContext'
import { setSettingsOrigin } from '../utils/settingsOrigin'

/**
 * Names an Administration page as "Administration → {children}": a link for admins, plain text for everyone else.
 * Back on that page returns here. `newTab` keeps a half-edited form open, since leaving the page would close it.
 */
export function AdminLink({ page, children, newTab }: { page: string; children: ReactNode; newTab?: boolean }) {
  const { isAdmin } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const guard = useNavigationGuardContext()
  const to = `/admin/${page}`
  const text = <>Administration → {children}</>
  if (!isAdmin || location.pathname === to) return text
  if (newTab) return <Link to={to} target="_blank" rel="noreferrer" className="underline">{text}</Link>
  return (
    <Link
      to={to}
      className="underline"
      onClick={(e) => {
        setSettingsOrigin(location.pathname)
        if (!guard.blocked) return
        e.preventDefault()
        guard.request(() => navigate(to))
      }}
    >
      {text}
    </Link>
  )
}
