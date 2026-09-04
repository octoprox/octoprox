// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'
import { cn } from '../../utils/cn'

/**
 * Standard page frame: scrolling content on the left, optional docked
 * Inspector on the right. The AppShell gives it a flex row to fill.
 */
interface PageProps {
  title: ReactNode
  subtitle?: ReactNode
  count?: number | string
  /** Right-aligned header controls. */
  actions?: ReactNode
  /** Rendered inline after the title (filters, segmented controls). */
  toolbar?: ReactNode
  /** A docked <Inspector>, or null. */
  panel?: ReactNode
  children: ReactNode
  contentClassName?: string
}

export function Page({ title, subtitle, count, actions, toolbar, panel, children, contentClassName }: PageProps) {
  return (
    <div className="flex flex-1 min-h-0 min-w-0">
      <div className="flex-1 min-w-0 overflow-y-auto">
        <div className={cn('p-6 flex flex-col gap-4', contentClassName)}>
          <header className="flex items-center justify-between gap-4 flex-wrap">
            <div className="flex items-center gap-3 min-w-0 flex-wrap">
              <div className="min-w-0">
                <h1 className="text-xl font-semibold text-fg flex items-center gap-2">
                  {title}
                  {count !== undefined && <span className="text-sm font-medium text-fg-subtle tabular-nums">{count}</span>}
                </h1>
                {subtitle && <p className="text-[12.5px] text-fg-muted mt-0.5">{subtitle}</p>}
              </div>
              {toolbar}
            </div>
            {actions && <div className="flex items-center gap-2 flex-none">{actions}</div>}
          </header>
          {children}
        </div>
      </div>
      {panel}
    </div>
  )
}

export function EmptyState({ icon, title, description, action }: { icon?: ReactNode; title: string; description?: ReactNode; action?: ReactNode }) {
  return (
    <div className="bg-surface rounded-lg border border-line p-10 text-center">
      {icon && <div className="w-10 h-10 mx-auto mb-3 text-fg-subtle [&>svg]:w-10 [&>svg]:h-10">{icon}</div>}
      <h3 className="text-base font-medium text-fg">{title}</h3>
      {description && <p className="text-sm text-fg-muted mt-1 max-w-md mx-auto">{description}</p>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  )
}
