// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * A menu that opens from a button: the Help, project and account menus.
 *
 * `usePopoverMenu` holds the behaviour they share: it places the menu from the
 * button's position when it opens and on every resize; it closes on navigation,
 * when the button moves, on Escape or Tab and on a press outside; it moves focus
 * into the menu once; and the arrow keys walk its items. `PopoverMenu` renders
 * it fixed in a portal, so neither the collapsed rail nor the phone overlay
 * clips it, with a backdrop on touch screens.
 */

import { useEffect, useEffectEvent, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { useLocation } from 'react-router'
import { cn } from '../../utils/cn'

/** Space kept between a menu and the edges of the screen (or the on-screen keyboard). */
export const MENU_GUTTER = 8

const ITEMS = '[role="menuitem"], [role="menuitemradio"]'

interface PopoverMenuOptions {
  /** Where the menu goes, from the button's rectangle. Called when it opens and again on every resize while open. */
  place: (button: DOMRect) => CSSProperties
  /** A value that moves the button when it changes, such as the sidebar collapsing: the menu closes then, as on navigation. */
  moves?: unknown
  /** The element to focus when the menu opens; nothing yet (a list still loading) tries again on the next render. The first item by default. */
  initialFocus?: (menu: HTMLElement) => HTMLElement | null | undefined
}

export interface PopoverMenuState {
  open: boolean
  close: () => void
  /** Spread on the button that opens the menu. */
  buttonProps: {
    ref: RefObject<HTMLButtonElement | null>
    onClick: () => void
    'aria-haspopup': 'menu'
    'aria-expanded': boolean
  }
  /** Set while the menu is open and placed: what `PopoverMenu` renders it with. */
  menu: {
    ref: RefObject<HTMLDivElement | null>
    backdropRef: RefObject<HTMLDivElement | null>
    style: CSSProperties
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => void
    close: () => void
  } | null
}

export function usePopoverMenu({ place, moves, initialFocus }: PopoverMenuOptions): PopoverMenuState {
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const [style, setStyle] = useState<CSSProperties | null>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const backdropRef = useRef<HTMLDivElement>(null)
  const focused = useRef(false)

  const close = () => setOpen(false)
  const closeToButton = () => {
    setOpen(false)
    buttonRef.current?.focus()
  }

  useEffect(() => { setOpen(false) }, [location.pathname, moves])

  // Placed before it paints, so a reopened menu never shows where it was last time. The visual viewport shrinks under
  // a phone's on-screen keyboard, so it counts as a resize too.
  const reposition = useEffectEvent(() => {
    const rect = buttonRef.current?.getBoundingClientRect()
    if (rect) setStyle({ position: 'fixed', ...place(rect) })
  })
  useLayoutEffect(() => {
    if (!open) return
    reposition()
    window.addEventListener('resize', reposition)
    window.visualViewport?.addEventListener('resize', reposition)
    return () => {
      window.removeEventListener('resize', reposition)
      window.visualViewport?.removeEventListener('resize', reposition)
    }
  }, [open])

  // A press on the backdrop closes through its own click, so the touch that lifts is not left to press what is under it.
  useEffect(() => {
    if (!open) return
    const onPointer = (e: PointerEvent) => {
      const t = e.target as Node
      if (menuRef.current?.contains(t) || buttonRef.current?.contains(t) || t === backdropRef.current) return
      setOpen(false)
    }
    document.addEventListener('pointerdown', onPointer)
    return () => document.removeEventListener('pointerdown', onPointer)
  }, [open])

  const items = () => Array.from(menuRef.current?.querySelectorAll<HTMLElement>(ITEMS) ?? [])

  // Focus moves in once per opening, so a resize does not pull it back. Checked after every render until it lands,
  // since what to focus may still be loading.
  useEffect(() => {
    if (!open) { focused.current = false; return }
    if (focused.current || !style || !menuRef.current) return
    const target = initialFocus ? initialFocus(menuRef.current) : items()[0]
    if (target) { target.focus(); focused.current = true }
  })

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    // The menu sits at the end of the page, so Tab would leave the button behind: it closes like Escape.
    if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); closeToButton(); return }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
    // In a text field Home and End move the caret.
    if ((e.key === 'Home' || e.key === 'End') && e.target instanceof HTMLInputElement) return
    e.preventDefault()
    const list = items()
    if (list.length === 0) return
    const at = list.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'Home' ? 0
      : e.key === 'End' ? list.length - 1
      : e.key === 'ArrowDown' ? (at + 1) % list.length
      : at <= 0 ? list.length - 1 : at - 1
    list[next].focus()
  }

  return {
    open,
    close,
    buttonProps: { ref: buttonRef, onClick: () => setOpen((v) => !v), 'aria-haspopup': 'menu', 'aria-expanded': open },
    menu: open && style ? { ref: menuRef, backdropRef, style, onKeyDown, close } : null,
  }
}

/** The open menu of a `usePopoverMenu`, or nothing while it is closed. */
export function PopoverMenu({ state, label, className, children }: { state: PopoverMenuState; label: string; className?: string; children: ReactNode }) {
  const { menu } = state
  if (!menu) return null
  return createPortal(
    <>
      {/* Touch screens: a tap beside the menu only closes it and never presses what is underneath. */}
      <div ref={menu.backdropRef} onClick={menu.close} className="fixed inset-0 z-50 pointer-fine:hidden" aria-hidden />
      <div
        ref={menu.ref}
        role="menu"
        aria-label={label}
        onKeyDown={menu.onKeyDown}
        style={menu.style}
        className={cn('z-50 bg-surface text-fg border border-line-strong rounded-lg shadow-lg text-[13px]', className)}
      >
        {children}
      </div>
    </>,
    document.body
  )
}

/**
 * For a button at the foot of the sidebar: beside it, bottom aligned, or above it on a phone with the sidebar open,
 * where there is no room beside it. Narrows to the screen's width; the height left is the menu's to scroll in.
 */
export function besideSidebarFoot(button: DOMRect, width: number, collapsed: boolean): CSSProperties {
  const beside = collapsed || button.right + MENU_GUTTER + width <= window.innerWidth - MENU_GUTTER
  const left = beside ? button.right + MENU_GUTTER : button.left
  const bottom = beside ? Math.max(MENU_GUTTER, window.innerHeight - button.bottom) : window.innerHeight - button.top + 4
  return { left, bottom, width: Math.min(width, window.innerWidth - left - MENU_GUTTER), maxHeight: window.innerHeight - bottom - MENU_GUTTER }
}
