// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ComponentProps } from 'react'
import { cn } from '../../utils/cn'

const selectClasses =
  'w-full px-4 py-2 border border-line-strong rounded-lg bg-surface text-fg focus:ring-1 focus:ring-ring focus:border-ring'

export function Select({ className, ...props }: ComponentProps<'select'>) {
  return <select className={cn(selectClasses, className)} {...props} />
}
