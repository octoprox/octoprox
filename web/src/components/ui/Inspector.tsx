// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { ArrowLeft, ChevronRight, X } from 'lucide-react'
import { cn } from '../../utils/cn'

/**
 * Docked side panel that replaces modals. Rendered by a page inside its
 * `panel` slot so it sits beside the content rather than on top of it.
 *
 * Nested steps (e.g. connector → new credential) swap the panel content and
 * pass `crumb` + `onBack` so the header shows where the user came from.
 */
interface InspectorProps {
  title: ReactNode
  subtitle?: ReactNode
  crumb?: ReactNode
  onBack?: () => void
  onClose: () => void
  footer?: ReactNode
  /** Panel width in px. Default 440. */
  width?: number
  children: ReactNode
  className?: string
}

export function Inspector({ title, subtitle, crumb, onBack, onClose, footer, width = 440, children, className }: InspectorProps) {
  return (
    <aside
      style={{ width }}
      className={cn('flex-none h-full bg-surface border-l border-line flex flex-col min-h-0 animate-panel-in', className)}
      aria-label={typeof title === 'string' ? title : undefined}
    >
      <div className="px-4 py-3 border-b border-line flex items-start gap-2 flex-none">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="p-1.5 -ml-1 rounded-md text-fg-muted hover:text-fg hover:bg-surface-raised transition-colors"
            title="Back"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
        )}
        <div className="flex-1 min-w-0">
          {crumb && (
            <div className="flex items-center gap-1 text-[11.5px] text-fg-subtle mb-0.5">
              <span className="truncate">{crumb}</span>
              <ChevronRight className="w-3 h-3 flex-none" />
              <span className="text-fg-muted truncate">{title}</span>
            </div>
          )}
          <div className="text-base font-semibold leading-6 truncate text-fg">{title}</div>
          {subtitle && <div className="text-xs text-fg-muted mt-0.5 truncate">{subtitle}</div>}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="p-1.5 -mr-1 rounded-md text-fg-muted hover:text-fg hover:bg-surface-raised transition-colors"
          title="Close"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto px-4 py-4 space-y-4">{children}</div>
      {footer && (
        <div className="px-4 py-3 border-t border-line flex items-center gap-2 flex-none bg-surface">{footer}</div>
      )}
    </aside>
  )
}

/** Small uppercase section label with optional right-side action. */
export function InspectorSection({ title, action, children, className }: { title: ReactNode; action?: ReactNode; children?: ReactNode; className?: string }) {
  return (
    <section className={cn('space-y-2', className)}>
      <div className="flex items-center justify-between">
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-fg-subtle">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  )
}

/** Compact key/value row used in panels and cards. */
export function KeyValue({ label, value, mono }: { label: ReactNode; value: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5 border-b border-line last:border-b-0 text-[13px]">
      <span className="text-fg-muted flex-none">{label}</span>
      <span className={cn('font-medium text-fg text-right truncate tabular-nums', mono && 'font-mono text-xs')}>{value}</span>
    </div>
  )
}

/** Three-up stat grid for panels. */
export function StatGrid({ items }: { items: { label: string; value: ReactNode; className?: string }[] }) {
  return (
    <div className="grid grid-cols-3 gap-3">
      {items.map((it) => (
        <div key={it.label} className="min-w-0">
          <div className="text-[11px] text-fg-muted">{it.label}</div>
          <div className={cn('text-[15px] font-semibold tabular-nums truncate', it.className)}>{it.value}</div>
        </div>
      ))}
    </div>
  )
}
