// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { forwardRef, type SelectHTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const selectClasses =
  'w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-1 focus:ring-blue-500 focus:border-blue-500'

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, ...props }, ref) => (
    <select ref={ref} className={cn(selectClasses, className)} {...props} />
  )
)
Select.displayName = 'Select'
