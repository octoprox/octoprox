// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { type ReactNode } from 'react'

/** A coloured dot, a bold count and what it counts: the pool health legend and the Needs attention counts. */
export function Legend({ dot, n, label }: { dot: string; n: number; label: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <span className={`w-2 h-2 rounded-full ${dot}`} />
      <b className="text-fg font-semibold">{n}</b> {label}
    </span>
  )
}
