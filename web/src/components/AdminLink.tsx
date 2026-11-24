// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { useAuth } from '../contexts/AuthContext'
import { useNavigationGuardContext } from '../contexts/NavigationGuardContext'
import { setSettingsOrigin } from '../utils/settingsOrigin'

/**
 * Names an Administration page as "Administration → {children}": a link for admins, plain text for everyone else.
 * `query` opens a view of it (`tab=policy`). Back on that page returns here. `newTab` keeps a half-edited form open,
 * since leaving the page would close it.
 */
export function AdminLink({ page, query, children, newTab }: { page: string; query?: string; children: ReactNode; newTab?: boolean }) {
  const { isAdmin } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const guard = useNavigationGuardContext()
  const path = `/admin/${page}`
  const to = query ? `${path}?${query}` : path
  const text = <>Administration → {children}</>
  // Already on that page and view: nothing to link to.
  const current = new URLSearchParams(location.search)
  const here = location.pathname === path && [...new URLSearchParams(query)].every(([key, value]) => current.get(key) === value)
  if (!isAdmin || here) return text
  if (newTab) return <Link to={to} target="_blank" rel="noreferrer" className="underline">{text}</Link>
  // Another view of the page it is on: a view change, which replaces the history entry.
  const samePage = location.pathname === path
  return (
    <Link
      to={to}
      replace={samePage}
      className="underline"
      onClick={(e) => {
        setSettingsOrigin(location.pathname + location.search)
        if (!guard.blocked) return
        e.preventDefault()
        guard.request(() => navigate(to, { replace: samePage }))
      }}
    >
      {text}
    </Link>
  )
}
