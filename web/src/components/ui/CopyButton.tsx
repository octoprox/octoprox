// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { cn } from '../../utils/cn'

/** An icon button that copies `text`, showing a tick for a moment after; `className` places it in its row. */
export function CopyButton({ text, label = 'Copy', className }: { text: string; label?: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable in insecure contexts; the value is selectable */ }
  }
  return (
    <button type="button" onClick={copy} title={label} aria-label={label} className={cn('flex-none p-1 rounded-sm text-fg-subtle hover:text-fg hover:bg-surface-raised', className)}>
      {copied ? <Check className="w-3.5 h-3.5 text-success" /> : <Copy className="w-3.5 h-3.5" />}
    </button>
  )
}
