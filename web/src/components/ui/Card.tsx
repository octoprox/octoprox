// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type HTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const cardClasses = 'bg-surface rounded-lg border border-line'

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn(cardClasses, className)} {...props} />
}

interface CardHeaderProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  title: React.ReactNode
  action?: React.ReactNode
}

export function CardHeader({ title, action, className, ...props }: CardHeaderProps) {
  return (
    <div className={cn('flex items-center justify-between gap-3', className)} {...props}>
      <div className="text-sm font-semibold text-fg">{title}</div>
      {action}
    </div>
  )
}
