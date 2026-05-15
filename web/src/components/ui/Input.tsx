// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { forwardRef, type InputHTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const inputClasses =
  'w-full px-4 py-2 border border-line-strong rounded-lg bg-surface text-fg focus:ring-1 focus:ring-ring focus:border-ring placeholder:text-fg-subtle'

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input ref={ref} className={cn(inputClasses, className)} {...props} />
  )
)
Input.displayName = 'Input'
