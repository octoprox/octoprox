// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Button, Input, Segmented } from '../ui'
import { cn } from '../../utils/cn'
import { formatDateTime } from '../../utils/format'

type Choice = 'now' | '1h' | '6h' | '1d' | '3d' | '7d' | 'custom'

const HOUR = 3_600_000
const CHOICES: { value: Choice; label: string }[] = [
  { value: 'now', label: 'Now' },
  { value: '1h', label: '1h ago' },
  { value: '6h', label: '6h ago' },
  { value: '1d', label: '1d ago' },
  { value: '3d', label: '3d ago' },
  { value: '7d', label: '7d ago' },
  { value: 'custom', label: 'Custom' },
]
const BACK_MS: Partial<Record<Choice, number>> = { '1h': HOUR, '6h': 6 * HOUR, '1d': 24 * HOUR, '3d': 72 * HOUR, '7d': 168 * HOUR }

const pad = (n: number) => String(n).padStart(2, '0')

/** A moment as the date field and the time box show it, in the viewer's time. */
function toFields(at: number): { date: string; time: string } {
  const d = new Date(at)
  return { date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`, time: `${pad(d.getHours())}:${pad(d.getMinutes())}` }
}

/**
 * A typed time of day as hours and minutes: "14:30", "14.30", "1430", "9" or
 * "9:05". Empty is the end of the day, so a date alone takes all of it in.
 * Null when the text is not a time.
 */
function parseTimeOfDay(text: string): [number, number] | null {
  const t = text.trim()
  if (!t) return [23, 59]
  const match = /^(\d{1,2})(?:[:.]?(\d{2}))?$/.exec(t)
  if (!match) return null
  const hours = Number(match[1])
  const minutes = match[2] ? Number(match[2]) : 0
  return hours < 24 && minutes < 60 ? [hours, minutes] : null
}

/** The date field and the time box as one moment, through the end of that minute; null while either does not read. */
function fieldsToMoment(date: string, time: string): number | null {
  const hm = parseTimeOfDay(time)
  const [year, month, day] = date.split('-').map(Number)
  if (!hm || !year || !month || !day) return null
  return new Date(year, month - 1, day, hm[0], hm[1], 59, 999).getTime()
}

/**
 * Where the observation log ends: now, a moment some way back, or a date and
 * time of the viewer's choosing.
 *
 * A preset is one deliberate click, so it applies at once, fixed at the moment
 * it was picked so the page's refresh does not slide it. Custom is a date
 * picker and a plain time box (typing "1430" beats stepping through the
 * segments of a native date-time control), and nothing reloads while they are
 * edited: Apply, or Enter in either, sets them. `onChange` hears the applied
 * moment as an ISO time, undefined for now.
 */
export function UntilPicker({ onChange }: { onChange: (until: string | undefined) => void }) {
  const [choice, setChoice] = useState<Choice>('now')
  const [applied, setApplied] = useState<number | null>(null)
  const [date, setDate] = useState('')
  const [time, setTime] = useState('')

  const apply = (at: number | null) => {
    setApplied(at)
    onChange(at === null ? undefined : new Date(at).toISOString())
  }

  const choose = (next: Choice) => {
    setChoice(next)
    if (next === 'custom') {
      // Start from the moment already shown, so a preset can be fine-tuned.
      const fields = toFields(applied ?? Date.now())
      setDate(fields.date)
      setTime(fields.time)
      return
    }
    apply(next === 'now' ? null : Date.now() - (BACK_MS[next] ?? 0))
  }

  const pending = fieldsToMoment(date, time)
  const timeInvalid = parseTimeOfDay(time) === null
  // The fields hold minutes; a preset was fixed to the millisecond, so compare at the minute.
  const sameMinute = pending !== null && applied !== null && Math.floor(pending / 60_000) === Math.floor(applied / 60_000)
  const canApply = pending !== null && !sameMinute
  const applyFields = () => {
    if (!canApply) return
    // Shown as it was read, so "930" becomes 09:30 and an empty time 23:59.
    setTime(toFields(pending).time)
    apply(pending)
  }
  const applyOnEnter = (e: React.KeyboardEvent) => { if (e.key === 'Enter') applyFields() }

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-xs text-fg-muted">Until</span>
      <Segmented options={CHOICES} value={choice} onChange={choose} size="sm" />
      {choice === 'custom' ? (
        <>
          <Input
            type="date"
            value={date}
            max={toFields(Date.now()).date}
            onChange={(e) => setDate(e.target.value)}
            onKeyDown={applyOnEnter}
            className="w-36 py-1 px-2 text-xs"
            aria-label="Date"
          />
          <Input
            value={time}
            onChange={(e) => setTime(e.target.value)}
            onKeyDown={applyOnEnter}
            placeholder="23:59"
            inputMode="numeric"
            aria-invalid={timeInvalid}
            title="24-hour time in your time zone: 14:30, 1430 or 9. Empty is the end of the day."
            className={cn('w-20 py-1 px-2 text-xs tabular-nums', timeInvalid && 'border-danger text-danger')}
            aria-label="Time, 24-hour"
          />
          <Button size="sm" variant={canApply ? 'primary' : 'secondary'} disabled={!canApply} onClick={applyFields} className="h-7">
            Apply
          </Button>
        </>
      ) : (
        applied !== null && <span className="text-xs text-fg-subtle tabular-nums">up to {formatDateTime(new Date(applied).toISOString())}</span>
      )}
    </div>
  )
}
