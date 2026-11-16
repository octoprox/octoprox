// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { Link } from 'react-router'
import { BellOff, CheckCircle2, ChevronRight, CircleOff } from 'lucide-react'
import type { AttentionLine, MuteDuration } from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { useAttentionMutes, useProjectAttention } from '../../hooks/useProjectAttention'
import { cn } from '../../utils/cn'
import { plural } from '../../utils/format'
import { Button, Card, Legend, LogoLoader } from '../ui'
import { MuteMenu } from './MuteMenu'
import { SeverityMark } from './SeverityMark'
import { lineSentence, whenText } from './lineText'

/** The rows the card holds; the rest are a Show all away, in the side panel. */
const ROWS = 2

/**
 * The top of a project's Overview: what needs attention now, two rows at most. Its height never changes on its own
 * (only with the page's width and pointer), so a refresh, a fix or a failed check never moves the page under the reader.
 */
export function AttentionCard({ projectId, onShowAll }: { projectId: string; onShowAll: () => void }) {
  const { data, isPending, isError, isFetching, refetch } = useProjectAttention(projectId)
  const { canMutate } = useAuth()
  const { mute, pending } = useAttentionMutes(projectId)
  // In the server's order, so a refresh changes text in place and never reorders rows.
  const shown = data?.lines.filter((line) => !line.mute?.active) ?? []
  const problems = data?.problems ?? 0
  const warnings = data?.warnings ?? 0
  const muted = data?.muted ?? 0
  const unmuted = problems + warnings
  const more = unmuted > ROWS || muted > 0

  return (
    <Card className="overflow-hidden">
      <section aria-label="Needs attention">
        {/* A narrow card has no room for the words: the counts alone, as one button that opens the full list. */}
        <div className="h-10 pointer-coarse:h-11 px-4 flex items-center gap-2 @md:gap-3 border-b border-line whitespace-nowrap overflow-hidden">
          <h2 className="text-sm font-semibold text-fg">Needs attention</h2>
          {data && (
            <>
              <div className="hidden @md:flex flex-1 min-w-0 items-center gap-3 text-[12.5px] text-fg-muted tabular-nums">
                {problems > 0 && <Legend dot="bg-danger" n={problems} label={<span className="sr-only @lg:not-sr-only">{plural(problems, 'problem', 'problems')}</span>} />}
                {warnings > 0 && <Legend dot="bg-warning" n={warnings} label={<span className="sr-only @lg:not-sr-only">{plural(warnings, 'warning', 'warnings')}</span>} />}
                <span className="flex-1" />
                {muted > 0 && (
                  <button type="button" onClick={onShowAll} className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-fg pointer-coarse:min-h-11">
                    <BellOff className="w-3.5 h-3.5" /> {muted} muted
                  </button>
                )}
                {unmuted > ROWS && (
                  <button type="button" onClick={onShowAll} className="text-xs font-medium text-primary hover:brightness-110 pointer-coarse:min-h-11">Show all {unmuted}</button>
                )}
              </div>
              <NarrowCounts problems={problems} warnings={warnings} muted={muted} onShowAll={more ? onShowAll : undefined} />
            </>
          )}
        </div>

        <div className="h-28 @xl:h-[68px] @xl:pointer-coarse:h-[88px]">
          {isPending ? (
            <State>
              <LogoLoader size="sm" label="Checking for problems" />
              <span aria-hidden>Checking for problems…</span>
            </State>
          ) : isError && !data ? (
            <State>
              <CircleOff className="w-4 h-4 flex-none text-fg-subtle" />
              <span className="flex-1 min-w-0">Could not check for problems right now.</span>
              <Button variant="ghost" size="sm" onClick={() => refetch()} disabled={isFetching} className="flex-none -mr-2 pointer-coarse:h-11">Try again</Button>
            </State>
          ) : shown.length === 0 ? (
            <State>
              <CheckCircle2 className="w-4 h-4 flex-none text-success" />
              Nothing needs attention right now.
            </State>
          ) : (
            <ul className="divide-y divide-line">
              {shown.slice(0, ROWS).map((line) => (
                <AttentionRow key={line.key} projectId={projectId} line={line} onMute={canMutate ? (duration) => mute(line.key, duration) : undefined} muting={pending} />
              ))}
            </ul>
          )}
        </div>
      </section>
    </Card>
  )
}

/** The counts on a narrow card: a dot and a number each, or the muted count when nothing else is left. */
function NarrowCounts({ problems, warnings, muted, onShowAll }: { problems: number; warnings: number; muted: number; onShowAll?: () => void }) {
  if (problems === 0 && warnings === 0 && muted === 0) return null
  const counts = (
    <>
      {problems > 0 && <Legend dot="bg-danger" n={problems} label={<span className="sr-only">{plural(problems, 'problem', 'problems')}</span>} />}
      {warnings > 0 && <Legend dot="bg-warning" n={warnings} label={<span className="sr-only">{plural(warnings, 'warning', 'warnings')}</span>} />}
      {problems === 0 && warnings === 0
        ? <span className="inline-flex items-center gap-1"><BellOff className="w-3.5 h-3.5" /> {muted}<span className="sr-only"> muted</span></span>
        : muted > 0 && <span className="sr-only">{muted} muted</span>}
    </>
  )
  const classes = 'ml-auto @md:hidden flex items-center gap-2 text-[12.5px] text-fg-muted tabular-nums'
  if (!onShowAll) return <div className={classes}>{counts}</div>
  return (
    <button type="button" onClick={onShowAll} className={`${classes} -mr-1 px-1 hover:text-fg pointer-coarse:min-h-11`}>
      <span className="sr-only">Show all: </span>
      {counts}
      <ChevronRight className="w-3.5 h-3.5 text-fg-subtle" />
    </button>
  )
}

/** The non-data states, inside the same box as the rows. */
function State({ children }: { children: ReactNode }) {
  return <div className="h-full flex items-center gap-2.5 px-4 text-[12.5px] text-fg-muted">{children}</div>
}

/** One line: one row on a wide card, at most two lines of text on a narrow one, at a fixed height either way. */
function AttentionRow({ projectId, line, onMute, muting }: { projectId: string; line: AttentionLine; onMute?: (duration: MuteDuration) => void; muting: boolean }) {
  const sentence = lineSentence(line)
  const title = [line.when ? `${sentence} (${whenText(line.when)})` : sentence, line.detail].filter(Boolean).join('\n')
  return (
    <li className="h-14 @xl:h-[34px] @xl:pointer-coarse:h-11 flex items-center">
      <Link to={`/projects/${projectId}/${line.link}`} title={title} className={cn('flex-1 min-w-0 h-full flex items-center gap-2 @xl:gap-2.5 pl-4 text-[12.5px] text-fg hover:bg-surface-raised transition-colors', onMute ? 'pr-2' : 'pr-4')}>
        <SeverityMark severity={line.severity} />
        <span className="flex-1 min-w-0 line-clamp-2 @xl:line-clamp-1 leading-[18px]">
          {line.lead}<span className="font-medium">{line.subject}</span>{line.text}
        </span>
        {line.when && <span className="hidden @xl:inline flex-none text-fg-muted whitespace-nowrap">{whenText(line.when)}</span>}
        <span className="hidden @xl:inline flex-none font-medium text-primary whitespace-nowrap">{line.action}</span>
        <ChevronRight className="@xl:hidden w-4 h-4 flex-none text-fg-subtle" aria-hidden />
        <span className="sr-only @xl:hidden">{line.action}</span>
      </Link>
      {onMute && <MuteMenu sentence={sentence} onMute={onMute} disabled={muting} className="mr-1 @xl:mr-2" />}
    </li>
  )
}
