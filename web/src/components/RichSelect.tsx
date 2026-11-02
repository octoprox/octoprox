// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useEffect, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ChevronDown, Check, Search } from 'lucide-react'
import { cn } from '../utils/cn'

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
}: RichSelectProps) {
  const [isOpen, setIsOpen] = useState(false)
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

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  // Close on escape key
  useEffect(() => {
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIsOpen(false)
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [])

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
          'w-full flex items-center justify-between px-3 py-1.5 text-sm border rounded-lg transition-colors text-left',
          'focus:outline-hidden focus:ring-1 focus:ring-ring focus:border-ring',
          disabled
            ? 'bg-surface-raised border-line cursor-not-allowed text-fg-muted'
            : 'bg-surface border-line-strong hover:border-fg-subtle text-fg',
          isOpen && 'ring-1 ring-ring border-ring'
        )}
        disabled={disabled}
        aria-required={required}
        aria-expanded={isOpen}
      >
        <span className={cn('truncate', !selectedOption && 'text-fg-subtle')}>
          {selectedOption ? selectedOption.label : placeholder}
        </span>
        <ChevronDown
          className={cn(
            'w-4 h-4 text-fg-subtle transition-transform shrink-0 ml-2',
            isOpen && 'rotate-180'
          )}
        />
      </button>

      {/* Dropdown menu */}
      {isOpen && (
        <div className="absolute z-50 w-full mt-1 bg-surface border border-line-strong rounded-lg shadow-lg flex flex-col max-h-72 overflow-hidden">
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
                className="flex-1 min-w-0 h-9 bg-transparent outline-none text-base pointer-fine:text-[13px] placeholder:text-fg-subtle"
              />
            </div>
          )}
          <div ref={listRef} className="flex-1 min-h-0 overflow-auto overscroll-contain max-h-60">
          {shown.length === 0 && <div className="px-3 py-2 text-sm text-fg-muted">No matches</div>}
          {shown.map((option, index) => (
            <button
              key={option.value}
              type="button"
              data-index={index}
              onClick={() => handleSelect(option.value)}
              onMouseMove={() => searchable && setActive(index)}
              className={cn(
                'w-full flex items-start gap-2 px-3 py-2 text-left hover:bg-primary-soft/60 transition-colors',
                (option.value === value || (searchable && index === active)) && 'bg-primary-soft/60'
              )}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-fg truncate">
                    {option.label}
                  </span>
                  {option.badge && (
                    <span className="inline-flex items-center px-1.5 py-0.5 text-xs font-medium rounded-sm bg-surface-raised text-fg-muted">
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
