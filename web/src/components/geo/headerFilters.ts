// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import type { ColumnFiltersState } from '@tanstack/react-table'
import { useToast } from '../../contexts/ToastContext'

/** A server-filtered table's applied header filters by column id, trimmed, the empty ones left out. */
export type FilterValues = Partial<Record<string, string>>

/** The filters a link carries among `keys` (column ids), as the table's header filters; a value its select does not offer (`choices`) is left out. */
export function linkedFilters(
  linked: Record<string, string> | undefined,
  keys: readonly string[],
  choices: Partial<Record<string, readonly string[]>> = {},
): ColumnFiltersState {
  return keys.flatMap((id) => {
    const value = linked?.[id]?.trim()
    return value && (choices[id]?.includes(value) ?? true) ? [{ id, value }] : []
  })
}

/**
 * Drops a link's connector filter, with the toast a dead link gets, once `options` (null until every list they come
 * from has answered, fresh) lack it. Only the link's own value is checked: one picked later comes from the options.
 */
export function useDropGoneConnector(linked: string | undefined, options: { value: string }[] | null, drop: () => void): void {
  const toast = useToast()
  const [pending, setPending] = useState(linked)
  useEffect(() => {
    if (!pending || !options) return
    setPending(undefined)
    if (options.some((o) => o.value === pending)) return
    drop()
    toast.show('That connector no longer exists.', 'error')
  }, [pending, options])
}

/** The header filters as typed, as the values they apply. */
export function filterValues(columnFilters: ColumnFiltersState): FilterValues {
  return Object.fromEntries(columnFilters.flatMap(({ id, value }) => (typeof value === 'string' && value.trim() ? [[id, value.trim()]] : [])))
}

/** Applied values as an address patch: each of `keys` set, or null to drop it. */
export function filterPatch(values: FilterValues, keys: readonly string[]): Record<string, string | null> {
  return Object.fromEntries(keys.map((key) => [key, values[key] ?? null]))
}
