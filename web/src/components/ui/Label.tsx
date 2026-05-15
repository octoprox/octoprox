// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type LabelHTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const labelClasses = 'block text-sm font-medium text-fg-muted mb-1'

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn(labelClasses, className)} {...props} />
}
