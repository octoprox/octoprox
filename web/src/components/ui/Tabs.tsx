// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef } from 'react'
import { cn } from '../../utils/cn'

export interface TabItem<T extends string> {
  id: T
  label: string
}

interface TabsProps<T extends string> {
  tabs: TabItem<T>[]
  active: T
  onChange: (id: T) => void
  size?: 'sm' | 'md'
  className?: string
  /** A link's request for a tab (a new object each time): the active tab is scrolled into view for it, the first too. */
  reveal?: object | null
  /** Brings the active tab into view by scrolling the strip alone, never the page: for a strip below the fold. */
  sideways?: boolean
}

export function Tabs<T extends string>({ tabs, active, onChange, size = 'md', className, reveal, sideways }: TabsProps<T>) {
  // A strip wider than a phone scrolls sideways: the active tab is brought into view when it changes or a link asks
  // for it. Not on a plain first render, where the strip may sit below the fold and the page must not jump to it.
  const stripRef = useRef<HTMLDivElement>(null)
  const activeRef = useRef<HTMLButtonElement>(null)
  const shown = useRef(active)
  useEffect(() => {
    if (shown.current === active && !reveal) return
    shown.current = active
    const tab = activeRef.current
    const strip = stripRef.current
    if (!tab || !strip) return
    if (!sideways) {
      tab.scrollIntoView({ inline: 'nearest', block: 'nearest' })
      return
    }
    const s = strip.getBoundingClientRect()
    const t = tab.getBoundingClientRect()
    if (t.left < s.left) strip.scrollLeft -= s.left - t.left
    else if (t.right > s.right) strip.scrollLeft += t.right - s.right
  }, [active, reveal])

  return (
    <div ref={stripRef} className={cn('flex gap-0.5 border-b border-line overflow-x-auto', className)} role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          ref={active === tab.id ? activeRef : undefined}
          type="button"
          role="tab"
          aria-selected={active === tab.id}
          onClick={() => onChange(tab.id)}
          className={cn(
            'relative whitespace-nowrap flex-none font-medium transition-colors -mb-px border-b-2',
            size === 'sm' ? 'px-2.5 py-1.5 text-xs' : 'px-3 py-2 text-[13px]',
            active === tab.id
              ? 'text-fg border-primary'
              : 'text-fg-muted border-transparent hover:text-fg'
          )}
        >
          {tab.label}
        </button>
      ))}
    </div>
  )
}

/** Segmented control (pill group) for small option sets like time ranges. */
export function Segmented<T extends string>({ options, value, onChange, size = 'md', className, label }: {
  options: { value: T; label: string }[]
  value: T
  onChange: (v: T) => void
  size?: 'sm' | 'md'
  className?: string
  /** The group's accessible name. */
  label?: string
}) {
  return (
    <div className={cn('inline-flex bg-surface-raised rounded-lg p-0.5', className)} role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            'rounded-md font-medium transition-colors',
            size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3 py-1.5 text-[13px]',
            value === o.value ? 'bg-surface text-fg shadow-xs' : 'text-fg-muted hover:text-fg'
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}
