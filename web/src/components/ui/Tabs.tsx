// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

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
}

export function Tabs<T extends string>({ tabs, active, onChange, size = 'md', className }: TabsProps<T>) {
  return (
    <div className={cn('flex gap-0.5 border-b border-line overflow-x-auto', className)} role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.id}
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
export function Segmented<T extends string>({ options, value, onChange, size = 'md', className }: {
  options: { value: T; label: string }[]
  value: T
  onChange: (v: T) => void
  size?: 'sm' | 'md'
  className?: string
}) {
  return (
    <div className={cn('inline-flex bg-surface-raised rounded-lg p-0.5', className)} role="radiogroup">
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
            value === o.value ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg'
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}
