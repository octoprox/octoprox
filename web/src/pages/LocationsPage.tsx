// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from '@tanstack/react-query'
import { type ExitReputationFilters, type ReputationStatusFilter } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useDropGone, useUrlChoice, useUrlParams, useUrlTab } from '../hooks/useUrlState'
import { useAccuracyRows, useOpenConnector } from '../hooks/useAccuracyRows'
import { Page } from '../components/layout/Page'
import { AdminLink } from '../components/AdminLink'
import { Tabs } from '../components/ui'
import { AccuracyInspector, ProviderAccuracyPanel, accuracyDays, useAccuracyRange } from '../components/geo/ProviderAccuracyPanel'
import { EXIT_IP_FILTERS, ExitIpsPanel } from '../components/geo/ExitIpsPanel'
import { ExitSharingPanel } from '../components/geo/ExitSharingPanel'
import { OBSERVATION_FILTERS, ObservationsPanel } from '../components/geo/ObservationsPanel'
import { ExitReputationPanel } from '../components/geo/ExitReputationPanel'
import { ExitIpReputationInspector, ipReputationQuery } from '../components/geo/ExitIpReputation'
import { PLACE_PRESETS, PlaceInspector, placesQuery } from '../components/geo/SitePlaces'

type Tab = 'accuracy' | 'sharing' | 'exits' | 'observations' | 'reputation'
const TABS: Tab[] = ['accuracy', 'sharing', 'exits', 'observations', 'reputation']
// The tabs IP attribution has too, so its link opens the same one.
const ADMIN_TABS: Tab[] = ['accuracy', 'exits', 'observations']
const STATUSES: ReputationStatusFilter[] = ['any', 'avoided', 'none']
// What belongs to one tab: a tab change drops it. The Reputation tab's site stays, as it always has.
const TAB_PARAMS = ['open', 'place', 'places', 'records_ip', 'records_site', 'records_status', ...EXIT_IP_FILTERS, ...OBSERVATION_FILTERS]
// `place=` for requests naming no place, whose key ('') would read as none open; the traffic split links it so too.
const NO_PLACE = '-'

/**
 * Where this project's proxies really exit: provider accuracy, exit sharing, exit IPs, the observation log and exit
 * reputation, project-scoped. The tab, the inspector open and the filters applied are in the address, so a link
 * opens the same view.
 */
export default function LocationsPage() {
  const { selectedProjectId, selectedProject } = useProject()
  const { isAdmin } = useAuth()
  const [tab, setTab, reveal] = useUrlTab(TABS, 'accuracy')
  const [params, set] = useUrlParams()
  const open = params.get('open')
  // The Reputation tab's site ('' the default one, pinned once its answer names it) and the place of it open.
  const site = params.get('site')?.trim() ?? ''
  const placeParam = params.get('place')
  const place = placeParam === NO_PLACE ? '' : placeParam
  const [preset, setPreset] = useUrlChoice('places', PLACE_PRESETS, 'all')
  const [status] = useUrlChoice('records_status', STATUSES, 'any')
  const records: ExitReputationFilters = {
    ip: params.get('records_ip') ?? undefined,
    site: params.get('records_site') ?? undefined,
    status: status === 'any' ? undefined : status,
  }
  // The filters a link opened Exit IPs or the Observation log with; each panel reads its own once.
  const linked = Object.fromEntries(params)

  // The connector open on the accuracy tab, from the table's own answer for the window shown.
  const [range] = useAccuracyRange()
  const days = accuracyDays(range)
  const openConnector = tab === 'accuracy' ? open : null
  const accuracy = useAccuracyRows(selectedProjectId ?? undefined, days, { enabled: !!selectedProjectId && !!openConnector })
  const openRow = openConnector ? accuracy.rows.find((r) => r.connector_id === openConnector) : undefined
  // Without exits in the window it stays open, so a wider window brings it back; a deleted one is dropped.
  const linkedConnector = useOpenConnector(openConnector, selectedProjectId ? [selectedProjectId] : undefined, !!openConnector && accuracy.settled && !openRow)
  useDropGone('open', linkedConnector.gone, 'That connector no longer exists.')

  // On the Reputation tab an exit IP or a place of the site, one at a time; each checked in the answer its inspector
  // reads, once fresh: a cached one may predate it.
  const openIp = tab === 'reputation' ? open : null
  const ip = useQuery({ ...ipReputationQuery(selectedProjectId ?? '', openIp ?? ''), enabled: !!selectedProjectId && !!openIp })
  useDropGone('open', !!openIp && !!ip.data && !ip.isFetching && ip.data.records.length === 0 && ip.data.connectors.length === 0, 'That exit IP has no records in this project.')
  const openPlace = tab === 'reputation' && !openIp ? place : null
  const places = useQuery({ ...placesQuery(selectedProjectId ?? '', site), enabled: !!selectedProjectId && openPlace !== null })
  useDropGone('place', openPlace !== null && !!places.data && !places.isFetching && !places.data.places.some((p) => p.place === openPlace), "That place is not in this site's data.")

  const pickTab = (next: Tab) => setTab(next, Object.fromEntries(TAB_PARAMS.map((key) => [key, null])))
  const closeOpen = () => set({ open: null })
  // The default site pinned once named keeps its place open; another site picked closes it.
  const pickSite = (value: string) => set(site === '' ? { site: value } : { site: value, place: null })
  const pickRecords = (filters: ExitReputationFilters) => set({ records_ip: filters.ip, records_site: filters.site, records_status: filters.status })

  if (!selectedProjectId) return null
  return (
    <Page
      title="Exit locations"
      subtitle={<>Whether this project's providers put its proxies where they claim, how much their exit IPs overlap, every exit IP seen behind them, and how each exit fares per site.{isAdmin && <> Every project at once is in <AdminLink page="geo" query={ADMIN_TABS.includes(tab) ? `tab=${tab}` : undefined}>IP attribution</AdminLink>.</>}</>}
      panel={openConnector ? (
        <AccuracyInspector row={openRow} loading={!accuracy.settled} name={linkedConnector.name} days={days} onClose={closeOpen} />
      ) : openIp ? (
        <ExitIpReputationInspector projectId={selectedProjectId} ip={openIp} onClose={closeOpen} />
      ) : openPlace !== null ? (
        <PlaceInspector projectId={selectedProjectId} site={site} place={openPlace} onClose={() => set({ place: null })} />
      ) : undefined}
    >
      <Tabs<Tab>
        tabs={[
          { id: 'accuracy', label: 'Provider accuracy' }, { id: 'sharing', label: 'IP sharing' }, { id: 'exits', label: 'Exit IPs' },
          { id: 'observations', label: 'Observation log' }, { id: 'reputation', label: 'Reputation' },
        ]}
        active={tab}
        onChange={pickTab}
        reveal={reveal}
        className="pointer-coarse:*:min-h-11"
      />
      {tab === 'accuracy' && (
        <ProviderAccuracyPanel
          projectId={selectedProjectId}
          onInspect={(row) => set({ open: row.connector_id })}
          inspecting={openConnector}
        />
      )}
      {tab === 'sharing' && <ExitSharingPanel projectId={selectedProjectId} />}
      {tab === 'exits' && <ExitIpsPanel projectId={selectedProjectId} initialFilters={linked} onFilters={set} />}
      {tab === 'observations' && <ObservationsPanel projectId={selectedProjectId} initialFilters={linked} onFilters={set} />}
      {tab === 'reputation' && (
        <ExitReputationPanel
          projectId={selectedProjectId}
          mode={selectedProject?.exit_reputation ?? 'off'}
          inspecting={openIp}
          onInspect={(record) => set({ open: record.ip, place: null })}
          site={site}
          onSite={pickSite}
          preset={preset}
          onPreset={setPreset}
          inspectingPlace={openPlace}
          onInspectPlace={(value) => set({ place: value || NO_PLACE, open: null })}
          records={records}
          onRecords={pickRecords}
        />
      )}
    </Page>
  )
}
