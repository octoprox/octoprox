// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Badge } from './ui'
import { cn } from '../utils/cn'

/** Marks a project being deleted: its cloud instances are terminated first, then it goes. */
export function DeletingBadge({ className }: { className?: string }) {
  return (
    <Badge color="red" className={cn('flex-none', className)} title="Its cloud instances are being terminated; the project goes once they are">
      Deleting
    </Badge>
  )
}
