// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Suspense, lazy, useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchProjectConnectors, fetchProjectProxies } from '../api/client'
import { isDynamic } from '../utils/connectors'
import type { CountryStat, DynamicCoverage } from './WorldMap'

// The map data is ~50 KB gzipped; load it only when the Overview renders it.
const WorldMap = lazy(() => import('./WorldMap'))

export interface LocationSummary {
  /** Proxies with a known exit country. */
  located: number
  /** Proxies whose exit country is not known. Dynamic gateway rows are not counted: they have none by design. */
  unknown: number
  /** Countries with proxies or named by a dynamic connector's allow-list. */
  countries: number
  /** At least one enabled dynamic connector has no allow-list, so every country is reachable. */
  anyCountry: boolean
}

/**
 * Where the project's proxies exit from, across every connector, aggregated by country.
 * A dynamic-sessions connector holds one gateway row with no exit of its own (the vendor
 * picks one per request), so it contributes the countries it allows instead: its allow-list,
 * or the whole world when it has none.
 */
export function ProjectLocations({ projectId, onSummary }: { projectId: string; onSummary?: (summary: LocationSummary) => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ['proxies', projectId],
    queryFn: () => fetchProjectProxies(projectId),
    enabled: !!projectId,
    refetchInterval: 10000, // statuses and counters move
  })
  // Same key as the Overview's connector list, so this is served from its cache.
  const { data: connectorsData, isLoading: connectorsLoading } = useQuery({
    queryKey: ['connectors', projectId],
    queryFn: () => fetchProjectConnectors(projectId),
    enabled: !!projectId,
    refetchInterval: 15000,
  })

  const { stats, dynamic, unknown, total, countries, anyCountry, disabledDynamic } = useMemo(() => {
    const stats: Record<string, CountryStat> = {}
    const connectorsByCountry: Record<string, Set<string>> = {}
    const dynamic: DynamicCoverage = { countries: {}, worldwide: 0 }
    const gateways = new Set<string>()
    let disabledDynamic = 0
    for (const c of connectorsData?.connectors ?? []) {
      if (!isDynamic(c)) continue
      gateways.add(c.id)
      // A disabled dynamic connector reaches nowhere, so it adds no coverage. This differs from
      // fixed proxies of a disabled connector, which stay on the map as unhealthy: those are real
      // rows still sitting in the pool, while dynamic coverage is only a promise of what a request could get.
      if (!c.enabled) { disabledDynamic += 1; continue }
      const allowed = c.target?.countries ?? []
      if (allowed.length === 0) dynamic.worldwide += 1
      for (const code of allowed) {
        const key = code.toUpperCase()
        dynamic.countries[key] = (dynamic.countries[key] ?? 0) + 1
      }
    }
    let unknown = 0
    let total = 0
    for (const proxy of data?.proxies ?? []) {
      // The gateway row of a dynamic connector is not a proxy in one place; its coverage came from the connector.
      if (gateways.has(proxy.connector_id)) continue
      total += 1
      const code = proxy.country?.toUpperCase()
      if (!code) { unknown += 1; continue }
      const entry = (stats[code] ??= { total: 0, healthy: 0, connectors: 0 })
      entry.total += 1
      if (proxy.status === 'healthy' && !proxy.quarantined && proxy.connector_enabled) entry.healthy += 1
      ;(connectorsByCountry[code] ??= new Set()).add(proxy.connector_id)
    }
    for (const [code, set] of Object.entries(connectorsByCountry)) stats[code].connectors = set.size
    const countries = new Set([...Object.keys(stats), ...Object.keys(dynamic.countries)]).size
    return { stats, dynamic, unknown, total, countries, anyCountry: dynamic.worldwide > 0, disabledDynamic }
  }, [data, connectorsData])

  const covered = anyCountry || countries > 0
  useEffect(() => {
    onSummary?.({ located: total - unknown, unknown, countries, anyCountry })
  }, [onSummary, total, unknown, countries, anyCountry])

  if ((isLoading && !data) || (connectorsLoading && !connectorsData)) return <div className="h-full min-h-40 rounded-lg bg-surface-raised animate-pulse" />
  if (total === 0 && !covered) {
    return (
      <p className="text-xs text-fg-muted py-3">
        {disabledDynamic > 0
          ? `${disabledDynamic === 1 ? 'The dynamic connector is' : `All ${disabledDynamic} dynamic connectors are`} disabled, so no location is available.`
          : 'No proxies provisioned yet.'}
      </p>
    )
  }
  if (!covered) {
    return <p className="text-xs text-fg-muted py-3">The exit location of {total === 1 ? 'the proxy' : `the ${total} proxies`} is not known yet.</p>
  }
  return (
    <Suspense fallback={<div className="h-full min-h-40 rounded-lg bg-surface-raised animate-pulse" />}>
      <WorldMap stats={stats} dynamic={dynamic} unknown={unknown} className="h-full" />
    </Suspense>
  )
}
