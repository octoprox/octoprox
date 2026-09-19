// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Info } from 'lucide-react'
import { cn } from '../../utils/cn'

const BUBBLE_WIDTH = 256
const MARGIN = 8

/**
 * A small info icon that reveals a short explanation on hover or keyboard focus.
 * Sits next to a control's label; the label itself stays to one line.
 *
 * The bubble is portalled to the document body and positioned with fixed
 * coordinates, so scrolling containers such as the docked inspector cannot
 * clip it, and it is shifted to stay inside the viewport.
 */
export function InfoTip({ children, className, label = 'More information' }: { children: ReactNode; className?: string; label?: string }) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const id = useId()

  useLayoutEffect(() => {
    if (!open) { setPos(null); return }
    const place = () => {
      const rect = buttonRef.current?.getBoundingClientRect()
      if (!rect) return
      const left = Math.min(Math.max(MARGIN, rect.left + rect.width / 2 - BUBBLE_WIDTH / 2), window.innerWidth - BUBBLE_WIDTH - MARGIN)
      setPos({ left, top: rect.bottom + 6 })
    }
    place()
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [open])

  return (
    <span className={cn('relative inline-flex align-middle', className)} onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button
        ref={buttonRef}
        type="button"
        aria-label={label}
        aria-describedby={open ? id : undefined}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen((v) => !v)}
        className="p-0.5 -m-0.5 rounded text-fg-subtle hover:text-fg focus:outline-none focus:ring-1 focus:ring-ring"
      >
        <Info className="w-3.5 h-3.5" />
      </button>
      {open && pos && createPortal(
        <span
          id={id}
          role="tooltip"
          style={{ position: 'fixed', left: pos.left, top: pos.top, width: BUBBLE_WIDTH }}
          className="z-[100] block rounded-lg border border-line bg-surface p-2.5 text-xs font-normal leading-relaxed text-fg shadow-lg normal-case"
        >
          {children}
        </span>,
        document.body,
      )}
    </span>
  )
}
