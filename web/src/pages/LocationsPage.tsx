// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchGeoExits } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { Page } from '../components/layout/Page'
import { Tabs } from '../components/ui'
import { ACCURACY_RANGES, AccuracyRow, ProviderAccuracyPanel } from '../components/geo/ProviderAccuracyPanel'
import { AccuracyDetailsInspector } from '../components/geo/AccuracyDetails'
import { ExitIpsPanel } from '../components/geo/ExitIpsPanel'
import { ObservationsPanel } from '../components/geo/ObservationsPanel'

type Tab = 'accuracy' | 'exits' | 'observations'
type Range = (typeof ACCURACY_RANGES)[number]['value']

/** Where this project's proxies really exit: provider accuracy and the observation log, project-scoped. */
export default function LocationsPage() {
  const { selectedProjectId } = useProject()
  const [tab, setTab] = useState<Tab>('accuracy')
  const [range, setRange] = useState<Range>('30d')
  const [inspecting, setInspecting] = useState<AccuracyRow | null>(null)
  const days = ACCURACY_RANGES.find((r) => r.value === range)?.days ?? 30
  const { data: exits } = useQuery({
    queryKey: ['geo-exits', selectedProjectId ?? 'all', days],
    queryFn: () => fetchGeoExits({ days, project_id: selectedProjectId ?? undefined }),
    enabled: !!selectedProjectId && !!inspecting,
  })
  if (!selectedProjectId) return null
  return (
    <Page
      title="Exit locations"
      subtitle="Whether this project's providers put its proxies where they claim, and every exit IP seen behind them."
      panel={inspecting && tab === 'accuracy' ? (
        <AccuracyDetailsInspector
          row={inspecting}
          exits={exits?.connectors.find((c) => c.connector_id === inspecting.connector_id) ?? inspecting.exit_stats}
          days={days}
          onClose={() => setInspecting(null)}
        />
      ) : undefined}
    >
      <Tabs<Tab>
        tabs={[{ id: 'accuracy', label: 'Provider accuracy' }, { id: 'exits', label: 'Exit IPs' }, { id: 'observations', label: 'Observation log' }]}
        active={tab}
        onChange={setTab}
      />
      {tab === 'accuracy' && (
        <ProviderAccuracyPanel
          projectId={selectedProjectId}
          range={range}
          onRangeChange={setRange}
          onInspect={setInspecting}
          inspecting={inspecting?.connector_id ?? null}
        />
      )}
      {tab === 'exits' && <ExitIpsPanel projectId={selectedProjectId} />}
      {tab === 'observations' && <ObservationsPanel projectId={selectedProjectId} />}
    </Page>
  )
}
