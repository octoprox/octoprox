import { type HTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const alertVariants = {
  error:
    'bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400',
  warning:
    'bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 text-yellow-700 dark:text-yellow-400',
  info:
    'bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 text-blue-700 dark:text-blue-400',
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
