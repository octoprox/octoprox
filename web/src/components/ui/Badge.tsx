// Copyright 2025 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type HTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const colorMap = {
  green: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-400',
  blue: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-400',
  yellow: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-400',
  red: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-400',
  gray: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-400',
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
