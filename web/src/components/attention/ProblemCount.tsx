// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { AlertTriangle } from 'lucide-react'
import { plural } from '../../utils/format'

/** A project's unmuted problems beside its name in the project list and switcher; nothing when there are none. */
export function ProblemCount({ count }: { count: number | undefined }) {
  if (!count) return null
  return (
    <span className="flex-none inline-flex items-center gap-1 text-xs font-medium text-danger tabular-nums" title={`${count} ${plural(count, 'problem needs', 'problems need')} attention`}>
      <AlertTriangle className="w-3.5 h-3.5" aria-hidden />
      {count}
      <span className="sr-only"> {plural(count, 'problem', 'problems')}</span>
    </span>
  )
}
