// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { AttentionLine, AttentionWhen } from '../../api/client'
import { formatClock, formatDay, relativeTime } from '../../utils/format'

/** A line's whole sentence, for hover titles and labels. */
export const lineSentence = (line: AttentionLine): string => line.lead + line.subject + line.text

/** The one time a line shows: when it last happened, last failed or resets, or an install alert's own time. */
export function whenText(when: AttentionWhen): string {
  switch (when.kind) {
    case 'last_at': return `last at ${formatClock(when.at)}`
    case 'last_failed': return `last failed ${relativeTime(when.at)}`
    case 'resets': return `resets ${formatDay(when.at)}`
    case 'at': return `at ${formatClock(when.at)}`
  }
}

/** Since when a line has held: the current spell, which a reopen starts again. */
export const sinceText = (line: AttentionLine): string => `since ${formatClock(line.since)}`

/** An install-wide sentence with its `{time}` in the viewer's clock: a line's `at`, a stored alert's `when.at`. */
export const installText = (text: string, at: string | null | undefined): string => text.replace('{time}', formatClock(at))
