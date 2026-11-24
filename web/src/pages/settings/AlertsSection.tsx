// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from '@tanstack/react-query'
import { ALERT_RANGES, fetchInstallAlert, fetchInstallAlertRetention, isNotFound, updateInstallAlertRetention } from '../../api/client'
import { AlertPanel } from '../../components/alerts/AlertPanel'
import { AlertsTable } from '../../components/alerts/AlertsTable'
import { INSTALL_KINDS } from '../../components/alerts/kinds'
import { Page } from '../../components/layout/Page'
import { RetentionCard } from '../../components/RetentionCard'
import { Inspector, LogoLoader, QueryStatus, Segmented } from '../../components/ui'
import { useTimeRange } from '../../contexts/TimeRangeContext'
import { useAlertFilters, useInstallAlerts } from '../../hooks/useAlerts'
import { useDropGone, useUrlParam } from '../../hooks/useUrlState'

/**
 * Administration, Alerts: the install-wide lines of the Projects page over time (gateways, background work, IP
 * databases), open and resolved, and how long resolved ones are kept.
 */
export default function AlertsSection() {
  const [range, setRange] = useTimeRange(ALERT_RANGES)
  const [filters, setFilters] = useAlertFilters(INSTALL_KINDS, false)
  const [open, setOpen] = useUrlParam('open')
  const openId = open || null
  const list = useInstallAlerts(filters)
  const loaded = openId ? list.alerts.find((alert) => alert.id === openId) : undefined
  // An alert the loaded rows do not hold (older, or under other filters) is read on its own.
  const single = useQuery({
    queryKey: ['install-alerts', 'one', openId],
    queryFn: () => fetchInstallAlert(openId!),
    enabled: !!openId && !loaded && !list.query.isPending,
    retry: (count, error) => !isNotFound(error) && count < 3,
  })
  useDropGone('open', isNotFound(single.error), 'That alert is no longer kept.')
  const shown = loaded ?? single.data
  const close = () => setOpen(null)

  return (
    <Page
      title="Alerts"
      subtitle="Install-wide problems over time: gateways, background work, IP databases."
      actions={(
        <div className="pointer-coarse:[&_button]:min-h-11">
          <Segmented options={ALERT_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} size="sm" />
        </div>
      )}
      panel={openId === null ? null : shown ? (
        <AlertPanel key={shown.id} alert={shown} projectId={null} onClose={close} />
      ) : (
        <Inspector title="Alert" onClose={close}>
          <QueryStatus query={single} what="the alert" loader={<LogoLoader className="py-6" />} />
        </Inspector>
      )}
    >
      <InstallRetentionCard />
      <AlertsTable
        list={list}
        filters={filters}
        onFiltersChange={setFilters}
        kinds={INSTALL_KINDS}
        activeId={openId}
        onOpen={(alert) => setOpen(alert.id)}
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
