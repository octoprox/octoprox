// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * What needed attention in a project, open and resolved: each line of the Needs attention card from when it opened
 * until it resolved, the covered and muted ones included. The rows refresh in place every minute; alerts that opened
 * since are announced above them and shown on request.
 */

import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { ALERT_RANGES, fetchProjectAlert } from '../api/client'
import { AlertPanel } from '../components/alerts/AlertPanel'
import { AlertsTable } from '../components/alerts/AlertsTable'
import { PROJECT_KINDS } from '../components/alerts/kinds'
import { Page } from '../components/layout/Page'
import { Inspector, LogoLoader, QueryStatus, Segmented } from '../components/ui'
import { useProject } from '../contexts/ProjectContext'
import { useTimeRange } from '../contexts/TimeRangeContext'
import { useProjectAlerts, type AlertListFilters } from '../hooks/useAlerts'

export default function AlertsPage() {
  const { selectedProjectId, selectedProject } = useProject()
  const [range, setRange] = useTimeRange(ALERT_RANGES)
  const [filters, setFilters] = useState<AlertListFilters>({ status: 'all' })
  const [openId, setOpenId] = useState<string | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const list = useProjectAlerts(selectedProjectId, filters)

  // Deep link from the Needs attention panel: ?open=<id>
  useEffect(() => {
    const open = searchParams.get('open')
    if (open) {
      setOpenId(open)
      searchParams.delete('open')
      setSearchParams(searchParams, { replace: true })
    }
  }, [searchParams])

  const loaded = openId ? list.alerts.find((alert) => alert.id === openId) : undefined
  // An alert the loaded rows do not hold (older, or under other filters) is read on its own.
  const single = useQuery({
    queryKey: ['alerts', selectedProjectId, 'one', openId],
    queryFn: () => fetchProjectAlert(selectedProjectId!, openId!),
    enabled: !!selectedProjectId && !!openId && !loaded && !list.query.isPending,
  })
  const shown = loaded ?? single.data
  const close = () => setOpenId(null)

  return (
    <Page
      title="Alerts"
      subtitle={selectedProject ? `What needed attention in ${selectedProject.name}, open and resolved.` : undefined}
      actions={(
        <div className="pointer-coarse:[&_button]:min-h-11">
          <Segmented options={ALERT_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} size="sm" />
        </div>
      )}
      panel={openId === null || !selectedProjectId ? null : shown ? (
        <AlertPanel key={shown.id} alert={shown} projectId={selectedProjectId} onClose={close} />
      ) : (
        <Inspector title="Alert" onClose={close}>
          <QueryStatus query={single} what="the alert" loader={<LogoLoader className="py-6" />} />
        </Inspector>
      )}
    >
      <AlertsTable
        list={list}
        filters={filters}
        onFiltersChange={setFilters}
        kinds={PROJECT_KINDS}
        search
        activeId={openId}
        onOpen={(alert) => setOpenId(alert.id)}
        emptyDescription="Each line the Needs attention card shows is kept here from when it opened until it resolved, with how long it lasted and how often it came back."
      />
    </Page>
  )
}
