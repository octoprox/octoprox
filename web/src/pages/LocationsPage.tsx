// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchGeoExits, type ExitReputationRecord } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { Page } from '../components/layout/Page'
import { AdminLink } from '../components/AdminLink'
import { Tabs } from '../components/ui'
import { AccuracyRow, ProviderAccuracyPanel, accuracyDays, useAccuracyRange } from '../components/geo/ProviderAccuracyPanel'
import { AccuracyDetailsInspector } from '../components/geo/AccuracyDetails'
import { ExitIpsPanel } from '../components/geo/ExitIpsPanel'
import { ExitSharingPanel } from '../components/geo/ExitSharingPanel'
import { ObservationsPanel } from '../components/geo/ObservationsPanel'
import { ExitReputationPanel, recordId } from '../components/geo/ExitReputationPanel'
import { ExitIpReputationInspector } from '../components/geo/ExitIpReputation'

type Tab = 'accuracy' | 'sharing' | 'exits' | 'observations' | 'reputation'

/** Where this project's proxies really exit: provider accuracy, exit sharing, exit IPs, the observation log and exit reputation, project-scoped. */
export default function LocationsPage() {
  const { selectedProjectId, selectedProject } = useProject()
  const { isAdmin } = useAuth()
  const [tab, setTab] = useState<Tab>('accuracy')
  const [range] = useAccuracyRange()
  const [inspecting, setInspecting] = useState<AccuracyRow | null>(null)
  // The reputation row whose IP is open: the IP's every site shows, the row stays marked.
  const [inspectingRecord, setInspectingRecord] = useState<ExitReputationRecord | null>(null)
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
      subtitle={<>Whether this project's providers put its proxies where they claim, how much their exit IPs overlap, every exit IP seen behind them, and how each exit fares per site.{isAdmin && <> Every project at once is in <AdminLink page="geo">IP attribution</AdminLink>.</>}</>}
      panel={inspecting && tab === 'accuracy' ? (
        <AccuracyDetailsInspector
          row={inspecting}
          exits={exits?.connectors.find((c) => c.connector_id === inspecting.connector_id) ?? inspecting.exit_stats}
          days={days}
          onClose={() => setInspecting(null)}
        />
      ) : inspectingRecord && tab === 'reputation' ? (
        <ExitIpReputationInspector projectId={selectedProjectId} ip={inspectingRecord.ip} onClose={() => setInspectingRecord(null)} />
      ) : undefined}
    >
      <Tabs<Tab>
        tabs={[
          { id: 'accuracy', label: 'Provider accuracy' }, { id: 'sharing', label: 'IP sharing' }, { id: 'exits', label: 'Exit IPs' },
          { id: 'observations', label: 'Observation log' }, { id: 'reputation', label: 'Reputation' },
        ]}
        active={tab}
        onChange={setTab}
        className="pointer-coarse:*:min-h-11"
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
      {tab === 'reputation' && (
        <ExitReputationPanel
          projectId={selectedProjectId}
          mode={selectedProject?.exit_reputation ?? 'off'}
          inspecting={inspectingRecord ? recordId(inspectingRecord) : null}
          onInspect={setInspectingRecord}
        />
      )}
    </Page>
  )
}
