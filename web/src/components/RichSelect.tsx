// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useEffect } from 'react'
import { ChevronDown, Check } from 'lucide-react'
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
}

export function RichSelect({
  options,
  value,
  onChange,
  placeholder = 'Select an option',
  required = false,
  disabled = false,
  className,
}: RichSelectProps) {
  const [isOpen, setIsOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const selectedOption = options.find((opt) => opt.value === value)

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

  return (
    <div ref={containerRef} className={cn('relative', className)}>
      {/* Trigger button */}
      <button
        type="button"
        onClick={() => !disabled && setIsOpen(!isOpen)}
        className={cn(
          'w-full flex items-center justify-between px-3 py-1.5 text-sm border rounded-lg transition-colors text-left',
          'focus:outline-none focus:ring-1 focus:ring-ring focus:border-ring',
          disabled
            ? 'bg-surface-raised cursor-not-allowed text-fg-muted'
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
            'w-4 h-4 text-fg-subtle transition-transform flex-shrink-0 ml-2',
            isOpen && 'rotate-180'
          )}
        />
      </button>

      {/* Dropdown menu */}
      {isOpen && (
        <div className="absolute z-50 w-full mt-1 bg-surface border border-line-strong rounded-lg shadow-lg max-h-60 overflow-auto">
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => handleSelect(option.value)}
              className={cn(
                'w-full flex items-start gap-2 px-3 py-2 text-left hover:bg-primary-soft/60 transition-colors',
                option.value === value && 'bg-primary-soft/60'
              )}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-fg truncate">
                    {option.label}
                  </span>
                  {option.badge && (
                    <span className="inline-flex items-center px-1.5 py-0.5 text-xs font-medium rounded bg-surface-raised text-fg-muted">
                      {option.badge}
                    </span>
                  )}
                </div>
                {option.description && (
                  <p className="text-xs text-fg-muted truncate mt-0.5">{option.description}</p>
                )}
              </div>
              {option.value === value && (
                <Check className="w-4 h-4 text-primary flex-shrink-0 mt-0.5" />
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default RichSelect
