// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { forwardRef, type SelectHTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const selectClasses =
  'w-full px-4 py-2 border border-line-strong rounded-lg bg-surface text-fg focus:ring-1 focus:ring-ring focus:border-ring'

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, ...props }, ref) => (
    <select ref={ref} className={cn(selectClasses, className)} {...props} />
  )
)
Select.displayName = 'Select'
