// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type LabelHTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const labelClasses = 'block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1'

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn(labelClasses, className)} {...props} />
}
