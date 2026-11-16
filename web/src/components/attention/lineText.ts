// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { AttentionLine, AttentionWhen, InstallAttentionLine } from '../../api/client'
import { formatClock, formatDay, relativeTime } from '../../utils/format'

/** A line's whole sentence, for hover titles and labels. */
export const lineSentence = (line: AttentionLine): string => line.lead + line.subject + line.text

/** The one time a line shows: when it last happened, last failed or resets, never when it started. */
export function whenText(when: AttentionWhen): string {
  if (when.kind === 'last_at') return `last at ${formatClock(when.at)}`
  if (when.kind === 'last_failed') return `last failed ${relativeTime(when.at)}`
  return `resets ${formatDay(when.at)}`
}

/** An install-wide line with its time in the viewer's clock. */
export const installText = (line: InstallAttentionLine): string => line.text.replace('{time}', formatClock(line.at))
