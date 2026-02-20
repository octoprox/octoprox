// Copyright 2025 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const baseClasses =
  'inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed'

const variants = {
  primary:
    'bg-blue-600 text-white hover:bg-blue-700 dark:bg-violet-600 dark:hover:bg-violet-700',
  success:
    'bg-green-600 text-white hover:bg-green-700 dark:bg-emerald-700 dark:hover:bg-emerald-800',
  danger: 'bg-red-600 text-white hover:bg-red-700',
  secondary:
    'bg-gray-200 dark:bg-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-500',
  ghost:
    'text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700',
  outline:
    'border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-300',
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
