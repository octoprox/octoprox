// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { ExitCoverage } from '../../api/client'

/** Short label for a dynamic-sessions connector whose session-less traffic is not fully observed. */
export function coverageLabel(coverage: ExitCoverage | null | undefined): { text: string; title: string } | null {
  if (!coverage || !coverage.dynamic || coverage.sampled_percent >= 100) return null
  if (!coverage.preflight_on) {
    return { text: 'preflight off', title: 'Dynamic sessions with preflight off: exits are not observed. Turn preflight on for the project to see them.' }
  }
  return {
    text: `sampled ${coverage.sampled_percent}%`,
    title: `Dynamic sessions: every client session is observed once; requests without a session are echoed for ${coverage.sampled_percent}% of requests. Distinct-exit and reuse figures for session-less traffic are undercounts.`,
  }
}
