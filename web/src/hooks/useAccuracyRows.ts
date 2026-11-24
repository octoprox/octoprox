// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { fetchGeoAccuracy, fetchGeoExits, fetchProjectConnectors, fetchProjects, type ConnectorAccuracy, type ConnectorExits, type LevelAccuracy } from '../api/client'

export type AccuracyRow = ConnectorAccuracy & { exit_stats?: ConnectorExits; project_name?: string | null }

const EMPTY_LEVEL: LevelAccuracy = { claimed: 0, confirmed: 0, contradicted: 0, open: 0, accuracy: null }

/**
 * Per-connector vendor accuracy over `days`, each connector's exits merged in: the accuracy table's rows, and the row
 * an inspector opened by connector id reads, from the same two answers. `scope` is a project id, or undefined for
 * every project; `named` adds each row's project name.
 */
export function useAccuracyRows(scope: string | undefined, days: number, { named = false, enabled = true } = {}) {
  // No timed refetch: each answer reads the connectors' whole exit history, and the numbers move by the day.
  // Another window or project keeps the last answer on screen until its own arrives, so the table keeps its place.
  const accuracy = useQuery({
    queryKey: ['geo-accuracy', scope ?? 'all', days],
    queryFn: () => fetchGeoAccuracy({ days, project_id: scope }),
    placeholderData: (previous) => previous,
    enabled,
  })
  const exits = useQuery({
    queryKey: ['geo-exits', scope ?? 'all', days],
    queryFn: () => fetchGeoExits({ days, project_id: scope }),
    placeholderData: (previous) => previous,
    enabled,
  })
  // A row merges both answers, so the rows wait for both: exits alone would read as "no claims".
  const isLoading = accuracy.isLoading || exits.isLoading
  const { data: projects } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects, enabled: enabled && named })
  const projectNames = useMemo(() => new Map((projects?.projects ?? []).map((p) => [p.id, p.name])), [projects])
  const data = accuracy.data
  const exitData = exits.data

  const rows: AccuracyRow[] = useMemo(() => {
    if (isLoading) return []
    const byConnector = new Map((exitData?.connectors ?? []).map((e) => [e.connector_id, e]))
    const seen = new Set<string>()
    const merged: AccuracyRow[] = (data?.connectors ?? []).map((c) => {
      seen.add(c.connector_id)
      return { ...c, exit_stats: byConnector.get(c.connector_id), project_name: c.project_id ? projectNames.get(c.project_id) ?? null : null }
    })
    // Connectors with exits but no vendor claims still deserve a row.
    for (const e of exitData?.connectors ?? []) {
      if (!seen.has(e.connector_id)) {
        merged.push({
          connector_id: e.connector_id, connector_name: e.connector_name, project_id: e.project_id,
          project_name: e.project_id ? projectNames.get(e.project_id) ?? null : null,
          exits: e.unique_in_window, country: EMPTY_LEVEL, state: EMPTY_LEVEL, city: EMPTY_LEVEL,
          breakdown: [], coverage: e.coverage, exit_stats: e,
        })
      }
    }
    return merged
  }, [isLoading, data, exitData, projectNames])

  return {
    rows,
    isLoading,
    reloading: accuracy.isPlaceholderData || exits.isPlaceholderData,
    /** Both answers are in, fresh, for this scope and window, so a connector missing from the rows has none in it. */
    settled: accuracy.isSuccess && exits.isSuccess && !accuracy.isPlaceholderData && !exits.isPlaceholderData
      && !accuracy.isFetching && !exits.isFetching,
  }
}

/**
 * A connector opened by link that the window's rows lack: its name while it still exists (it has no exits in the
 * window), or `gone` once every project it could be in has answered without it. Asked only while `ask`.
 */
export function useOpenConnector(connectorId: string | null, projectIds: string[] | undefined, ask: boolean) {
  const lists = useQueries({
    queries: (ask && connectorId ? projectIds ?? [] : []).map((id) => ({ queryKey: ['connectors', id], queryFn: () => fetchProjectConnectors(id) })),
  })
  const found = lists.flatMap((q) => q.data?.connectors ?? []).find((c) => c.id === connectorId)
  // Fresh lists only: a cached one may predate the connector.
  const answered = ask && !!connectorId && projectIds !== undefined && lists.every((q) => q.isSuccess && !q.isFetching)
  return { name: found?.name, gone: answered && !found }
}
