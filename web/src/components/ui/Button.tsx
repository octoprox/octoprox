// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const baseClasses =
  'inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed'

const variants = {
  primary: 'bg-primary text-fg-on-primary hover:bg-primary-hover',
  success: 'bg-success text-fg-on-primary hover:brightness-110',
  danger: 'bg-danger text-fg-on-primary hover:brightness-110',
  secondary: 'bg-surface-raised text-fg hover:bg-line',
  ghost: 'text-fg-muted hover:text-fg hover:bg-surface-raised',
  outline: 'border border-line-strong text-fg hover:bg-surface-raised',
} as const

const sizes = {
  sm: 'px-3 py-1.5 text-sm',
  md: 'px-4 py-2 text-sm',
  icon: 'p-2',
} as const

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: keyof typeof variants
  size?: keyof typeof sizes
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = 'primary', size = 'md', ...props }, ref) => (
    <button
      ref={ref}
      className={cn(baseClasses, variants[variant], sizes[size], className)}
      {...props}
    />
  )
)
Button.displayName = 'Button'
