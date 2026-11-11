// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, type ReactNode } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { ChevronRight, History } from 'lucide-react'
import { fetchEntityHistory, type AuditEntityType, type AuditEntry, type AuditValue } from '../../api/client'
import { actionLabel, actionTone, fieldLabel, formatAuditValue, humanize, isMasked } from '../../utils/audit'
import { formatDateTime, relativeTime } from '../../utils/format'
import { EmptyState } from '../layout/Page'
import { Alert, Badge, Button, LogoLoader, QueryContent } from '../ui'

const PAGE_SIZE = 50

// Longer values show their start, and the rest on request.
const FOLD_AT = 200

// The personal access token the actor acted through, by name and ID, is about the actor rather than the action:
// its name shows beside the actor instead of as a row.
const ACTOR_DETAILS = new Set(['auth_token', 'auth_token_id'])

/**
 * An entity's History, newest first, a page at a time. Inspectors mount it only while its tab is chosen,
 * so it loads afresh each time it is opened, a save just made included.
 */
export function EntityHistory({ entityType, entityId }: { entityType: AuditEntityType; entityId: string }) {
  const query = useInfiniteQuery({
    queryKey: ['audit', entityType, entityId],
    queryFn: ({ pageParam }) => fetchEntityHistory(entityType, entityId, pageParam, PAGE_SIZE),
    initialPageParam: null as string | null,
    // The next page starts below the oldest entry shown, so entries made meanwhile never shift it.
    getNextPageParam: (last) => (last.more && last.entries.length > 0 ? last.entries[last.entries.length - 1].id : null),
  })
  return (
    <QueryContent query={query} what="the history" loader={<LogoLoader className="py-6" />}>
      {(data) => {
        const entries = data.pages.flatMap((page) => page.entries)
        if (entries.length === 0) {
          return <EmptyState icon={<History />} title="No changes recorded yet" description="Each change shows here with who made it, when, and the fields it changed." />
        }
        return (
          <div className="space-y-3">
            <ol className="divide-y divide-line">
              {entries.map((entry) => (
                <li key={entry.id} className="py-3 first:pt-0">
                  <AuditEntryView entry={entry} />
                </li>
              ))}
            </ol>
            {/* The entries shown stay; the button below asks again. */}
            {query.isFetchNextPageError && (
              <Alert variant="error" role="alert" className="wrap-break-word">
                Could not load the older entries{query.error?.message ? `: ${query.error.message}` : ''}.
              </Alert>
            )}
            {query.hasNextPage && (
              <Button type="button" variant="outline" size="sm" className="w-full pointer-coarse:h-11" onClick={() => query.fetchNextPage()} disabled={query.isFetchingNextPage}>
                {query.isFetchingNextPage ? 'Loading…' : query.isFetchNextPageError ? 'Try again' : 'Load older'}
              </Button>
            )}
          </div>
        )
      }}
    </QueryContent>
  )
}

/** One entry: what was done, by whom and when, then the fields it changed and what else it records. */
export function AuditEntryView({ entry }: { entry: AuditEntry }) {
  const changes = Object.entries(entry.changes)
  // Empty context is not worth a row: a password sign-in names no provider.
  const details = Object.entries(entry.details).filter(([key, value]) => value !== null && value !== '' && !ACTOR_DETAILS.has(key))
  const token = entry.details.auth_token
  return (
    <article className="@container space-y-2">
      <header className="flex items-center gap-x-2 gap-y-1 flex-wrap text-[13px]">
        <Badge color={actionTone(entry.action)}>{actionLabel(entry.action)}</Badge>
        {entry.actor && (
          <span className="min-w-0 wrap-break-word text-fg-muted">
            by <span className="font-medium text-fg">{entry.actor}</span>
            {typeof token === 'string' && token && <> with token <span className="font-medium text-fg">{token}</span></>}
          </span>
        )}
        <time dateTime={entry.created_at} title={formatDateTime(entry.created_at)} className="ml-auto text-xs text-fg-subtle whitespace-nowrap">
          {/* A touch screen shows no hover title, so it shows the exact time itself. */}
          <span className="pointer-coarse:hidden">{relativeTime(entry.created_at)}</span>
          <span className="hidden pointer-coarse:inline">{formatDateTime(entry.created_at)}</span>
        </time>
      </header>
      {changes.length > 0 && (
        <div className="rounded-lg border border-line overflow-hidden">
          {changes.map(([field, [before, after]]) => {
            const { group, name } = fieldLabel(field)
            return (
              <Row key={field} label={<>{group && <span className="font-normal text-fg-subtle">{group}<ChevronRight className="inline w-3 h-3 mx-0.5 align-[-2px]" /></span>}{name}</>}>
                <span className="font-mono wrap-break-word"><Change action={entry.action} before={before} after={after} /></span>
              </Row>
            )
          })}
        </div>
      )}
      {details.length > 0 && (
        <div className="rounded-lg border border-line overflow-hidden">
          {details.map(([key, value]) => (
            <Row key={key} label={humanize(key)}>
              <span className="wrap-break-word"><Value value={value} /></span>
            </Row>
          ))}
        </div>
      )}
    </article>
  )
}

/** A label and its value side by side, or the value under its label where the entry is narrow (a phone). */
function Row({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="grid grid-cols-1 @md:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)] gap-x-2.5 gap-y-0.5 px-2.5 py-1.5 border-b border-line last:border-b-0 text-[11.5px]">
      <span className="font-semibold text-fg-muted wrap-break-word">{label}</span>
      <span className="min-w-0 text-fg">{children}</span>
    </div>
  )
}

/**
 * A field's change. A created entity shows what it was made with and a deleted one what it last held,
 * so their one side reads alone. A secret shows only whether it was set: set, changed or not set.
 */
function Change({ action, before, after }: { action: string; before: AuditValue; after: AuditValue }) {
  if (isMasked(before) && isMasked(after)) return <Word>changed</Word>
  if (action === 'deleted') return <Value value={before} />
  if (before === null && (action === 'created' || action === 'imported' || action === 'invited')) return <Value value={after} />
  return <><Value value={before} /><span className="text-fg-subtle"> → </span><Value value={after} /></>
}

function Value({ value }: { value: AuditValue }) {
  if (value === null) return <Word>not set</Word>
  if (isMasked(value)) return <Word>set</Word>
  const text = formatAuditValue(value)
  if (text === '') return <Word>empty</Word>
  return text.length > FOLD_AT ? <Folded text={text} /> : <>{text}</>
}

/** Wording about a value rather than the value itself, so it never reads as one. */
function Word({ children }: { children: ReactNode }) {
  return <span className="font-sans italic text-fg-subtle">{children}</span>
}

function Folded({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      {open ? text : `${text.slice(0, FOLD_AT)}…`}
      <button type="button" onClick={() => setOpen(!open)} className="block mt-0.5 font-sans font-medium text-primary hover:underline pointer-coarse:min-h-11">
        {open ? 'Show less' : `Show all ${text.length.toLocaleString()} characters`}
      </button>
    </>
  )
}
