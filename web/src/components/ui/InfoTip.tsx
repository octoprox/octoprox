// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Info } from 'lucide-react'
import { cn } from '../../utils/cn'

const BUBBLE_WIDTH = 256
const MARGIN = 8

/**
 * A small info icon that reveals a short explanation on hover, keyboard focus or a tap.
 * Sits next to a control's label; the label itself stays to one line.
 *
 * Only a mouse opens it on hover, and a press does not open it on focus: a tap
 * also fires hover and focus before its click, which would open the bubble and
 * then toggle it shut. A tap anywhere else closes it, since a tapped button is
 * not always focused (Safari), so blur alone may never come.
 *
 * The bubble is portalled to the document body and positioned with fixed
 * coordinates, so scrolling containers such as the docked inspector cannot
 * clip it, and it is shifted to stay inside the viewport.
 */
export function InfoTip({ children, className, label = 'More information' }: { children: ReactNode; className?: string; label?: string }) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  // A press under way: the focus it brings is not keyboard focus, and its click decides.
  const pressed = useRef(false)
  const id = useId()

  useEffect(() => {
    if (!open) return
    const close = (e: PointerEvent) => {
      if (!buttonRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [open])

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
    <span
      className={cn('relative inline-flex align-middle', className)}
      onPointerEnter={(e) => { if (e.pointerType === 'mouse') setOpen(true) }}
      onPointerLeave={(e) => { if (e.pointerType === 'mouse') setOpen(false) }}
    >
      <button
        ref={buttonRef}
        type="button"
        aria-label={label}
        aria-describedby={open ? id : undefined}
        onPointerDown={() => { pressed.current = true }}
        // A press the browser takes over for a scroll ends here, with no click to clear it.
        onPointerCancel={() => { pressed.current = false }}
        onFocus={() => { if (!pressed.current) setOpen(true) }}
        onBlur={() => { pressed.current = false; setOpen(false) }}
        onClick={() => { pressed.current = false; setOpen((v) => !v) }}
        className="p-0.5 -m-0.5 rounded-sm text-fg-subtle hover:text-fg focus:outline-hidden focus:ring-1 focus:ring-ring"
      >
        <Info className="w-3.5 h-3.5" />
      </button>
      {open && pos && createPortal(
        <span
          id={id}
          role="tooltip"
          style={{ position: 'fixed', left: pos.left, top: pos.top, width: BUBBLE_WIDTH }}
          className="z-100 block rounded-lg border border-line bg-surface p-2.5 text-xs font-normal leading-relaxed text-fg shadow-lg normal-case"
        >
          {children}
        </span>,
        document.body,
      )}
    </span>
  )
}
