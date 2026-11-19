// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useContext } from 'react'
import { Link } from 'react-router'
import type { AttentionLine, AttentionMute, MuteDuration, ProjectAttention } from '../../api/client'
import { useAttentionMutes } from '../../hooks/useProjectAttention'
import { cn } from '../../utils/cn'
import { formatClock, formatDateTime, plural } from '../../utils/format'
import { ProjectSettingsContext } from '../layout/Page'
import { Button, Inspector, InspectorSection, LogoLoader } from '../ui'
import { MuteMenu } from './MuteMenu'
import { SeverityMark } from './SeverityMark'
import { lineSentence, sinceText, whenText } from './lineText'

const HEADER_LINK = 'inline-flex items-center font-medium text-primary hover:brightness-110 pointer-coarse:min-h-11'

/**
 * Every line of the Needs attention card in the page's side panel: problems, warnings, and what is muted and by whom,
 * with the way to the project's alert history and its alert settings.
 */
export function AttentionPanel({ projectId, projectName, attention, canMutate, onClose }: {
  projectId: string
  projectName: string
  attention: ProjectAttention | undefined
  canMutate: boolean
  onClose: () => void
}) {
  const { mute, unmute, pending } = useAttentionMutes(projectId)
  // Null where the user cannot change the project's settings.
  const openSettings = useContext(ProjectSettingsContext)
  const lines = attention?.lines ?? []
  const shown = lines.filter((line) => !line.mute?.active)
  const sections = [
    { title: 'Problems', lines: shown.filter((line) => line.severity === 'problem') },
    { title: 'Warnings', lines: shown.filter((line) => line.severity === 'warning') },
    { title: 'Muted', lines: lines.filter((line) => line.mute?.active) },
  ].filter((section) => section.lines.length > 0)
  const checked = attention?.checked_at ? `checked ${formatClock(attention.checked_at)}` : 'now'

  return (
    <Inspector
      title="Needs attention"
      subtitle={`${projectName} · ${checked}`}
      actions={(
        <>
          <Link to={`/projects/${projectId}/alerts`} className={HEADER_LINK}>Alert history</Link>
          {canMutate && openSettings && (
            <button type="button" onClick={() => openSettings('alerts')} className={HEADER_LINK}>Alert settings</button>
          )}
        </>
      )}
      onClose={onClose}
    >
      {!attention || (attention.checked_at === null && lines.length === 0) ? (
        <LogoLoader className="py-6" label="Checking for problems…" />
      ) : sections.length === 0 ? (
        <p className="text-[13px] text-fg-muted">Nothing needs attention right now.</p>
      ) : sections.map((section) => (
        <InspectorSection key={section.title} title={`${section.title} (${section.lines.length})`}>
          <ul className="divide-y divide-line">
            {section.lines.map((line) => (
              <PanelLine
                key={line.key}
                projectId={projectId}
                line={line}
                canMutate={canMutate}
                pending={pending}
                onMute={(duration) => mute(line.key, duration)}
                onUnmute={() => unmute(line.key)}
              />
            ))}
          </ul>
        </InspectorSection>
      ))}
    </Inspector>
  )
}

/** Until when a mute holds, as people read it. */
export const mutedUntil = (mute: AttentionMute): string => (mute.until ? formatDateTime(mute.until) : 'it clears')

function PanelLine({ projectId, line, canMutate, pending, onMute, onUnmute }: {
  projectId: string
  line: AttentionLine
  canMutate: boolean
  pending: boolean
  onMute: (duration: MuteDuration) => void
  onUnmute: () => void
}) {
  const sentence = lineSentence(line)
  const muted = !!line.mute?.active
  // Since when, opening the alert's history, and how often it came back.
  const spell = (
    <span className="text-fg-muted">
      <Link to={`/projects/${projectId}/alerts?open=${encodeURIComponent(line.alert_id)}`} title="Open in the alert history" className="inline-flex items-center hover:text-fg hover:underline pointer-coarse:min-h-11">
        {sinceText(line)}
      </Link>
      {line.reopened > 0 && ` · reopened ${line.reopened} ${plural(line.reopened, 'time', 'times')}`}
    </span>
  )
  return (
    <li className="py-3 first:pt-1 last:pb-0 flex items-start gap-2.5 text-[13px] leading-5">
      <SeverityMark severity={line.severity} className="mt-[3px]" />
      <div className="flex-1 min-w-0 space-y-1">
        <p className={cn('wrap-break-word', muted ? 'text-fg-muted' : 'text-fg')}>
          {line.lead}<span className="font-medium">{line.subject}</span>{line.text}
        </p>
        {line.detail && <p className="text-xs text-fg-muted">{line.detail}</p>}
        {line.mute && !muted && (
          <p className="text-xs text-fg-muted">Muted as a {line.mute.severity} until {mutedUntil(line.mute)} · back because it got worse</p>
        )}
        {muted && line.mute ? (
          <div className="flex items-center gap-x-3 gap-y-1 flex-wrap text-xs text-fg-muted">
            <span className="flex-1 min-w-0">{spell} · Muted by <span className="font-medium text-fg">{line.mute.muted_by}</span> · until {mutedUntil(line.mute)}</span>
            {canMutate && (
              <Button type="button" variant="outline" size="sm" onClick={onUnmute} disabled={pending} aria-label={`Unmute: ${sentence}`} className="flex-none pointer-coarse:h-11">Unmute</Button>
            )}
          </div>
        ) : (
          <div className="flex items-center gap-x-3 gap-y-1 flex-wrap text-xs">
            {spell}
            {line.when && <span className="text-fg-muted">{whenText(line.when)}</span>}
            <Link to={`/projects/${projectId}/${line.link}`} className="inline-flex items-center font-medium text-primary hover:brightness-110 pointer-coarse:min-h-11">{line.action} →</Link>
            {canMutate && <MuteMenu sentence={sentence} onMute={onMute} disabled={pending} className="ml-auto -my-1" />}
          </div>
        )}
      </div>
    </li>
  )
}
