// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchGeoExits } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { Page } from '../components/layout/Page'
import { Tabs } from '../components/ui'
import { AccuracyRow, ProviderAccuracyPanel, accuracyDays, useAccuracyRange } from '../components/geo/ProviderAccuracyPanel'
import { AccuracyDetailsInspector } from '../components/geo/AccuracyDetails'
import { ExitIpsPanel } from '../components/geo/ExitIpsPanel'
import { ExitSharingPanel } from '../components/geo/ExitSharingPanel'
import { ObservationsPanel } from '../components/geo/ObservationsPanel'

type Tab = 'accuracy' | 'sharing' | 'exits' | 'observations'

/** Where this project's proxies really exit: provider accuracy, exit sharing, exit IPs and the observation log, project-scoped. */
export default function LocationsPage() {
  const { selectedProjectId } = useProject()
  const [tab, setTab] = useState<Tab>('accuracy')
  const [range] = useAccuracyRange()
  const [inspecting, setInspecting] = useState<AccuracyRow | null>(null)
  const days = accuracyDays(range)
  const { data: exits } = useQuery({
    queryKey: ['geo-exits', selectedProjectId ?? 'all', days],
    queryFn: () => fetchGeoExits({ days, project_id: selectedProjectId ?? undefined }),
    enabled: !!selectedProjectId && !!inspecting,
  })
  if (!selectedProjectId) return null
  return (
    <Page
      title="Exit locations"
      subtitle="Whether this project's providers put its proxies where they claim, how much their exit IPs overlap, and every exit IP seen behind them."
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
        tabs={[{ id: 'accuracy', label: 'Provider accuracy' }, { id: 'sharing', label: 'IP sharing' }, { id: 'exits', label: 'Exit IPs' }, { id: 'observations', label: 'Observation log' }]}
        active={tab}
        onChange={setTab}
      />
      {tab === 'accuracy' && (
        <ProviderAccuracyPanel
          projectId={selectedProjectId}
          onInspect={setInspecting}
          inspecting={inspecting?.connector_id ?? null}
        />
      )}
      {tab === 'sharing' && <ExitSharingPanel projectId={selectedProjectId} />}
      {tab === 'exits' && <ExitIpsPanel projectId={selectedProjectId} />}
      {tab === 'observations' && <ObservationsPanel projectId={selectedProjectId} />}
    </Page>
  )
}
