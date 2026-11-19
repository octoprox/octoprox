// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { ALERT_RANGES, fetchInstallAlertRetention, updateInstallAlertRetention } from '../../api/client'
import { AlertPanel } from '../../components/alerts/AlertPanel'
import { AlertsTable } from '../../components/alerts/AlertsTable'
import { INSTALL_KINDS } from '../../components/alerts/kinds'
import { Page } from '../../components/layout/Page'
import { RetentionCard } from '../../components/RetentionCard'
import { Segmented } from '../../components/ui'
import { useTimeRange } from '../../contexts/TimeRangeContext'
import { useInstallAlerts, type AlertListFilters } from '../../hooks/useAlerts'

/**
 * Administration, Alerts: the install-wide lines of the Projects page over time (gateways, background work, IP
 * databases), open and resolved, and how long resolved ones are kept.
 */
export default function AlertsSection() {
  const [range, setRange] = useTimeRange(ALERT_RANGES)
  const [filters, setFilters] = useState<AlertListFilters>({ status: 'all' })
  const [openId, setOpenId] = useState<string | null>(null)
  const list = useInstallAlerts(filters)
  const shown = openId ? list.alerts.find((alert) => alert.id === openId) : undefined

  return (
    <Page
      title="Alerts"
      subtitle="Install-wide problems over time: gateways, background work, IP databases."
      actions={(
        <div className="pointer-coarse:[&_button]:min-h-11">
          <Segmented options={ALERT_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} size="sm" />
        </div>
      )}
      panel={shown ? <AlertPanel key={shown.id} alert={shown} projectId={null} onClose={() => setOpenId(null)} /> : null}
    >
      <InstallRetentionCard />
      <AlertsTable
        list={list}
        filters={filters}
        onFiltersChange={setFilters}
        kinds={INSTALL_KINDS}
        activeId={shown ? shown.id : null}
        onOpen={(alert) => setOpenId(alert.id)}
        emptyDescription="Each install-wide line of the Projects page is kept here from when it opened until it resolved: no gateway serving, changes not applied, background work behind, IP databases failing."
      />
    </Page>
  )
}

/** How many days resolved install alerts are kept; the hourly compaction deletes older ones. */
function InstallRetentionCard() {
  return (
    <RetentionCard
      queryKey="install-alert-retention"
      fetch={fetchInstallAlertRetention}
      update={updateInstallAlertRetention}
      label="Keep install alerts for"
      inputId="install-alert-retention"
      noun="Install alerts"
    />
  )
}
