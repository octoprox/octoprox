// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from 'react'
import { Connector, HISTORY_RANGES } from '../api/client'
import { useTimeRange } from '../contexts/TimeRangeContext'
import { useConnectorHistory } from './ConnectorTraffic'
import { LatencyChart } from './charts/LatencyChart'
import { InspectorSection, Segmented } from './ui'

/** Inspector section: the connector's latency over the range, by step, to tell its vendor's time from Octoprox's. */
export function ConnectorLatencySection({ connector, projectId }: { connector: Connector; projectId: string }) {
  const [range, setRange] = useTimeRange(HISTORY_RANGES)
  // The traffic section reads the same history, so the two share one request.
  const { data: history } = useConnectorHistory(projectId, connector.id, range)
  const snapshots = useMemo(() => history?.snapshots ?? [], [history])
  const average = useMemo(() => {
    let sum = 0, successes = 0
    for (const s of snapshots) {
      if (s.avg_latency_ms == null) continue
      sum += s.avg_latency_ms * s.success_count
      successes += s.success_count
    }
    return successes > 0 ? sum / successes : null
  }, [snapshots])

  return (
    <InspectorSection
      title="Latency"
      action={<Segmented options={HISTORY_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} size="sm" />}
    >
      <p className="text-xs text-fg-muted tabular-nums">
        {average == null ? `No successful requests over ${range}` : <>Average <span className="text-fg font-medium">{Math.round(average)} ms</span> over the successful requests of {range}, until the tunnel was up or the response started</>}
      </p>
      <LatencyChart snapshots={snapshots} range={range} height={110} compact />
    </InspectorSection>
  )
}
