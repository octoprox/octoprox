// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useToast } from '../contexts/ToastContext'
import { cn } from '../utils/cn'
import { HistoryButton } from './audit/HistoryButton'
import { Button, Card, CardHeader, Input, LogoLoader, QueryContent } from './ui'

// The most days the api keeps records for, short of forever (0).
export const MAX_RETENTION_DAYS = 36_500

/** How many days the api keeps some records, 0 for ever. */
export interface Retention {
  days: number
}

interface RetentionCardProps {
  queryKey: string
  fetch: () => Promise<Retention>
  update: (days: number) => Promise<Retention>
  /** The words before the field, such as "Keep entries for". */
  label: string
  inputId: string
  /** What the toast says is kept, such as "Audit entries". */
  noun: string
}

/** An install-wide retention an admin sets, in days, with its History; the hourly compaction deletes older records. */
export function RetentionCard({ queryKey, fetch, update, label, inputId, noun }: RetentionCardProps) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const retentionQuery = useQuery({ queryKey: [queryKey], queryFn: fetch })
  const save = useMutation({
    mutationFn: update,
    onSuccess: (saved) => {
      queryClient.setQueryData([queryKey], saved)
      toast.show(saved.days === 0 ? `${noun} are kept forever` : `${noun} are kept for ${saved.days.toLocaleString()} days`)
    },
    onError: (e: Error) => toast.show(e.message || 'Failed to save the retention', 'error'),
  })
  return (
    <Card className="p-4 @2xl:p-5">
      <CardHeader title="Retention" action={<HistoryButton entityType="system_settings" entityId="1" />} />
      <QueryContent query={retentionQuery} what="the retention" loader={<LogoLoader className="py-4" />}>
        {/* Keyed by the stored value, so a save or another admin's starts the field afresh. */}
        {(retention) => (
          <RetentionForm
            key={retention.days}
            retention={retention}
            label={label}
            inputId={inputId}
            saving={save.isPending}
            onSave={(days) => save.mutate(days)}
          />
        )}
      </QueryContent>
    </Card>
  )
}

function RetentionForm({ retention, label, inputId, saving, onSave }: {
  retention: Retention
  label: string
  inputId: string
  saving: boolean
  onSave: (days: number) => void
}) {
  const [text, setText] = useState(String(retention.days))
  const days = /^\d+$/.test(text.trim()) ? Number(text.trim()) : NaN
  const valid = days >= 0 && days <= MAX_RETENTION_DAYS
  return (
    <form className="mt-1 space-y-1.5" onSubmit={(e) => { e.preventDefault(); if (valid) onSave(days) }}>
      {/* The words stay together, and the field with its unit and Save, so a narrow card wraps between the two. */}
      <div className="flex items-center gap-x-2 gap-y-1 flex-wrap text-sm text-fg">
        <label htmlFor={inputId} className="whitespace-nowrap">{label}</label>
        <span className="inline-flex items-center gap-2">
          <Input
            id={inputId}
            type="number"
            inputMode="numeric"
            min={0}
            max={MAX_RETENTION_DAYS}
            step={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            aria-invalid={!valid}
            className={cn('w-24 px-3 py-1.5 text-base pointer-fine:text-[13px] pointer-coarse:h-11 tabular-nums', !valid && 'border-danger text-danger')}
          />
          days
          <Button type="submit" size="sm" disabled={!valid || days === retention.days || saving} className="ml-1 pointer-coarse:h-11">
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </span>
      </div>
      <p className="text-[11px] text-fg-subtle">
        {valid ? '0 keeps them forever; the hourly compaction deletes older ones.' : `A whole number of days, from 0 to ${MAX_RETENTION_DAYS.toLocaleString()}.`}
      </p>
    </form>
  )
}
