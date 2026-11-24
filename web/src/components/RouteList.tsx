// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'
import type { ConnectRoute } from '../api/client'
import { useNavigationGuardContext } from '../contexts/NavigationGuardContext'
import { cn } from '../utils/cn'

/**
 * A link to another page's view. A plain click asks first while an editor has unsaved work; a modifier click opens a
 * new browser tab and leaves this page as it is.
 */
export function GuardedLink({ to, className, title, children }: { to: string; className?: string; title?: string; children: ReactNode }) {
  const navigate = useNavigate()
  const guard = useNavigationGuardContext()
  return (
    <Link
      to={to}
      title={title}
      onClick={(e) => {
        if (!guard.blocked || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
        e.preventDefault()
        guard.request(() => navigate(to))
      }}
      className={className}
    >
      {children}
    </Link>
  )
}

/** A connector's name that opens it on the Connectors page, asking first while an editor has unsaved work. */
export function ConnectorLink({ projectId, connectorId, className, children }: {
  projectId: string
  connectorId: string
  className?: string
  children: ReactNode
}) {
  return (
    // 44 px to tap on a touch screen without making the row it sits in any taller.
    <GuardedLink
      to={`/projects/${projectId}/connectors?open=${connectorId}`}
      className={cn('inline-flex items-center min-w-0 max-w-full font-medium text-fg hover:text-primary pointer-coarse:min-h-11 pointer-coarse:-my-3', className)}
    >
      <span className="truncate">{children}</span>
    </GuardedLink>
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
