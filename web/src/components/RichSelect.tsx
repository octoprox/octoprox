// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useEffect, useEffectEvent, useLayoutEffect, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ChevronDown, Check, Search } from 'lucide-react'
import { cn } from '../utils/cn'
import { MENU_GUTTER } from './ui/PopoverMenu'

export interface RichSelectOption {
  value: string
  label: string
  description?: string
  badge?: string
}

interface RichSelectProps {
  options: RichSelectOption[]
  value: string
  onChange: (value: string) => void
  placeholder?: string
  required?: boolean
  disabled?: boolean
  className?: string
  /** A filter box over the list, matching label, value and description; typing on the closed control opens it. */
  searchable?: boolean
  /** 'sm' is the compact picker that sits in a line of text, as on a card's header; 'md' is the form field. */
  size?: 'md' | 'sm'
  /** The control's name for screen readers, where no label names it. */
  'aria-label'?: string
}

/** The narrowest the list gets, so a short control still opens a list wide enough to read. */
const MIN_MENU_WIDTH = 256
/** The open list's tallest (max-h-72), and its options' (max-h-60) under the filter. */
const MAX_MENU_HEIGHT = 288
const MAX_LIST_HEIGHT = 240

const TRIGGER_SIZES = {
  md: 'px-3 py-1.5 text-sm rounded-lg pointer-coarse:min-h-11',
  sm: 'h-7 px-2 text-[12.5px] rounded-md pointer-coarse:h-11 pointer-coarse:text-base',
}

/**
 * Where the open list goes, relative to the control: as wide as the control or MIN_MENU_WIDTH, lined up with its left
 * edge or else its right, and below it unless there is more room above. Kept inside the screen and the nearest box
 * that clips it (a side panel's body), with MENU_GUTTER to spare.
 */
function menuPlacement(container: HTMLElement, height: number): CSSProperties {
  const rect = container.getBoundingClientRect()
  let top = 0
  let bottom = window.visualViewport?.height ?? window.innerHeight
  let left = 0
  let right = document.documentElement.clientWidth
  for (let el = container.parentElement; el && el !== document.body; el = el.parentElement) {
    const style = getComputedStyle(el)
    if (style.overflowX === 'visible' && style.overflowY === 'visible') continue
    const box = el.getBoundingClientRect()
    top = Math.max(top, box.top)
    bottom = Math.min(bottom, box.bottom)
    left = Math.max(left, box.left)
    right = Math.min(right, box.right)
    break
  }
  const width = Math.min(Math.max(rect.width, MIN_MENU_WIDTH), right - left - 2 * MENU_GUTTER)
  const start = rect.left + width <= right - MENU_GUTTER ? rect.left : rect.right - width
  const x = Math.min(Math.max(start, left + MENU_GUTTER), right - MENU_GUTTER - width)
  const below = bottom - rect.bottom - 4 - MENU_GUTTER
  const above = rect.top - top - 4 - MENU_GUTTER
  const vertical: CSSProperties = below >= height || below >= above
    ? { top: '100%', marginTop: 4, maxHeight: Math.min(MAX_MENU_HEIGHT, below) }
    : { bottom: '100%', marginBottom: 4, maxHeight: Math.min(MAX_MENU_HEIGHT, above) }
  return { ...vertical, left: x - rect.left, width }
}

/** How well an option matches the filter, best first: its exact value, then label prefix, word prefix, anywhere; null for no match. */
function matchRank(option: RichSelectOption, needle: string): number | null {
  const label = option.label.toLowerCase()
  if (option.value.toLowerCase() === needle) return 0
  if (label.startsWith(needle)) return 1
  if (label.split(/[\s(,.-]+/).some((word) => word.startsWith(needle))) return 2
  if (label.includes(needle) || option.value.toLowerCase().includes(needle) || option.description?.toLowerCase().includes(needle)) return 3
  return null
}

export function RichSelect({
  options,
  value,
  onChange,
  placeholder = 'Select an option',
  required = false,
  disabled = false,
  className,
  searchable = false,
  size = 'md',
  'aria-label': ariaLabel,
}: RichSelectProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [placement, setPlacement] = useState<CSSProperties | null>(null)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const containerRef = useRef<HTMLDivElement>(null)
  const filterRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  // Opened by a key typed on the trigger: there is a keyboard, so the filter takes the next keys on any screen.
  const typedRef = useRef(false)

  const selectedOption = options.find((opt) => opt.value === value)
  const needle = query.trim().toLowerCase()
  const shown = searchable && needle
    ? options
        .map((option, index) => ({ option, index, rank: matchRank(option, needle) }))
        .filter((m): m is { option: RichSelectOption; index: number; rank: number } => m.rank !== null)
        .sort((a, b) => a.rank - b.rank || a.index - b.index)
        .map((m) => m.option)
    : options

  // The mark starts on the best match, or on the chosen option before anything is typed; a closed list forgets its filter.
  useEffect(() => { setActive(needle ? 0 : Math.max(options.findIndex((o) => o.value === value), 0)) }, [needle, isOpen])
  useEffect(() => { if (!isOpen) setQuery('') }, [isOpen])
  // The filter takes focus with a mouse or after typing; a touch screen is left alone, since focus would open the
  // keyboard over the list.
  useEffect(() => {
    if (isOpen && searchable && (typedRef.current || window.matchMedia('(pointer: fine)').matches)) filterRef.current?.focus()
    typedRef.current = false
  }, [isOpen, searchable])
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])

  // Placed before it paints, from the list's full height, and again when the screen or the visual viewport (an
  // on-screen keyboard) resizes.
  const place = useEffectEvent(() => {
    const list = listRef.current
    if (!list || !containerRef.current) return
    const filter = filterRef.current?.parentElement?.offsetHeight ?? 0
    // The options' full height, not what the last placement left them, plus the borders.
    const height = Math.min(MAX_MENU_HEIGHT, filter + Math.min(list.scrollHeight, MAX_LIST_HEIGHT) + 2)
    setPlacement(menuPlacement(containerRef.current, height))
  })
  useLayoutEffect(() => {
    if (!isOpen) return
    place()
    window.addEventListener('resize', place)
    window.visualViewport?.addEventListener('resize', place)
    return () => {
      window.removeEventListener('resize', place)
      window.visualViewport?.removeEventListener('resize', place)
    }
  }, [isOpen])

  // A press outside closes the list. On a touch screen the tap that closes it does nothing else, so it never presses
  // what is underneath; the gesture's click is swallowed, unless it turns into a scroll. A fixed backdrop cannot do it
  // here: inside a page, which is a container-query container, it would be anchored to the page content.
  useEffect(() => {
    if (!isOpen) return
    const swallow = (e: MouseEvent) => {
      e.preventDefault()
      e.stopPropagation()
      disarm()
    }
    const disarm = () => {
      document.removeEventListener('click', swallow, true)
      document.removeEventListener('pointercancel', disarm, true)
      document.removeEventListener('pointerdown', disarm, true)
    }
    const onPointer = (e: PointerEvent) => {
      if (containerRef.current?.contains(e.target as Node)) return
      setIsOpen(false)
      if (e.pointerType === 'mouse') return
      document.addEventListener('click', swallow, true)
      document.addEventListener('pointercancel', disarm, true)
      // A tap on something that never clicks: the next press disarms it, before its own click.
      document.addEventListener('pointerdown', disarm, true)
    }
    document.addEventListener('pointerdown', onPointer)
    return () => document.removeEventListener('pointerdown', onPointer)
  }, [isOpen])

  useEffect(() => {
    if (!isOpen) return
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIsOpen(false)
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [isOpen])

  const handleSelect = (optionValue: string) => {
    onChange(optionValue)
    setIsOpen(false)
  }

  // Arrows move through the matches and Enter picks the one marked.
  const onFilterKey = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const step = e.key === 'ArrowDown' ? 1 : -1
      setActive((i) => Math.min(Math.max(i + step, 0), Math.max(shown.length - 1, 0)))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (shown[active]) {
        handleSelect(shown[active].value)
        // The filter goes with the list; the control keeps the keyboard's place.
        triggerRef.current?.focus()
      }
    }
  }
  // A letter typed on the closed control opens the list filtered by it.
  const onTriggerKey = (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (!searchable || disabled || isOpen || e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey || e.key === ' ') return
    e.preventDefault()
    typedRef.current = true
    setQuery(e.key)
    setIsOpen(true)
  }

  return (
    <div ref={containerRef} className={cn('relative', className)}>
      {/* Trigger button */}
      <button
        ref={triggerRef}
        type="button"
        onClick={() => !disabled && setIsOpen(!isOpen)}
        onKeyDown={onTriggerKey}
        className={cn(
          'w-full flex items-center justify-between border transition-colors text-left',
          TRIGGER_SIZES[size],
          'focus:outline-hidden focus:ring-1 focus:ring-ring focus:border-ring',
          disabled
            ? 'bg-surface-raised border-line cursor-not-allowed text-fg-muted'
            : 'bg-surface border-line-strong hover:border-fg-subtle text-fg',
          isOpen && 'ring-1 ring-ring border-ring'
        )}
        disabled={disabled}
        aria-required={required}
        aria-expanded={isOpen}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
      >
        <span className={cn('truncate min-w-0', !selectedOption && 'text-fg-subtle')}>
          {selectedOption ? selectedOption.label : placeholder}
        </span>
        <ChevronDown
          className={cn(
            'text-fg-subtle transition-transform shrink-0',
            size === 'sm' ? 'w-3.5 h-3.5 ml-1' : 'w-4 h-4 ml-2',
            isOpen && 'rotate-180'
          )}
        />
      </button>

      {/* Dropdown menu */}
      {isOpen && (
        <div
          style={placement ?? { top: '100%', marginTop: 4, left: 0, width: '100%' }}
          className="absolute z-50 bg-surface border border-line-strong rounded-lg shadow-lg flex flex-col max-h-72 overflow-hidden"
        >
          {searchable && (
            <div className="flex-none flex items-center gap-2 px-3 border-b border-line">
              <Search className="w-3.5 h-3.5 text-fg-subtle flex-none" />
              {/* 16px on touch screens: iOS zooms the page into any smaller input that takes focus. */}
              <input
                ref={filterRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={onFilterKey}
                placeholder="Type to filter"
                aria-label="Filter the options"
                className="flex-1 min-w-0 h-9 pointer-coarse:h-11 bg-transparent outline-none text-base pointer-fine:text-[13px] placeholder:text-fg-subtle"
              />
            </div>
          )}
          <div ref={listRef} role="listbox" aria-label={ariaLabel} className="flex-1 min-h-0 overflow-auto overscroll-contain max-h-60">
          {shown.length === 0 && <div className="px-3 py-2 text-sm text-fg-muted">No matches</div>}
          {shown.map((option, index) => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === value}
              data-index={index}
              onClick={() => handleSelect(option.value)}
              onMouseMove={() => searchable && setActive(index)}
              className={cn(
                'w-full flex items-start gap-2 px-3 py-2 pointer-coarse:py-3 pointer-coarse:min-h-11 text-left hover:bg-primary-soft/60 transition-colors',
                (option.value === value || (searchable && index === active)) && 'bg-primary-soft/60'
              )}
            >
              <div className="flex-1 min-w-0">
                {/* A long label wraps, a host anywhere, so two alike stay apart; the badge keeps to one line, under a
                    label too long to share one with it. */}
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="min-w-0 text-sm font-medium text-fg wrap-anywhere">
                    {option.label}
                  </span>
                  {option.badge && (
                    <span className="flex-none inline-flex items-center px-1.5 py-0.5 text-xs font-medium rounded-sm bg-surface-raised text-fg-muted whitespace-nowrap">
                      {option.badge}
                    </span>
                  )}
                </div>
                {option.description && (
                  <p className="text-xs text-fg-muted mt-0.5 leading-snug">{option.description}</p>
                )}
              </div>
              {option.value === value && (
                <Check className="w-4 h-4 text-primary shrink-0 mt-0.5" />
              )}
            </button>
          ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default RichSelect
