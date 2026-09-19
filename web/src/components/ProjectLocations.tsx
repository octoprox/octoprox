// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Suspense, lazy, useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchProjectProxies } from '../api/client'
import type { CountryStat } from './WorldMap'

// The map data is ~50 KB gzipped; load it only when the Overview renders it.
const WorldMap = lazy(() => import('./WorldMap'))

export interface LocationSummary {
  /** Proxies with a known exit country. */
  located: number
  /** Proxies whose exit country is not known. */
  unknown: number
  countries: number
}

/** Where the project's proxies exit from, across every connector, aggregated by country. */
export function ProjectLocations({ projectId, onSummary }: { projectId: string; onSummary?: (summary: LocationSummary) => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ['proxies', projectId],
    queryFn: () => fetchProjectProxies(projectId),
    enabled: !!projectId,
    refetchInterval: 10000, // statuses and counters move
  })

  const { stats, unknown, total } = useMemo(() => {
    const stats: Record<string, CountryStat> = {}
    const connectorsByCountry: Record<string, Set<string>> = {}
    let unknown = 0
    let total = 0
    for (const proxy of data?.proxies ?? []) {
      total += 1
      const code = proxy.country?.toUpperCase()
      if (!code) { unknown += 1; continue }
      const entry = (stats[code] ??= { total: 0, healthy: 0, connectors: 0 })
      entry.total += 1
      if (proxy.status === 'healthy' && !proxy.quarantined && proxy.connector_enabled) entry.healthy += 1
      ;(connectorsByCountry[code] ??= new Set()).add(proxy.connector_id)
    }
    for (const [code, set] of Object.entries(connectorsByCountry)) stats[code].connectors = set.size
    return { stats, unknown, total }
  }, [data])

  const countries = Object.keys(stats).length
  useEffect(() => {
    onSummary?.({ located: total - unknown, unknown, countries })
  }, [onSummary, total, unknown, countries])

  if (isLoading && !data) return <div className="h-full min-h-40 rounded-lg bg-surface-raised animate-pulse" />
  if (total === 0) return <p className="text-xs text-fg-muted py-3">No proxies provisioned yet.</p>
  if (countries === 0) {
    return <p className="text-xs text-fg-muted py-3">The exit location of {total === 1 ? 'the proxy' : `the ${total} proxies`} is not known yet.</p>
  }
  return (
    <Suspense fallback={<div className="h-full min-h-40 rounded-lg bg-surface-raised animate-pulse" />}>
      <WorldMap stats={stats} unknown={unknown} className="h-full" />
    </Suspense>
  )
}
