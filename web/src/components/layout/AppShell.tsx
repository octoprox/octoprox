// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState, type ReactNode } from 'react'
import { NavLink as RouterNavLink, Outlet, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  BarChart3, Server, Link2, Key, Eye, Settings as SettingsIcon, LogOut, ChevronLeft, ChevronRight,
  ChevronsUpDown, FolderOpen, ArrowLeft, User as UserIcon, Palette, Users as UsersIcon, DatabaseBackup,
} from 'lucide-react'
import octoproxLogo from '../../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../../assets/logos/octoprox_horizontal_dark.svg'
import octoproxLogoOnly from '../../assets/logos/octoprox_logo_only.svg'
import octoproxLogoOnlyDark from '../../assets/logos/octoprox_logo_only_dark.svg'
import { useProject } from '../../contexts/ProjectContext'
import { useAuth } from '../../contexts/AuthContext'
import { useTheme } from '../../contexts/ThemeContext'
import { fetchProjectMetrics, fetchProjectScalingMetrics } from '../../api/client'
import { popSettingsOrigin, setSettingsOrigin } from '../../utils/settingsOrigin'
import { cn } from '../../utils/cn'

const COLLAPSED_KEY = 'octoprox_sidebar_collapsed'

const PAGE_TITLES: Record<string, string> = {
  overview: 'Overview',
  proxies: 'Proxies',
  connectors: 'Connectors',
  credentials: 'Credentials',
  'mitm-inspector': 'MITM Inspector',
  account: 'Account',
  appearance: 'Appearance',
  users: 'Users',
  backup: 'Backup & Migration',
}

/**
 * Application frame: collapsible grouped sidebar, top bar with breadcrumb and
 * live pool ticker, and a flex row for the routed page (which may dock an
 * Inspector on its right). Used for project pages and for global settings.
 */
export default function AppShell({ onLogout }: { onLogout: () => void }) {
  const { projectId } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const { selectedProject, setSelectedProjectId, isLoading } = useProject()
  const { authStatus, isAdmin } = useAuth()
  const { isDark } = useTheme()
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem(COLLAPSED_KEY) === '1' } catch { return false }
  })

  useEffect(() => {
    try { localStorage.setItem(COLLAPSED_KEY, collapsed ? '1' : '0') } catch { /* ignore */ }
  }, [collapsed])

  // Keep the project context in sync with the URL.
  useEffect(() => {
    if (projectId) setSelectedProjectId(projectId)
  }, [projectId, setSelectedProjectId])

  const inSettings = /\/settings(\/|$)/.test(location.pathname)
  const base = projectId ? `/projects/${projectId}` : ''
  const settingsBase = `${base}/settings`

  const enterSettings = () => {
    if (!inSettings) setSettingsOrigin(location.pathname)
    navigate(settingsBase)
  }
  const leaveSettings = () => {
    const from = popSettingsOrigin()
    navigate(from || (projectId ? `${base}/overview` : '/'))
  }

  const segment = location.pathname.split('/').filter(Boolean).pop() || ''
  const pageTitle = PAGE_TITLES[segment] || (inSettings ? 'Settings' : 'Overview')

  return (
    <div className="h-screen flex bg-bg text-fg overflow-hidden">
      <aside
        className={cn(
          'flex-none h-full bg-surface border-r border-line flex flex-col transition-[width] duration-200 overflow-hidden',
          collapsed ? 'w-14' : 'w-[232px]'
        )}
      >
        {/* Logo + collapse toggle */}
        <div className={cn('h-14 flex items-center flex-none', collapsed ? 'justify-center' : 'justify-between pl-4 pr-2.5')}>
          {collapsed ? (
            <button onClick={() => setCollapsed(false)} className="p-1 rounded-md hover:bg-surface-raised transition-colors" title="Expand sidebar">
              <img src={isDark ? octoproxLogoOnlyDark : octoproxLogoOnly} alt="Octoprox" className="h-7 w-auto" />
            </button>
          ) : (
            <>
              <img src={isDark ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-[26px] w-auto" />
              <button
                onClick={() => setCollapsed(true)}
                className="p-1.5 rounded-md text-fg-subtle hover:text-fg hover:bg-surface-raised transition-colors"
                title="Collapse sidebar"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
            </>
          )}
        </div>

        {/* Project switcher */}
        <button
          onClick={() => { setSelectedProjectId(null); navigate('/') }}
          title="Switch project"
          className={cn(
            'flex items-center gap-2 text-[13px] font-medium text-fg hover:bg-surface-raised transition-colors flex-none',
            collapsed ? 'mx-2 my-1 h-9 justify-center rounded-md' : 'mx-3 my-1 h-[34px] px-2.5 rounded-lg border border-line'
          )}
        >
          <FolderOpen className="w-4 h-4 text-fg-subtle flex-none" />
          {!collapsed && (
            <>
              <span className="truncate flex-1 text-left">{projectId ? (selectedProject?.name ?? '…') : 'All projects'}</span>
              <ChevronsUpDown className="w-3.5 h-3.5 text-fg-subtle flex-none" />
            </>
          )}
        </button>

        {/* Navigation */}
        <nav className={cn('flex-1 min-h-0 overflow-y-auto flex flex-col gap-0.5', collapsed ? 'px-2 py-1' : 'px-3 py-1')}>
          {inSettings ? (
            <>
              <NavButton icon={<ArrowLeft />} label="Back" collapsed={collapsed} onClick={leaveSettings} />
              <NavGroup label="Settings" collapsed={collapsed} />
              <NavItem to={`${settingsBase}/account`} icon={<UserIcon />} label="Account" collapsed={collapsed} />
              <NavItem to={`${settingsBase}/appearance`} icon={<Palette />} label="Appearance" collapsed={collapsed} />
              {isAdmin && (
                <>
                  <NavGroup label="Administration" collapsed={collapsed} />
                  <NavItem to={`${settingsBase}/users`} icon={<UsersIcon />} label="Users" collapsed={collapsed} />
                  <NavItem to={`${settingsBase}/backup`} icon={<DatabaseBackup />} label="Backup & Migration" collapsed={collapsed} />
                </>
              )}
            </>
          ) : projectId ? (
            <>
              <NavGroup label="Overview" collapsed={collapsed} first />
              <NavItem to={`${base}/overview`} icon={<BarChart3 />} label="Overview" collapsed={collapsed} />
              <NavGroup label="Pool" collapsed={collapsed} />
              <NavItem to={`${base}/proxies`} icon={<Server />} label="Proxies" collapsed={collapsed} />
              <NavItem to={`${base}/connectors`} icon={<Link2 />} label="Connectors" collapsed={collapsed} />
              <NavItem to={`${base}/credentials`} icon={<Key />} label="Credentials" collapsed={collapsed} />
              <NavGroup label="Tools" collapsed={collapsed} />
              <NavItem to={`${base}/mitm-inspector`} icon={<Eye />} label="MITM Inspector" collapsed={collapsed} />
            </>
          ) : null}
        </nav>

        {/* Bottom: settings + account */}
        <div className={cn('border-t border-line flex-none', collapsed ? 'px-2 py-2 flex flex-col items-center gap-1' : 'px-3 py-2')}>
          {!inSettings && (
            <NavButton icon={<SettingsIcon />} label="Settings" collapsed={collapsed} onClick={enterSettings} />
          )}
          <div className={cn('flex items-center gap-2.5', collapsed ? 'justify-center py-1' : 'px-1 py-1.5')}>
            <div className="w-7 h-7 rounded-full bg-primary-soft text-primary-soft-fg flex items-center justify-center text-[11px] font-semibold flex-none uppercase" title={authStatus?.username ?? ''}>
              {(authStatus?.username ?? '?').slice(0, 2)}
            </div>
            {!collapsed && (
              <>
                <div className="flex-1 min-w-0 leading-tight">
                  <div className="text-[13px] font-medium truncate">{authStatus?.username}</div>
                  <div className="text-[11px] text-fg-subtle">{authStatus?.role}</div>
                </div>
                <button onClick={onLogout} className="p-1.5 rounded-md text-fg-muted hover:text-fg hover:bg-surface-raised transition-colors" title="Sign out">
                  <LogOut className="w-4 h-4" />
                </button>
              </>
            )}
          </div>
          {collapsed && (
            <button onClick={onLogout} className="p-1.5 rounded-md text-fg-muted hover:text-fg hover:bg-surface-raised transition-colors" title="Sign out">
              <LogOut className="w-4 h-4" />
            </button>
          )}
        </div>
      </aside>

      <main className="flex-1 min-w-0 flex flex-col">
        <header className="h-11 flex-none flex items-center gap-4 px-4 border-b border-line bg-surface">
          <div className="flex items-center gap-1.5 text-[13px] min-w-0">
            {projectId && selectedProject ? (
              <>
                <button onClick={() => navigate(`${base}/overview`)} className="text-fg-muted hover:text-fg truncate">{selectedProject.name}</button>
                <ChevronRight className="w-3 h-3 text-fg-subtle flex-none" />
              </>
            ) : (
              <>
                <button onClick={() => navigate('/')} className="text-fg-muted hover:text-fg">Projects</button>
                <ChevronRight className="w-3 h-3 text-fg-subtle flex-none" />
              </>
            )}
            {inSettings && (
              <>
                <span className="text-fg-muted">Settings</span>
                <ChevronRight className="w-3 h-3 text-fg-subtle flex-none" />
              </>
            )}
            <span className="font-semibold truncate">{pageTitle}</span>
          </div>
          <div className="flex-1" />
          {projectId && <PoolTicker projectId={projectId} />}
        </header>

        <div className="flex-1 min-h-0 flex">
          {projectId && isLoading && !selectedProject ? (
            <div className="flex-1 flex items-center justify-center text-fg-muted text-sm">Loading project…</div>
          ) : (
            <Outlet />
          )}
        </div>
      </main>
    </div>
  )
}

function NavGroup({ label, collapsed, first }: { label: string; collapsed: boolean; first?: boolean }) {
  if (collapsed) {
    return first ? null : <div className="h-px bg-line my-2 mx-1.5" aria-hidden />
  }
  return (
    <div className={cn('px-2.5 pb-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-fg-subtle', first ? 'pt-2' : 'pt-3.5')}>
      {label}
    </div>
  )
}

const navItemBase = 'flex items-center gap-2.5 rounded-md text-[13.5px] transition-colors whitespace-nowrap [&>svg]:w-4 [&>svg]:h-4 [&>svg]:flex-none'
const navItemCollapsed = 'justify-center w-10 h-10 [&>svg]:w-5 [&>svg]:h-5'
const navItemExpanded = 'h-[34px] px-2.5'

function NavItem({ to, icon, label, collapsed }: { to: string; icon: ReactNode; label: string; collapsed: boolean }) {
  return (
    <RouterNavLink
      to={to}
      title={collapsed ? label : undefined}
      className={({ isActive }) =>
        cn(
          navItemBase,
          collapsed ? navItemCollapsed : navItemExpanded,
          isActive
            ? 'bg-primary-soft text-primary-soft-fg font-medium'
            : 'text-fg-muted hover:bg-surface-raised hover:text-fg'
        )
      }
    >
      {icon}
      {!collapsed && <span className="truncate">{label}</span>}
    </RouterNavLink>
  )
}

function NavButton({ icon, label, collapsed, onClick }: { icon: ReactNode; label: string; collapsed: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={collapsed ? label : undefined}
      className={cn(navItemBase, collapsed ? navItemCollapsed : cn(navItemExpanded, 'w-full'), 'text-fg-muted hover:bg-surface-raised hover:text-fg')}
    >
      {icon}
      {!collapsed && <span className="truncate">{label}</span>}
    </button>
  )
}

/** Live pool summary in the top bar. Shared query keys with the Overview page. */
function PoolTicker({ projectId }: { projectId: string }) {
  const { data: metrics } = useQuery({
    queryKey: ['metrics', projectId],
    queryFn: () => fetchProjectMetrics(projectId),
    refetchInterval: 10000,
  })
  const { data: scaling } = useQuery({
    queryKey: ['scaling-metrics', projectId],
    queryFn: () => fetchProjectScalingMetrics(projectId),
    refetchInterval: 10000,
  })
  const pool = metrics?.pool
  if (!pool) return null
  return (
    <div className="flex items-center gap-2.5 text-xs text-fg-muted tabular-nums whitespace-nowrap">
      <TickerDot className="bg-success" value={pool.healthy_proxies} title="Healthy" />
      <TickerDot className="bg-orange-500" value={pool.quarantined_proxies} title="Quarantined" />
      <TickerDot className="bg-danger" value={pool.unhealthy_proxies} title="Unhealthy" />
      <span className="text-line-strong">|</span>
      <span title="Average latency">{Math.round(pool.avg_latency_ms)} ms</span>
      {scaling && (
        <>
          <span className="text-line-strong">|</span>
          <span title="Requests per minute">{scaling.requests_per_minute.toFixed(0)} req/min</span>
        </>
      )}
      <span className="inline-flex items-center gap-1 px-1.5 py-px rounded-full bg-success-soft text-success text-[11px] font-medium">
        <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse-soft" />
        live
      </span>
    </div>
  )
}

function TickerDot({ className, value, title }: { className: string; value: number; title: string }) {
  return (
    <span className="inline-flex items-center gap-1.5" title={title}>
      <span className={cn('w-2 h-2 rounded-full', className)} />
      {value}
    </span>
  )
}
