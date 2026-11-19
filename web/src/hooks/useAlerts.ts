// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useInfiniteQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query'
import {
  ALERT_RANGES, fetchInstallAlerts, fetchProjectAlerts,
  type AlertEntry, type AlertFilters, type AlertPage, type AlertRange,
} from '../api/client'
import { TIME_RANGE_MS, useTimeRange } from '../contexts/TimeRangeContext'
import { parseApiDate } from '../utils/format'

/** How often an open Alerts page reads its rows again: their statuses and last seen change in place. */
const ALERTS_POLL_MS = 60_000

/** What an Alerts page filters by; the window comes from the shared time range and the load's first answer. */
export type AlertListFilters = Omit<AlertFilters, 'since' | 'until'>

/** Where a page after the first starts: below the last alert of the page before, in the window its load fixed. */
interface PageCursor {
  before: string
  until: string
}

/** A time as the API writes them: naive UTC. */
const apiTime = (ms: number): string => new Date(ms).toISOString().replace('Z', '')

export interface AlertList {
  query: ReturnType<typeof useAlertPages>
  /** Every alert loaded, newest first. */
  alerts: AlertEntry[]
  /** The first page: the counts under the filters and how many are newer than the rows. */
  first: AlertPage | undefined
  /** Alerts opened since the rows were loaded; none while a load is on its way. */
  newer: number
  /** Loads from the newest again, the rows staying up, dimmed, until the new ones arrive. */
  showNewest: () => void
}

/**
 * A project's alerts under the filters and the shared time range, a page at a time. The first answer fixes the window's
 * end (`until`): the minutely refresh reads the same window, so loaded rows change in place (status, last seen) and
 * nothing is inserted above them; `newer` counts what opened since, and `showNewest` loads from the newest.
 */
export function useProjectAlerts(projectId: string | null | undefined, filters: AlertListFilters): AlertList {
  return useAlertList(['alerts', projectId], filters, (window, before) => fetchProjectAlerts(projectId!, window, before), !!projectId)
}

/** The install's alerts (admins), held and refreshed as a project's are. */
export function useInstallAlerts(filters: AlertListFilters): AlertList {
  return useAlertList(['install-alerts'], filters, fetchInstallAlerts, true)
}

function useAlertList(
  scope: readonly unknown[],
  filters: AlertListFilters,
  fetchPage: (filters: AlertFilters, before?: string) => Promise<AlertPage>,
  enabled: boolean,
): AlertList {
  const [range] = useTimeRange(ALERT_RANGES)
  // Each load has a key of its own, so a window is never come back to: other filters, another range and "show" each
  // load from the newest.
  const of = JSON.stringify([filters, range])
  const [load, setLoad] = useState({ of, n: 0 })
  if (load.of !== of) setLoad({ of, n: load.n + 1 })
  const query = useAlertPages(scope, filters, range, load.n, fetchPage, enabled)
  const alerts = useMemo(() => query.data?.pages.flatMap((page) => page.alerts) ?? [], [query.data])
  const first = query.data?.pages[0]
  return {
    query,
    alerts,
    first,
    newer: query.isPlaceholderData ? 0 : (first?.newer ?? 0),
    showNewest: () => setLoad((current) => ({ ...current, n: current.n + 1 })),
  }
}

function useAlertPages(
  scope: readonly unknown[],
  filters: AlertListFilters,
  range: AlertRange,
  load: number,
  fetchPage: (filters: AlertFilters, before?: string) => Promise<AlertPage>,
  enabled: boolean,
) {
  const queryClient = useQueryClient()
  return useInfiniteQuery({
    queryKey: [...scope, filters, range, load],
    queryFn: ({ pageParam, queryKey }) => {
      // The window ends where this load's first answer put it, once there is one: a refresh asks for the same rows.
      const until = pageParam?.until ?? queryClient.getQueryData<InfiniteData<AlertPage>>(queryKey)?.pages[0]?.until
      const end = parseApiDate(until)?.getTime() ?? Date.now()
      return fetchPage({ ...filters, since: apiTime(end - TIME_RANGE_MS[range]), until }, pageParam?.before)
    },
    initialPageParam: null as PageCursor | null,
    getNextPageParam: (last, pages): PageCursor | null =>
      last.more && last.alerts.length > 0 ? { before: last.alerts[last.alerts.length - 1].id, until: pages[0].until } : null,
    enabled,
    refetchInterval: ALERTS_POLL_MS,
    // Another load keeps the rows up, dimmed, until its own arrive.
    placeholderData: (previous) => previous,
  })
}
