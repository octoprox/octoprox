// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from 'react'
import { cn } from '../../utils/cn'
import { LogoLoader } from './LogoLoader'

/** Content about to be replaced: dimmed once the loader shows, and not clickable. It returns at once when the new content lands. */
export const RELOADING = 'opacity-50 pointer-events-none transition-opacity delay-200'

interface ReloadingProps {
  /** The next version is loading while this one stays on screen. */
  active: boolean
  /** Where the loader sits: the middle of a chart or card, or the top of a block taller than the screen. */
  align?: 'center' | 'top'
  /** The content's own layout, e.g. the spacing between the cards it holds. */
  className?: string
  children: ReactNode
}

/** Keeps content in place while its next version loads, dimmed under the loader, so nothing on the page moves. */
export function Reloading({ active, align = 'center', className, children }: ReloadingProps) {
  return (
    <div className="relative" aria-busy={active || undefined}>
      <div className={cn(className, active && RELOADING)}>{children}</div>
      {active && (
        <div className={cn('pointer-events-none absolute inset-0 flex justify-center', align === 'top' ? 'items-start' : 'items-center')}>
          <LogoLoader className={align === 'top' ? 'py-10' : undefined} />
        </div>
      )}
    </div>
  )
}
