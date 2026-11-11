// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { createPortal } from 'react-dom'
import { History } from 'lucide-react'
import type { AuditEntityType } from '../../api/client'
import { entityLabel } from '../../utils/audit'
import { cn } from '../../utils/cn'
import { Button, Inspector } from '../ui'
import { EntityHistory } from './EntityHistory'

/**
 * A History button for what has no inspector of its own (install-wide settings, an IP database): it opens the
 * entity's History in a sheet over the page, at every width.
 */
export function HistoryButton({ entityType, entityId, name, className }: {
  entityType: AuditEntityType
  /** '1' for an install-wide singleton (IP attribution settings, system settings) and for backups. */
  entityId: string
  /** What the sheet says it shows; the entity type's name by default. */
  name?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className={cn('pointer-coarse:h-11', className)}
        // Placed in a clickable row or card, opening the sheet is not a click on that too.
        onClick={(e) => { e.stopPropagation(); setOpen(true) }}
      >
        <History className="w-3.5 h-3.5" /> History
      </Button>
      {/* In <body>: a page is a container-query container, which would anchor `fixed` to it. Clicks stay in the
          sheet, so a row or card it was opened from does not take them as its own. */}
      {open && createPortal(
        <div onClick={(e) => e.stopPropagation()}>
          {/* The inspector's own backdrop is for narrow screens, where it floats; here it floats at every width. */}
          <div className="hidden xl:block fixed inset-0 z-30 bg-overlay/30" onClick={() => setOpen(false)} aria-hidden />
          <Inspector
            title="History"
            subtitle={name ?? entityLabel(entityType)}
            onClose={() => setOpen(false)}
            className="xl:fixed xl:inset-y-0 xl:right-0 xl:z-40 xl:shadow-2xl"
          >
            <EntityHistory entityType={entityType} entityId={entityId} />
          </Inspector>
        </div>,
        document.body,
      )}
    </>
  )
}
