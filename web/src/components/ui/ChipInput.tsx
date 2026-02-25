// Copyright 2025 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef } from 'react'
import { X } from 'lucide-react'
import { Label } from './Label'

interface ChipInputProps {
  label?: string
  values: string[]
  onAdd: (input: string) => void
  onRemove: (index: number) => void
  placeholder?: string
  /** Separators that trigger adding a chip (in addition to Enter). Default: [','] */
  separators?: string[]
}

export function ChipInput({ label, values, onAdd, onRemove, placeholder, separators = [','] }: ChipInputProps) {
  const [inputValue, setInputValue] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || separators.includes(e.key)) {
      e.preventDefault()
      e.stopPropagation()
      if (inputValue.trim()) {
        onAdd(inputValue)
        setInputValue('')
      }
    } else if (e.key === 'Backspace' && !inputValue && values.length > 0) {
      onRemove(values.length - 1)
    }
  }

  const handlePaste = (e: React.ClipboardEvent<HTMLInputElement>) => {
    const text = e.clipboardData.getData('text')
    const separatorPattern = new RegExp(`[\\n${separators.map(s => `\\${s}`).join('')}]`)
    if (separatorPattern.test(text)) {
      e.preventDefault()
      onAdd(text)
      setInputValue('')
    }
  }

  return (
    <div>
      {label && <Label className="text-xs text-gray-600 dark:text-gray-400">{label}</Label>}
      <div
        className="flex flex-wrap gap-1.5 p-2 min-h-[44px] border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 cursor-text focus-within:ring-2 focus-within:ring-blue-500 focus-within:border-blue-500"
        onClick={() => inputRef.current?.focus()}
      >
        {values.map((value, index) => (
          <span
            key={`${value}-${index}`}
            className="inline-flex items-center gap-1 px-2 py-0.5 text-sm bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-200 rounded-md"
          >
            {value}
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onRemove(index) }}
              className="text-blue-500 hover:text-blue-700 dark:text-blue-300 dark:hover:text-blue-100"
            >
              <X className="w-3 h-3" />
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          type="text"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onBlur={() => { if (inputValue.trim()) { onAdd(inputValue); setInputValue('') } }}
          className="flex-1 min-w-[150px] bg-transparent border-none outline-none text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500"
          placeholder={values.length === 0 ? (placeholder ?? 'Type and press Enter...') : ''}
        />
      </div>
    </div>
  )
}
