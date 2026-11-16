// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { BellOff } from 'lucide-react'
import type { MuteDuration } from '../../api/client'
import { cn } from '../../utils/cn'
import { MENU_GUTTER, PopoverMenu, usePopoverMenu } from '../ui/PopoverMenu'

const MENU_WIDTH = 200
/** Four items of 44px on a touch screen and the menu's padding: below the button when that fits, above it otherwise. */
const MENU_HEIGHT = 4 * 44 + 12

const CHOICES: { duration: MuteDuration; label: string }[] = [
  { duration: 'day', label: 'Mute for 1 day' },
  { duration: 'week', label: 'Mute for 1 week' },
  { duration: 'month', label: 'Mute for 1 month' },
  { duration: 'until_clear', label: 'Mute until it clears' },
]

/** The bell button beside a line and its menu of how long to mute it for. Only for those who may change the project. */
export function MuteMenu({ sentence, onMute, disabled, className }: { sentence: string; onMute: (duration: MuteDuration) => void; disabled?: boolean; className?: string }) {
  // Right edge under the button, narrowed to the screen; above it where the screen ends first.
  const menu = usePopoverMenu({
    place: (button) => {
      const width = Math.min(MENU_WIDTH, window.innerWidth - 2 * MENU_GUTTER)
      const left = Math.min(Math.max(MENU_GUTTER, button.right - width), window.innerWidth - MENU_GUTTER - width)
      const viewHeight = window.visualViewport?.height ?? window.innerHeight
      const below = viewHeight - button.bottom - 4 - MENU_GUTTER
      const above = button.top - 4 - MENU_GUTTER
      return below >= MENU_HEIGHT || below >= above
        ? { left, width, top: button.bottom + 4, maxHeight: below }
        : { left, width, bottom: window.innerHeight - button.top + 4, maxHeight: above }
    },
  })
  const pick = (duration: MuteDuration) => {
    menu.close()
    onMute(duration)
  }
  return (
    <>
      <button
        {...menu.buttonProps}
        type="button"
        disabled={disabled}
        title="Mute"
        aria-label={`Mute: ${sentence}`}
        className={cn(
          'flex-none inline-flex items-center justify-center p-1.5 rounded-md text-fg-subtle hover:text-fg hover:bg-surface-raised transition-colors disabled:opacity-50 pointer-coarse:w-11 pointer-coarse:h-11',
          menu.open && 'bg-surface-raised text-fg',
          className
        )}
      >
        <BellOff className="w-3.5 h-3.5" />
      </button>
      <PopoverMenu state={menu} label="Mute" className="py-1.5 overflow-y-auto overscroll-contain">
        {CHOICES.map(({ duration, label }) => (
          <button
            key={duration}
            type="button"
            role="menuitem"
            onClick={() => pick(duration)}
            className="w-full flex items-center px-3 h-8 pointer-coarse:h-11 text-left text-fg-muted outline-none hover:bg-surface-raised hover:text-fg focus-visible:bg-surface-raised focus-visible:text-fg transition-colors"
          >
            {label}
          </button>
        ))}
      </PopoverMenu>
    </>
  )
}
