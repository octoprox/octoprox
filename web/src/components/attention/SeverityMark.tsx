// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { AlertTriangle } from 'lucide-react'
import type { AttentionSeverity } from '../../api/client'
import { cn } from '../../utils/cn'

/** A line's severity in a 14px slot: a red triangle for a problem, an amber dot for a warning, said out for screen readers. */
export function SeverityMark({ severity, className }: { severity: AttentionSeverity; className?: string }) {
  return (
    <span className={cn('flex-none w-3.5 h-3.5 inline-flex items-center justify-center', className)}>
      {severity === 'problem'
        ? <AlertTriangle className="w-3.5 h-3.5 text-danger" aria-hidden />
        : <span className="w-2 h-2 rounded-full bg-warning" aria-hidden />}
      <span className="sr-only">{severity === 'problem' ? 'Problem:' : 'Warning:'}</span>
    </span>
  )
}
