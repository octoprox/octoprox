// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * What's New: the release notes of the running version and those before it,
 * the ones the user had not opened marked New, and for admins the notes of a
 * newer release the daily update check found. Opened from the Help menu.
 */

import { useEffect, type ReactNode } from 'react'
import { ArrowUpCircle, ExternalLink, X } from 'lucide-react'
import type { ReleaseInfo, ReleaseNote } from '../api/client'
import { Badge, Modal } from './ui'
import { DOCS_URL } from '../utils/docs'
import { relativeTime } from '../utils/format'

const dateFormat = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' })

/** The text of a change with its `code` spans set as code; nothing else in it is markup. */
function ChangeText({ text }: { text: string }) {
  return (
    <>
      {text.split('`').map((part, i) =>
        i % 2 === 1
          ? <code key={i} className="px-1 py-px rounded bg-surface-raised text-[12px] font-mono">{part}</code>
          : <span key={i}>{part}</span>,
      )}
    </>
  )
}

function DocsLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline whitespace-nowrap">
      {children}
      <ExternalLink className="w-3 h-3" />
    </a>
  )
}

function NoteCard({ note, isNew }: { note: ReleaseNote; isNew: boolean }) {
  const span = note.from ? `${note.from} to ${note.version}` : note.version
  return (
    <section className="py-4 border-b border-line last:border-b-0">
      <div className="flex items-baseline gap-2 flex-wrap">
        <h3 className="text-[15px] font-semibold text-fg">{note.title}</h3>
        {isNew && <Badge color="blue" className="py-0.5">New</Badge>}
      </div>
      <div className="mt-0.5 text-[12px] text-fg-subtle">
        {span} · {dateFormat.format(new Date(note.date))}
      </div>
      <ul className="mt-2.5 space-y-2 text-[13.5px] text-fg-muted list-disc pl-5 marker:text-fg-subtle">
        {note.changes.map((change, i) => (
          <li key={i}>
            <ChangeText text={change.text} />
            {change.docs && (
              <>
                {' '}
                <DocsLink href={`${DOCS_URL}/${change.docs}`}>Read more</DocsLink>
              </>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

/**
 * Shows `info` as it was when opened: the parent marks the notes seen as it
 * opens, and the New marks stay for as long as the dialog is open.
 */
export function WhatsNewDialog({ info, onClose }: { info: ReleaseInfo; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // The notes come newest first, so the unseen ones are the first `unseen`.
  const isNew = (index: number) => index < info.unseen
  const update = info.update

  return (
    <Modal onClose={onClose} className="max-w-2xl max-h-[min(90dvh,860px)] flex flex-col" overlayClassName="p-3 sm:p-4">
      <div role="dialog" aria-modal="true" aria-labelledby="whats-new-title" className="flex flex-col min-h-0">
        <header className="flex items-start justify-between gap-3 px-5 pt-4 pb-3 border-b border-line flex-none">
          <div className="min-w-0">
            <h2 id="whats-new-title" className="text-lg font-semibold">What's New</h2>
            <p className="text-[12.5px] text-fg-subtle">You are running Octoprox {info.current_version}</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="p-2 -mr-2 rounded-lg text-fg-muted hover:text-fg hover:bg-surface-raised">
            <X className="w-5 h-5" />
          </button>
        </header>

        <div className="overflow-y-auto overscroll-contain px-5 min-h-0">
          {update && (
            <section className="mt-4 rounded-lg border border-primary/30 bg-primary-soft/40 px-4 py-3">
              <div className="flex items-center gap-2 text-[14px] font-semibold text-fg">
                <ArrowUpCircle className="w-4 h-4 text-primary flex-none" />
                Octoprox {update.version} is available
              </div>
              <p className="mt-1 text-[12.5px] text-fg-muted">
                Last checked {relativeTime(update.checked_at)}. Only admins see this.{' '}
                <DocsLink href={`${DOCS_URL}/deployment#restarts-and-upgrades`}>How to upgrade</DocsLink>
              </p>
              <div className="mt-1">
                {update.notes.map((note) => <NoteCard key={note.version} note={note} isNew={false} />)}
              </div>
            </section>
          )}
          {info.notes.length === 0
            ? <p className="py-6 text-[13.5px] text-fg-muted">This build has no release notes.</p>
            : info.notes.map((note, i) => <NoteCard key={note.version} note={note} isNew={isNew(i)} />)}
        </div>

        <footer className="flex-none px-5 py-3 border-t border-line text-[13px]">
          <DocsLink href={`${DOCS_URL}/changelog`}>Full changelog on octoprox.com</DocsLink>
        </footer>
      </div>
    </Modal>
  )
}
