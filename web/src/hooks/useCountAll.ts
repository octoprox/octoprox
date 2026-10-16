// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { ManualPagination } from '../components/DataTable'

/**
 * A server-paged table's total, made exact on request.
 *
 * The server counts a page's total only so far, so `total_exact` false makes
 * it a lower bound. The exact count is asked for and never refreshed: under
 * some filters it reads the whole table. It holds for the filters it was asked
 * under, so changing them never sets off another. A page that counted to the
 * end itself is fresher than an earlier count and wins, and so does a lower
 * bound the count has fallen behind.
 */
export function useCountAll<F>(
  name: string,
  filters: F,
  count: (filters: F) => Promise<number>,
  page: { total: number; total_exact: boolean } | undefined,
  fallbackTotal: number,
): Pick<ManualPagination, 'total' | 'totalExact' | 'countAll'> {
  const key = JSON.stringify(filters)
  const [askedFor, setAskedFor] = useState<string | null>(null)
  const asked = askedFor === key
  const counted = useQuery({
    queryKey: [name, filters],
    queryFn: () => count(filters),
    enabled: asked,
    staleTime: Infinity,
    retry: false,
  })
  const pageTotal = page?.total ?? fallbackTotal
  const pageExact = page?.total_exact ?? true
  const exact = asked && !pageExact && counted.data !== undefined && counted.data >= pageTotal ? counted.data : undefined
  return {
    total: exact ?? pageTotal,
    totalExact: pageExact || exact !== undefined,
    countAll: {
      onCount: () => { if (asked) void counted.refetch(); else setAskedFor(key) },
      counting: counted.isFetching,
      error: counted.error?.message,
    },
  }
}
