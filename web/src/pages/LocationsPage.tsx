// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router'
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
import { PlaceInspector, type InspectedPlace } from '../components/geo/SitePlaces'

type Tab = 'accuracy' | 'sharing' | 'exits' | 'observations' | 'reputation'
const TABS: Tab[] = ['accuracy', 'sharing', 'exits', 'observations', 'reputation']

/** Where this project's proxies really exit: provider accuracy, exit sharing, exit IPs, the observation log and exit reputation, project-scoped. */
export default function LocationsPage() {
  const { selectedProjectId, selectedProject } = useProject()
  const { isAdmin } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [tab, setTab] = useState<Tab>('accuracy')
  // A link's request for a tab, so a phone's tab strip scrolls to it.
  const [tabRequest, setTabRequest] = useState<object | null>(null)
  const [range] = useAccuracyRange()
  const [inspecting, setInspecting] = useState<AccuracyRow | null>(null)
  // The reputation row whose IP is open: the IP's every site shows, the row stays marked.
  const [inspectingRecord, setInspectingRecord] = useState<ExitReputationRecord | null>(null)
  // The Reputation tab's site ('' the default one) and the place of it open in the side panel; one panel at a time.
  const [site, setSite] = useState('')
  const [inspectingPlace, setInspectingPlace] = useState<InspectedPlace | null>(null)
  const inspectRecord = (record: ExitReputationRecord) => {
    setInspectingRecord(record)
    setInspectingPlace(null)
  }
  const inspectPlace = (place: InspectedPlace) => {
    setInspectingPlace(place)
    setInspectingRecord(null)
  }
  // A place open for the very site picked stays open (the default site, pinned once named); another site's closes.
  const pickSite = (value: string) => {
    setSite(value)
    setInspectingPlace((open) => (open && open.site === value ? { ...open, key: value } : null))
  }

  // Deep link (a Needs attention line): ?tab=reputation&site=<site>, read once and dropped.
  useEffect(() => {
    const wanted = searchParams.get('tab')
    const linkedSite = searchParams.get('site')
    if (!wanted && linkedSite === null) return
    if (TABS.includes(wanted as Tab)) {
      setTab(wanted as Tab)
      setTabRequest({})
    }
    if (linkedSite !== null) pickSite(linkedSite.trim())
    searchParams.delete('tab')
    searchParams.delete('site')
    setSearchParams(searchParams, { replace: true })
  }, [searchParams])
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
      ) : inspectingPlace && tab === 'reputation' ? (
        <PlaceInspector projectId={selectedProjectId} inspected={inspectingPlace} onClose={() => setInspectingPlace(null)} />
      ) : undefined}
    >
      <Tabs<Tab>
        tabs={[
          { id: 'accuracy', label: 'Provider accuracy' }, { id: 'sharing', label: 'IP sharing' }, { id: 'exits', label: 'Exit IPs' },
          { id: 'observations', label: 'Observation log' }, { id: 'reputation', label: 'Reputation' },
        ]}
        active={tab}
        onChange={setTab}
        reveal={tabRequest}
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
          onInspect={inspectRecord}
          site={site}
          onSite={pickSite}
          inspectingPlace={inspectingPlace}
          onInspectPlace={inspectPlace}
        />
      )}
    </Page>
  )
}
