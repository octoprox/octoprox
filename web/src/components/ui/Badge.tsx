// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type HTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const colorMap = {
  green: 'bg-success-soft text-success',
  blue: 'bg-primary-soft text-primary-soft-fg',
  yellow: 'bg-warning-soft text-warning',
  red: 'bg-danger-soft text-danger',
  gray: 'bg-surface-raised text-fg-muted',
  // Decorative palette badges — fixed colors, not theme-driven.
  orange: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-400',
  purple: 'bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-400',
  slate: 'bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-400',
} as const

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  color?: keyof typeof colorMap
}

export function Badge({ color = 'gray', className, ...props }: BadgeProps) {
  return (
    <span
      className={cn('px-2 py-1 rounded-full text-xs font-medium', colorMap[color], className)}
      {...props}
    />
  )
}
