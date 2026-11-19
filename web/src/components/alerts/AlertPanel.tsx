// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Link } from 'react-router'
import type { AlertEntry, AttentionSeverity, InstallAttentionLine } from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { useAttentionMutes } from '../../hooks/useProjectAttention'
import { formatClock, formatDateTime, plural } from '../../utils/format'
import { mutedUntil } from '../attention/AttentionPanel'
import { ADMIN_PAGES } from '../attention/InstallAttention'
import { whenText } from '../attention/lineText'
import { MuteMenu } from '../attention/MuteMenu'
import { SeverityMark } from '../attention/SeverityMark'
import { Button, Inspector, InspectorSection, KeyValue } from '../ui'
import { alertText, markOf } from './AlertsTable'
import { kindLabel, kindOfKey } from './kinds'

const SEVERITY_LABELS: Record<AttentionSeverity, string> = { problem: 'Problem', warning: 'Warning' }

// A link that reads as the panel's main button: 44 px on touch screens.
const LINK_BUTTON = 'inline-flex items-center justify-center h-8 px-3 rounded-lg text-[13px] font-medium bg-primary text-fg-on-primary hover:bg-primary-hover whitespace-nowrap pointer-coarse:h-11'

/**
 * One alert beside the Alerts table: what it said, when it opened, came back and resolved, its mute (a project's open
 * alert), and the page that fixes it. `projectId` is null for an install alert, which links to its admin page instead.
 */
export function AlertPanel({ alert, projectId, onClose }: { alert: AlertEntry; projectId: string | null; onClose: () => void }) {
  const open = alert.resolved_at === null
  const fix = fixOf(alert, projectId)
  return (
    <Inspector
      title={kindLabel(alert.kind)}
      subtitle={open ? 'Open' : 'Resolved'}
      onClose={onClose}
      footer={fix ? (
        <>
          <span className="flex-1" />
          <Link to={fix.to} className={LINK_BUTTON}>{fix.label}</Link>
        </>
      ) : undefined}
    >
      <div className="flex items-start gap-2.5 text-[13px] leading-5">
        <SeverityMark severity={markOf(alert)} className="mt-[3px]" />
        <div className="flex-1 min-w-0 space-y-1">
          <p className="text-fg wrap-break-word">
            {alert.lead}<span className="font-medium">{alert.subject}</span>{alertText(alert)}
          </p>
          {alert.detail && <p className="text-xs text-fg-muted">{alert.detail}</p>}
          {/* An install alert's own time is in its sentence already. */}
          {alert.when && alert.when.kind !== 'at' && <p className="text-xs text-fg-muted">{whenText(alert.when)}</p>}
          {open && alert.hidden_by && (
            <p className="text-xs text-fg-muted">Covered on the card by: {kindLabel(kindOfKey(alert.hidden_by))}</p>
          )}
        </div>
      </div>

      <InspectorSection title="Timeline">
        <div>
          <KeyValue label="Opened" value={formatDateTime(alert.opened_at)} />
          <KeyValue
            label="Reopened"
            value={alert.reopened > 0 ? `${alert.reopened} ${plural(alert.reopened, 'time', 'times')}, last at ${formatClock(alert.reopened_at)}` : 'Never'}
          />
          <KeyValue label="Last seen" value={formatDateTime(alert.last_seen_at)} />
          <KeyValue label="Resolved" value={alert.resolved_at ? formatDateTime(alert.resolved_at) : 'Still open'} />
          {open && <KeyValue label="Severity now" value={SEVERITY_LABELS[alert.severity]} />}
          <KeyValue label="Worst" value={SEVERITY_LABELS[alert.worst_severity]} />
        </div>
      </InspectorSection>

      {open && projectId !== null && <MuteSection projectId={projectId} alert={alert} />}
    </Inspector>
  )
}

/** The page that fixes an alert: the project page its line links to, or an install alert's admin page. */
function fixOf(alert: AlertEntry, projectId: string | null): { to: string; label: string } | null {
  if (projectId !== null) return alert.action ? { to: `/projects/${projectId}/${alert.link}`, label: alert.action } : null
  const page = ADMIN_PAGES[alert.link as InstallAttentionLine['admin_page']] ?? ADMIN_PAGES.system
  return { to: page.path, label: `Open ${page.name}` }
}

/** An open project alert's mute as the Needs attention panel shows it, set and lifted there by editors. */
function MuteSection({ projectId, alert }: { projectId: string; alert: AlertEntry }) {
  const { canMutate } = useAuth()
  const { mute, unmute, pending } = useAttentionMutes(projectId)
  const sentence = alert.lead + alert.subject + alert.text
  const current = alert.mute
  return (
    <InspectorSection title="Mute">
      <div className="flex items-center gap-x-3 gap-y-1 flex-wrap text-[13px] text-fg-muted">
        <span className="flex-1 min-w-0">
          {!current ? (
            'Not muted.'
          ) : current.active ? (
            <>Muted by <span className="font-medium text-fg">{current.muted_by}</span> until {mutedUntil(current)}</>
          ) : (
            <>Muted as a {current.severity} until {mutedUntil(current)} · back because it got worse</>
          )}
        </span>
        {canMutate && (current?.active ? (
          <Button type="button" variant="outline" size="sm" onClick={() => unmute(alert.key)} disabled={pending} aria-label={`Unmute: ${sentence}`} className="flex-none pointer-coarse:h-11">Unmute</Button>
        ) : (
          <MuteMenu sentence={sentence} onMute={(duration) => mute(alert.key, duration)} disabled={pending} className="-my-1" />
        ))}
      </div>
    </InspectorSection>
  )
}
