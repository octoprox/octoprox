// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { AlertTriangle } from 'lucide-react'
import { Link, useNavigate } from 'react-router'
import { useNavigationGuardContext } from '../../contexts/NavigationGuardContext'
import { plural } from '../../utils/format'

/**
 * A project's unmuted problems beside its name in the project list and switcher, nothing when there are none. It links
 * to the project's Overview with the Needs attention panel open.
 */
export function ProblemCount({ count, projectId, role, onFollow }: {
  count: number | undefined
  projectId: string
  /** In a menu, an item of its own. */
  role?: 'menuitem'
  /** Runs when the link is followed with a plain click (the switcher closes its menu). */
  onFollow?: () => void
}) {
  const navigate = useNavigate()
  const guard = useNavigationGuardContext()
  if (!count) return null
  const to = `/projects/${projectId}/overview?open=attention`
  const label = `${count} ${plural(count, 'problem needs', 'problems need')} attention`
  return (
    <Link
      to={to}
      role={role}
      title={label}
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation()
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
        onFollow?.()
        if (!guard.blocked) return
        e.preventDefault()
        guard.request(() => navigate(to))
      }}
      // A phone's tap area is 44px tall without making the row it sits in any taller.
      className="flex-none inline-flex items-center gap-1 text-xs font-medium text-danger tabular-nums rounded-sm hover:underline pointer-coarse:min-h-11 pointer-coarse:-my-3"
    >
      <AlertTriangle className="w-3.5 h-3.5" aria-hidden />
      {count}
    </Link>
  )
}
