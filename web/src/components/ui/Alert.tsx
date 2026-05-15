// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type HTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const alertVariants = {
  error: 'bg-danger-soft border border-danger/30 text-danger',
  warning: 'bg-warning-soft border border-warning/30 text-warning',
  info: 'bg-primary-soft border border-primary/30 text-primary-soft-fg',
  success: 'bg-success-soft border border-success/30 text-success',
} as const

interface AlertProps extends HTMLAttributes<HTMLDivElement> {
  variant?: keyof typeof alertVariants
}

export function Alert({ variant = 'error', className, ...props }: AlertProps) {
  return (
    <div
      className={cn('px-4 py-3 rounded-lg text-sm', alertVariants[variant], className)}
      {...props}
    />
  )
}
