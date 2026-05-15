// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type HTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

const cardClasses = 'bg-surface rounded-lg shadow'

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn(cardClasses, className)} {...props} />
}
