// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from 'react'
import { useNavigate } from 'react-router'
import type { ConnectRoute } from '../api/client'
import { useNavigationGuardContext } from '../contexts/NavigationGuardContext'
import { cn } from '../utils/cn'

/** A connector's name that opens it on the Connectors page, asking first while an editor has unsaved work. */
export function ConnectorLink({ projectId, connectorId, className, children }: {
  projectId: string
  connectorId: string
  className?: string
  children: ReactNode
}) {
  const navigate = useNavigate()
  const guard = useNavigationGuardContext()
  return (
    <button
      type="button"
      onClick={() => guard.request(() => navigate(`/projects/${projectId}/connectors?open=${connectorId}`))}
      className={cn('font-medium text-fg hover:text-primary truncate text-left min-w-0', className)}
    >
      {children}
    </button>
  )
}

/** How a connector takes the request: sessions made on demand, dynamic sessions, or its proxies. */
function routeKind(route: ConnectRoute): string {
  if (route.on_demand) return 'creates sessions for the country on the first request'
  if (route.dynamic) return 'dynamic sessions: the place is rendered into each vendor session'
  return `${route.proxies} ${route.proxies === 1 ? 'proxy' : 'proxies'}`
}

/** The connectors a request would reach now, each with how it takes the request and its expected share by weight. */
export function RouteList({ projectId, routes }: { projectId: string; routes: ConnectRoute[] }) {
  return (
    <ul className="divide-y divide-line">
      {routes.map((route) => (
        <li key={route.connector_id} className="flex items-center gap-3 py-2 text-[13px]">
          <ConnectorLink projectId={projectId} connectorId={route.connector_id}>{route.connector_name}</ConnectorLink>
          <span className="text-xs text-fg-muted flex-1 truncate">{routeKind(route)}</span>
          <span className="text-xs tabular-nums text-fg-muted flex-none" title="Expected share of requests, by connector weight">{Math.round(route.share * 100)}%</span>
        </li>
      ))}
    </ul>
  )
}
