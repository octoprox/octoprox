// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Link, useLocation } from 'react-router'
import type { InstallAttentionLine } from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { useInstallAttention } from '../../hooks/useProjectAttention'
import { formatClock } from '../../utils/format'
import { setSettingsOrigin } from '../../utils/settingsOrigin'
import { Card } from '../ui'
import { SeverityMark } from './SeverityMark'
import { installText } from './lineText'

/** Where an admin sees more about an install-wide line or alert. */
export const ADMIN_PAGES: Record<InstallAttentionLine['admin_page'], { path: string; name: string }> = {
  system: { path: '/admin/system', name: 'System' },
  geo: { path: '/admin/geo', name: 'IP attribution' },
}

/**
 * What needs attention across the install, at the top of the Projects page: the same plain words for everyone, since
 * when each has held, and a link to the page that shows more for admins. Nothing at all while all is well.
 */
export function InstallAttention() {
  const lines = useInstallAttention()
  const { isAdmin } = useAuth()
  const location = useLocation()
  if (lines.length === 0) return null
  const pages = [...new Set(lines.map((line) => ADMIN_PAGES[line.admin_page].name))]
  return (
    <Card className="overflow-hidden">
      <section aria-label="Needs attention, all projects">
        <div className="h-10 px-4 flex items-center gap-2 border-b border-line whitespace-nowrap overflow-hidden">
          <h2 className="text-sm font-semibold text-fg">Needs attention</h2>
          <span className="px-2 py-0.5 rounded-full border border-line bg-surface-raised text-[11px] font-medium text-fg-muted whitespace-nowrap">All projects</span>
        </div>
        <ul className="divide-y divide-line">
          {lines.map((line) => {
            const page = ADMIN_PAGES[line.admin_page]
            return (
              <li key={line.key} className="flex items-start gap-2.5 px-4 py-2 text-[12.5px] leading-[18px]">
                <SeverityMark severity={line.severity} className="mt-0.5" />
                <div className="flex-1 min-w-0 flex flex-col @lg:flex-row gap-x-3 gap-y-0.5">
                  <span className="flex-1 min-w-0 text-fg">{installText(line.text, line.at)}</span>
                  {/* A line that already names a time gets no second one. */}
                  {line.since && !line.text.includes('{time}') && (
                    <span className="flex-none text-fg-muted whitespace-nowrap">since {formatClock(line.since)}</span>
                  )}
                  {isAdmin && (
                    <Link to={page.path} onClick={() => setSettingsOrigin(location.pathname)} className="self-start flex-none inline-flex items-center font-medium text-primary hover:brightness-110 whitespace-nowrap pointer-coarse:min-h-11">
                      Open {page.name}
                    </Link>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
        {!isAdmin && (
          <p className="px-4 py-2 border-t border-line text-xs text-fg-muted">An admin can see more under {pages.join(' and ')}.</p>
        )}
      </section>
    </Card>
  )
}
