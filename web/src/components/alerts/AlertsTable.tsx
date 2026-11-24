// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { ColumnDef } from '@tanstack/react-table'
import { ArrowUp, Bell, BellOff, X } from 'lucide-react'
import { ALERT_RANGES, type AlertEntry, type AlertStatus, type AttentionSeverity } from '../../api/client'
import { useTimeRange } from '../../contexts/TimeRangeContext'
import type { AlertList, AlertListFilters } from '../../hooks/useAlerts'
import { useWidth } from '../../hooks/useWidth'
import { cn } from '../../utils/cn'
import { formatClock, formatDateTime, formatDuration, parseApiDate } from '../../utils/format'
import { installText } from '../attention/lineText'
import { SeverityMark } from '../attention/SeverityMark'
import { DataTable, type DataTableFeatures } from '../DataTable'
import { EmptyState } from '../layout/Page'
import { Alert, Badge, Button, Input, LogoLoader, Segmented, Select } from '../ui'
import { LoadError, loadFailed } from '../ui/LoadError'
import { kindLabel } from './kinds'

// Filters and their buttons: 16 px text and 44 px targets on touch screens, compact under a mouse.
const FILTER_CONTROL = 'py-1.5 text-base pointer-fine:text-[13px] pointer-coarse:h-11'

// Narrower than this, or beside an open alert, the table keeps to the alert and says the rest under its sentence.
const WIDE_MIN_PX = 640

/** Where an alert stands: open on the card, muted, covered by a wider line (open, off the card), or resolved. */
export type AlertState = 'open' | 'muted' | 'covered' | 'resolved'

const STATE_LABELS: Record<AlertState, string> = { open: 'Open', muted: 'Muted', covered: 'Covered', resolved: 'Resolved' }

export function stateOf(alert: AlertEntry): AlertState {
  if (alert.resolved_at) return 'resolved'
  if (alert.mute?.active) return 'muted'
  return alert.hidden_by ? 'covered' : 'open'
}

/** The severity an alert is marked with: its severity now while open, the worst it reached once resolved. */
export const markOf = (alert: AlertEntry): AttentionSeverity => (alert.resolved_at ? alert.worst_severity : alert.severity)

/** An alert's text after its subject; only an install alert's holds a `{time}`. */
export const alertText = (alert: AlertEntry): string => installText(alert.text, alert.when?.at)

/** How long an alert has held: from its first opening to its resolving, or to now while open. */
function lastedSeconds(alert: AlertEntry): number {
  const end = parseApiDate(alert.resolved_at)?.getTime() ?? Date.now()
  return (end - (parseApiDate(alert.opened_at)?.getTime() ?? end)) / 1000
}

/** An alert's status as a badge: Open in its severity's colour, the others gray. */
export function StatusBadge({ alert }: { alert: AlertEntry }) {
  const state = stateOf(alert)
  if (state === 'open') return <Badge color={alert.severity === 'problem' ? 'red' : 'yellow'} className="py-0.5">Open</Badge>
  return (
    <Badge color="gray" className="py-0.5 inline-flex items-center gap-1" title={state === 'covered' ? 'Open, and covered on the card by a wider line' : undefined}>
      {state === 'muted' && <BellOff className="w-3 h-3" aria-hidden />}
      {STATE_LABELS[state]}
    </Badge>
  )
}

/**
 * The alerts of a project or of the install, newest first, under filters that apply as they are set (the subject on
 * Enter or Apply). The rows stay in place while they refresh; what opened since is announced above them.
 */
export function AlertsTable({ list, filters, onFiltersChange, kinds, search = false, activeId, onOpen, emptyDescription }: {
  list: AlertList
  filters: AlertListFilters
  onFiltersChange: (filters: AlertListFilters) => void
  /** The kinds the kind filter offers. */
  kinds: readonly string[]
  /** The subject search: project alerts have subjects, the install's do not. */
  search?: boolean
  /** The alert open in the side panel, if any: the table then keeps to the alert itself. */
  activeId: string | null
  onOpen: (alert: AlertEntry) => void
  emptyDescription: string
}) {
  const [range] = useTimeRange(ALERT_RANGES)
  const [ref, width] = useWidth()
  const { query, alerts, first, newer, showNewest } = list
  const roomy = width >= WIDE_MIN_PX && activeId === null

  const columns: ColumnDef<DataTableFeatures, AlertEntry>[] = useMemo(() => [
    {
      id: 'mark', header: '', size: 40, enableSorting: false,
      cell: ({ row }) => <SeverityMark severity={markOf(row.original)} />,
    },
    {
      // Unsized: takes the room the others leave.
      id: 'alert', header: 'Alert', enableSorting: false, meta: { minWidth: 150 },
      cell: ({ row }) => <AlertCell alert={row.original} compact={!roomy} />,
    },
    {
      id: 'opened', header: 'Opened', size: 176, enableSorting: false,
      cell: ({ row }) => <span className="text-fg-muted">{formatDateTime(row.original.opened_at)}</span>,
    },
    {
      id: 'lasted', header: 'Lasted', size: 80, enableSorting: false,
      cell: ({ row }) => <span className="tabular-nums">{formatDuration(lastedSeconds(row.original))}</span>,
    },
    {
      id: 'reopened', header: 'Reopened', size: 88, enableSorting: false, meta: { align: 'right' as const },
      cell: ({ row }) => (row.original.reopened > 0
        ? <span title={`Came back ${row.original.reopened} times within 15 minutes of clearing`}>×{row.original.reopened}</span>
        : null),
    },
    {
      id: 'status', header: 'Status', size: 100, enableSorting: false,
      cell: ({ row }) => <StatusBadge alert={row.original} />,
    },
  ], [roomy])

  const change = (patch: AlertListFilters) => onFiltersChange({ ...filters, ...patch })
  const status = filters.status ?? 'all'
  const statusOptions: { value: AlertStatus; label: string }[] = [
    { value: 'open', label: first ? `Open ${first.open.toLocaleString()}` : 'Open' },
    { value: 'resolved', label: first ? `Resolved ${first.resolved.toLocaleString()}` : 'Resolved' },
    { value: 'all', label: 'All' },
  ]
  const filtering = status !== 'all' || !!filters.severity || !!filters.kind || !!filters.q

  return (
    <div ref={ref} className="flex flex-col gap-3 min-w-0">
      <div className="flex flex-col @lg:flex-row @lg:flex-wrap @lg:items-center gap-2">
        {/* Segmented has no touch sizing of its own: 44 px buttons on touch screens, as the other filters. */}
        <div className="pointer-coarse:[&_button]:min-h-11">
          <Segmented options={statusOptions} value={status} onChange={(value) => change({ status: value })} />
        </div>
        <Select
          aria-label="Severity"
          value={filters.severity ?? ''}
          onChange={(e) => change({ severity: (e.target.value || undefined) as AttentionSeverity | undefined })}
          className={cn('@lg:w-40', FILTER_CONTROL)}
        >
          <option value="">Any severity</option>
          <option value="problem">Problems</option>
          <option value="warning">Warnings</option>
        </Select>
        <Select
          aria-label="Kind"
          value={filters.kind ?? ''}
          onChange={(e) => change({ kind: e.target.value || undefined })}
          className={cn('@lg:w-56', FILTER_CONTROL)}
        >
          <option value="">Any kind</option>
          {kinds.map((kind) => <option key={kind} value={kind}>{kindLabel(kind)}</option>)}
        </Select>
        {search && <SubjectFilter applied={filters.q} onApply={(q) => change({ q })} />}
      </div>

      {loadFailed(query) ? (
        <LoadError what="the alerts" query={query} />
      ) : query.isPending ? (
        <LogoLoader className="py-10" />
      ) : (
        <section className="flex flex-col gap-2">
          {/* Always this tall, so the button appearing never moves the rows. */}
          <div className="flex items-center justify-between gap-3 h-8 pointer-coarse:h-11">
            <span className="text-xs text-fg-subtle truncate">Newest first, each open at some point in the last {range}</span>
            {newer > 0 && (
              <Button variant="outline" size="sm" onClick={showNewest} className="flex-none pointer-coarse:h-11">
                <ArrowUp className="w-3.5 h-3.5" /> {newer > 999 ? '999+' : newer.toLocaleString()} new, show
              </Button>
            )}
          </div>
          {alerts.length === 0 && !filtering ? (
            <EmptyState icon={<Bell />} title="No alerts in this range." reloading={query.isPlaceholderData} description={emptyDescription} />
          ) : (
            <DataTable
              columns={columns}
              data={alerts}
              defaultPageSize={50}
              getRowId={(alert) => alert.id}
              onRowClick={onOpen}
              activeRowId={activeId}
              columnVisibility={{ opened: roomy, lasted: roomy, reopened: roomy, status: roomy }}
              loading={query.isLoading}
              reloading={query.isPlaceholderData}
              emptyMessage="No alerts match these filters."
            />
          )}
          {/* The rows shown stay; the button below asks again. */}
          {query.isFetchNextPageError && (
            <Alert variant="error" role="alert" className="wrap-break-word">
              Could not load the older alerts{query.error?.message ? `: ${query.error.message}` : ''}.
            </Alert>
          )}
          {query.hasNextPage && (
            <div className="flex justify-end">
              <Button variant="outline" size="sm" onClick={() => query.fetchNextPage()} disabled={query.isFetchingNextPage} className="pointer-coarse:h-11">
                {query.isFetchingNextPage ? 'Loading…' : query.isFetchNextPageError ? 'Try again' : 'Load more'}
              </Button>
            </div>
          )}
        </section>
      )}
    </div>
  )
}

/**
 * The alert's sentence, two lines at most. Beside a panel or on a narrow table, where the other columns are hidden,
 * a muted line under it says when it opened, how long it lasted and where it stands.
 */
function AlertCell({ alert, compact }: { alert: AlertEntry; compact: boolean }) {
  const sentence = alert.lead + alert.subject + alertText(alert)
  return (
    <div className="min-w-0 py-1.5 whitespace-normal leading-[18px]" title={[sentence, alert.detail].filter(Boolean).join('\n')}>
      <p className="line-clamp-2 wrap-break-word text-fg">
        {alert.lead}<span className="font-semibold">{alert.subject}</span>{alertText(alert)}
      </p>
      {compact && (
        <p className="text-[11px] text-fg-subtle truncate">
          {formatClock(alert.opened_at)} · {formatDuration(lastedSeconds(alert))} · {STATE_LABELS[stateOf(alert)]}
        </p>
      )}
    </div>
  )
}

/** The subject search, applied on Enter or Apply rather than on every key, and cleared in one tap. */
function SubjectFilter({ applied, onApply }: { applied: string | undefined; onApply: (q: string | undefined) => void }) {
  const [text, setText] = useState(applied ?? '')
  // The box follows a search set from outside it: a link, or the sidebar entry resetting the page. Its own, which the
  // address shows a moment later, leaves what was typed meanwhile.
  const [shownFor, setShownFor] = useState(applied)
  const [sent, setSent] = useState<{ q: string | undefined } | null>(null)
  if (shownFor !== applied) {
    setShownFor(applied)
    setSent(null)
    if (!(sent && sent.q === applied)) setText(applied ?? '')
  }
  const apply = (q: string | undefined) => {
    setSent({ q })
    onApply(q)
  }
  const typed = text.trim() || undefined
  return (
    <form className="flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); apply(typed) }}>
      <Input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Subject"
        aria-label="Subject"
        maxLength={100}
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        className={cn('flex-1 min-w-0 @lg:w-44 @lg:flex-none px-3', FILTER_CONTROL)}
      />
      <Button type="submit" size="sm" variant={typed !== applied ? 'primary' : 'secondary'} disabled={typed === applied} className="pointer-coarse:h-11">
        Apply
      </Button>
      {applied && (
        <Button
          type="button"
          size="icon"
          variant="ghost"
          onClick={() => { setText(''); apply(undefined) }}
          aria-label="Clear the subject search"
          title="Clear the subject search"
          className="flex-none pointer-coarse:w-11 pointer-coarse:h-11"
        >
          <X className="w-3.5 h-3.5" />
        </Button>
      )}
    </form>
  )
}
