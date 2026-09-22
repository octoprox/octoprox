// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useProject } from '../contexts/ProjectContext'
import { Page } from '../components/layout/Page'
import { Tabs } from '../components/ui'
import { ProviderAccuracyPanel } from '../components/geo/ProviderAccuracyPanel'
import { ExitIpsPanel } from '../components/geo/ExitIpsPanel'
import { ObservationsPanel } from '../components/geo/ObservationsPanel'

type Tab = 'accuracy' | 'exits' | 'observations'

/** Where this project's proxies really exit: provider accuracy and the observation log, project-scoped. */
export default function LocationsPage() {
  const { selectedProjectId } = useProject()
  const [tab, setTab] = useState<Tab>('accuracy')
  if (!selectedProjectId) return null
  return (
    <Page
      title="Exit locations"
      subtitle="Whether this project's providers put its proxies where they claim, and every exit IP seen behind them."
    >
      <Tabs<Tab>
        tabs={[{ id: 'accuracy', label: 'Provider accuracy' }, { id: 'exits', label: 'Exit IPs' }, { id: 'observations', label: 'Observation log' }]}
        active={tab}
        onChange={setTab}
      />
      {tab === 'accuracy' && <ProviderAccuracyPanel projectId={selectedProjectId} />}
      {tab === 'exits' && <ExitIpsPanel projectId={selectedProjectId} />}
      {tab === 'observations' && <ObservationsPanel projectId={selectedProjectId} />}
    </Page>
  )
}
