// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { forwardRef, type TextareaHTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const textareaClasses =
  'w-full px-4 py-2 border border-line-strong rounded-lg bg-surface text-fg focus:ring-1 focus:ring-ring focus:border-ring placeholder:text-fg-subtle'

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea ref={ref} className={cn(textareaClasses, className)} {...props} />
  )
)
Textarea.displayName = 'Textarea'
