// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { startTransition, useEffect, useState, type ReactNode } from 'react'
import { NavLink as RouterNavLink, Outlet, useLocation, useNavigate, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import {
  BarChart3, Server, Link2, KeyRound, Eye, Settings as SettingsIcon, ChevronLeft, ChevronRight,
  ArrowLeft, User as UserIcon, Palette, Users as UsersIcon, DatabaseBackup, Puzzle,
  Activity, Globe, Router, Shield, ShieldCheck, Target, LogIn, Plug, AlertTriangle,
} from 'lucide-react'
import octoproxLogo from '../../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../../assets/logos/octoprox_horizontal_dark.svg'
import octoproxLogoOnly from '../../assets/logos/octoprox_logo_only.svg'
import octoproxLogoOnlyDark from '../../assets/logos/octoprox_logo_only_dark.svg'
import { useProject } from '../../contexts/ProjectContext'
import { useAuth } from '../../contexts/AuthContext'
import { useTheme } from '../../contexts/ThemeContext'
import { useNavigationGuardContext } from '../../contexts/NavigationGuardContext'
import { fetchProjectMetrics, fetchProjectScalingMetrics, poolBreakdown } from '../../api/client'
import { popSettingsOrigin, setSettingsOrigin } from '../../utils/settingsOrigin'
import { HelpMenu } from '../HelpMenu'
import { ProjectPanel } from '../ProjectForm'
import { NewVersionBanner } from '../NewVersion'
import { UserMenu } from '../UserMenu'
import { ProjectSwitcher } from './ProjectSwitcher'
import { PageScopeContext, ShellPanelContext } from './Page'
import { navItemBase, navItemCollapsed, navItemExpanded, navItemIdle } from './navStyles'
import { cn } from '../../utils/cn'

const COLLAPSED_KEY = 'octoprox_sidebar_collapsed'
/** Below Tailwind's lg breakpoint the sidebar is a rail and opens over the page. In rem, as Tailwind writes it, so the two agree at any font size. */
const NARROW = '(width < 64rem)'
const isNarrow = () => window.matchMedia(NARROW).matches
/** The same path in another project. */
const inProject = (path: string, id: string) => path.replace(/^\/projects\/[^/]+/, `/projects/${id}`)

const PAGE_TITLES: Record<string, string> = {
  overview: 'Overview',
  hosts: 'Hosts',
  failures: 'Failures',
  proxies: 'Proxies',
  connectors: 'Connectors',
  credentials: 'Credentials',
  connect: 'Connect',
  'mitm-inspector': 'MITM Inspector',
  locations: 'Exit locations',
  devices: 'Devices',
  wireguard: 'WireGuard',
  openvpn: 'OpenVPN',
  account: 'Account',
  appearance: 'Appearance',
  users: 'Users',
  'sign-in': 'Sign-in',
  backup: 'Backup & Migration',
  providers: 'Providers',
  geo: 'IP attribution',
  system: 'System',
}

/** The areas outside any project: what the sidebar header says in place of the project switcher. */
const AREAS = {
  account: { title: 'Your account', scope: 'Only you', icon: <UserIcon /> },
  admin: { title: 'Administration', scope: 'All projects', icon: <Shield /> },
}
export type Area = keyof typeof AREAS

/**
 * Application frame: collapsible grouped sidebar, top bar with breadcrumb and
 * live pool ticker, and a flex row for the routed page (which may dock an
 * Inspector on its right). Used for project pages, and for Account and
 * Administration, which their routes name in `area`.
 */
export default function AppShell({ onLogout, area }: { onLogout: () => void; area?: Area }) {
  const { projectId } = useParams()
  const location = useLocation()
  const routerNavigate = useNavigate()
  const guard = useNavigationGuardContext()
  // Every navigation the shell performs goes through the unsaved-changes guard.
  const navigate = (to: string) => guard.request(() => routerNavigate(to))
  const { selectedProjectId, selectedProject, setSelectedProjectId } = useProject()
  const { isAdmin, canMutate } = useAuth()
  const { isDark } = useTheme()
  // Project settings belong to the page they were opened on: leaving it closes them.
  const [projectSettingsOn, setProjectSettingsOn] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (isNarrow()) return true // a phone never opens on the overlay
    try { return localStorage.getItem(COLLAPSED_KEY) === '1' } catch { return false }
  })

  useEffect(() => {
    if (isNarrow()) return // auto-collapsed, not a preference
    try { localStorage.setItem(COLLAPSED_KEY, collapsed ? '1' : '0') } catch { /* ignore */ }
  }, [collapsed])

  // Narrow viewports: collapse the sidebar automatically and restore the saved
  // preference when there is room again, so the page content never disappears.
  useEffect(() => {
    const media = window.matchMedia(NARROW)
    const apply = () => {
      if (media.matches) setCollapsed(true)
      else { try { setCollapsed(localStorage.getItem(COLLAPSED_KEY) === '1') } catch { setCollapsed(false) } }
    }
    apply()
    media.addEventListener('change', apply)
    return () => media.removeEventListener('change', apply)
  }, [])

  // On narrow viewports the open sidebar covers the page, so picking a page (the current one too) closes it.
  useEffect(() => {
    if (isNarrow()) setCollapsed(true)
  }, [location.key])

  // Leaving the page closes project settings for good, so coming back to it does not reopen them.
  useEffect(() => { setProjectSettingsOn(null) }, [location.pathname])

  // Keep the project context in sync with the URL.
  useEffect(() => {
    if (projectId) setSelectedProjectId(projectId)
  }, [projectId, setSelectedProjectId])

  const base = `/projects/${projectId}`

  const enterAdmin = () => {
    setSettingsOrigin(location.pathname)
    navigate('/admin')
  }
  // Back to the page the area was entered from; without one, the project open before or the project list.
  // Popped only once leaving is confirmed, so cancelling the unsaved-changes prompt keeps it.
  const leaveArea = () => guard.request(() => routerNavigate(popSettingsOrigin() || (selectedProjectId ? `/projects/${selectedProjectId}/overview` : '/')))

  // The same page in the other project. Query string and hash belong to the old project.
  // The router applies a navigation as a transition; the new id joins it, so no page renders it under the old URL.
  const switchProject = (id: string) => guard.request(() => startTransition(() => {
    setSelectedProjectId(id)
    routerNavigate(inProject(location.pathname, id))
  }))
  const showAllProjects = () => guard.request(() => startTransition(() => {
    setSelectedProjectId(null)
    routerNavigate('/')
  }))
  // Pages read the project from the context, so they wait until it holds the project in the URL (after a switch,
  // or Back into another project). A failed load clears the selection and lets the page show that.
  const projectPending = !!projectId && !!selectedProjectId && (selectedProjectId !== projectId || !selectedProject)
  const openProjectSettings = () => {
    if (isNarrow()) setCollapsed(true) // the panel covers the screen on a phone
    setProjectSettingsOn(location.pathname)
  }

  const segment = location.pathname.split('/').filter(Boolean).pop() || ''
  const pageTitle = PAGE_TITLES[segment] || 'Overview'

  return (
    <div className="h-screen flex bg-bg text-fg overflow-hidden">
      {/* Narrow viewports: the sidebar is fixed over a rail-wide spacer, so it opens over the page like the Inspector
          and closing it never reflows the page; the backdrop closes it. */}
      <div className="w-14 flex-none lg:hidden" aria-hidden />
      {!collapsed && <div className="fixed inset-0 z-30 bg-overlay/30 lg:hidden" onClick={() => setCollapsed(true)} aria-hidden />}
      <aside
        className={cn(
          'flex-none h-full bg-surface border-r border-line flex flex-col transition-[width] duration-200 overflow-hidden',
          'max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:z-20',
          collapsed ? 'w-14' : 'w-[232px] max-lg:z-40 max-lg:shadow-2xl'
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

        {area ? (
          <AreaHeader {...AREAS[area]} collapsed={collapsed} />
        ) : (
          <ProjectSwitcher
            collapsed={collapsed}
            projectId={projectId}
            label={selectedProject?.name ?? '…'}
            onSwitch={(p) => switchProject(p.id)}
            onAllProjects={showAllProjects}
            onProjectSettings={canMutate && selectedProject ? openProjectSettings : undefined}
          />
        )}

        {/* Navigation */}
        <nav className={cn('flex-1 min-h-0 overflow-y-auto flex flex-col gap-0.5', collapsed ? 'px-2 py-1' : 'px-3 py-1')}>
          {area === 'account' ? (
            <>
              <NavButton icon={<ArrowLeft />} label="Back" collapsed={collapsed} onClick={leaveArea} />
              <NavDivider />
              <NavItem to="/account" end icon={<UserIcon />} label="Account" collapsed={collapsed} />
              <NavItem to="/account/appearance" icon={<Palette />} label="Appearance" collapsed={collapsed} />
            </>
          ) : area === 'admin' ? (
            <>
              <NavButton icon={<ArrowLeft />} label="Back" collapsed={collapsed} onClick={leaveArea} />
              <NavDivider />
              <NavItem to="/admin/users" icon={<UsersIcon />} label="Users" collapsed={collapsed} />
              <NavItem to="/admin/sign-in" icon={<LogIn />} label="Sign-in" collapsed={collapsed} />
              <NavItem to="/admin/providers" icon={<Puzzle />} label="Providers" collapsed={collapsed} />
              <NavItem to="/admin/geo" icon={<Globe />} label="IP attribution" collapsed={collapsed} />
              <NavItem to="/admin/backup" icon={<DatabaseBackup />} label="Backup & Migration" collapsed={collapsed} />
              <NavItem to="/admin/system" icon={<Activity />} label="System" collapsed={collapsed} />
              <NavGroup label="Tunnels" collapsed={collapsed} />
              <NavItem to="/admin/wireguard" icon={<Router />} label="WireGuard" collapsed={collapsed} />
              <NavItem to="/admin/openvpn" icon={<ShieldCheck />} label="OpenVPN" collapsed={collapsed} />
            </>
          ) : projectId ? (
            <>
              <NavGroup label="Overview" collapsed={collapsed} first />
              <NavItem to={`${base}/overview`} icon={<BarChart3 />} label="Overview" collapsed={collapsed} />
              <NavItem to={`${base}/hosts`} icon={<Target />} label="Hosts" collapsed={collapsed} />
              <NavItem to={`${base}/failures`} icon={<AlertTriangle />} label="Failures" collapsed={collapsed} />
              <NavGroup label="Pool" collapsed={collapsed} />
              <NavItem to={`${base}/proxies`} icon={<Server />} label="Proxies" collapsed={collapsed} />
              <NavItem to={`${base}/connectors`} icon={<Link2 />} label="Connectors" collapsed={collapsed} />
              <NavItem to={`${base}/credentials`} icon={<KeyRound />} label="Credentials" collapsed={collapsed} />
              <NavGroup label="Tools" collapsed={collapsed} />
              <NavItem to={`${base}/connect`} icon={<Plug />} label="Connect" collapsed={collapsed} />
              <NavItem to={`${base}/locations`} icon={<Globe />} label="Exit locations" collapsed={collapsed} />
              <NavItem to={`${base}/mitm-inspector`} icon={<Eye />} label="MITM Inspector" collapsed={collapsed} />
              <NavGroup label="Tunnels" collapsed={collapsed} />
              <NavItem to={`${base}/devices`} icon={<Router />} label="Devices" collapsed={collapsed} />
              {canMutate && selectedProject && (
                <>
                  <NavGroup label="Project" collapsed={collapsed} />
                  <NavButton icon={<SettingsIcon />} label="Project settings" collapsed={collapsed} onClick={projectSettingsOn === location.pathname ? () => setProjectSettingsOn(null) : openProjectSettings} active={projectSettingsOn === location.pathname} />
                </>
              )}
            </>
          ) : null}
        </nav>

        {/* Bottom: administration, help and the user's own menu */}
        <div className={cn('border-t border-line flex-none', collapsed ? 'px-2 py-2 flex flex-col items-center gap-1' : 'px-3 py-2')}>
          {isAdmin && area !== 'admin' && (
            <NavButton icon={<Shield />} label="Administration" collapsed={collapsed} onClick={enterAdmin} />
          )}
          <HelpMenu collapsed={collapsed} />
          <UserMenu placement="sidebar" collapsed={collapsed} onSignOut={onLogout} />
        </div>
      </aside>

      <main className="flex-1 min-w-0 flex flex-col">
        <NewVersionBanner />
        {/* The pool ticker drops to a second line when it does not fit beside the breadcrumb. */}
        <header className="min-h-11 flex-none flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-1.5 border-b border-line bg-surface">
          <div className="flex items-center gap-1.5 text-[13px] min-w-0">
            {area ? (
              <span className="text-fg-muted truncate">{AREAS[area].title}</span>
            ) : selectedProject ? (
              <button onClick={() => navigate(`${base}/overview`)} className="text-fg-muted hover:text-fg truncate">{selectedProject.name}</button>
            ) : (
              <button onClick={() => navigate('/')} className="text-fg-muted hover:text-fg">Projects</button>
            )}
            <ChevronRight className="w-3 h-3 text-fg-subtle flex-none" />
            <span className="font-semibold truncate">{pageTitle}</span>
          </div>
          {projectId && <PoolTicker projectId={projectId} />}
        </header>

        <div className="flex-1 min-h-0 flex">
          {projectPending ? (
            <div className="flex-1 flex items-center justify-center text-fg-muted text-sm">Loading project…</div>
          ) : (
            // Keyed by project, so a switch starts the page afresh instead of keeping the old project's panels and filters.
            <ShellPanelContext
              value={projectSettingsOn === location.pathname && selectedProject ? <ProjectPanel project={selectedProject} onClose={() => setProjectSettingsOn(null)} /> : null}
            >
              <PageScopeContext value={area === 'admin' ? AREAS.admin.scope : null}>
                <Outlet key={projectId} />
              </PageScopeContext>
            </ShellPanelContext>
          )}
        </div>
      </main>
    </div>
  )
}

function NavGroup({ label, collapsed, first }: { label: string; collapsed: boolean; first?: boolean }) {
  if (collapsed) {
    return first ? null : <NavDivider />
  }
  return (
    <div className={cn('px-2.5 pb-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-fg-subtle', first ? 'pt-2' : 'pt-3.5')}>
      {label}
    </div>
  )
}

/** Stands where the project switcher does while the sidebar shows an area outside any project, and says what it covers. */
function AreaHeader({ title, scope, icon, collapsed }: { title: string; scope: string; icon: ReactNode; collapsed: boolean }) {
  if (collapsed) {
    return (
      <div className="mx-2 my-1 h-9 flex-none flex items-center justify-center text-fg-subtle [&>svg]:w-4 [&>svg]:h-4" title={`${title}: ${scope.toLowerCase()}`}>
        {icon}
      </div>
    )
  }
  return (
    <div className="mx-3 my-1 px-2.5 py-1.5 flex-none flex items-center gap-2 rounded-lg border border-line bg-surface-raised/60 [&>svg]:w-4 [&>svg]:h-4 [&>svg]:flex-none [&>svg]:text-fg-subtle">
      {icon}
      <div className="min-w-0 leading-tight">
        <div className="text-[13px] font-medium text-fg truncate">{title}</div>
        <div className="text-[11px] text-fg-subtle truncate">{scope}</div>
      </div>
    </div>
  )
}

function NavDivider() {
  return <div className="h-px bg-line my-2 mx-1.5" aria-hidden />
}

function NavItem({ to, end, icon, label, collapsed }: { to: string; end?: boolean; icon: ReactNode; label: string; collapsed: boolean }) {
  const guard = useNavigationGuardContext()
  const routerNavigate = useNavigate()
  return (
    <RouterNavLink
      to={to}
      end={end}
      onClick={(e) => {
        if (!guard.blocked) return
        e.preventDefault()
        guard.request(() => routerNavigate(to))
      }}
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

/** `active` marks a button whose panel is open, as the Help button is marked while its menu is. */
function NavButton({ icon, label, collapsed, onClick, active }: { icon: ReactNode; label: string; collapsed: boolean; onClick: () => void; active?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={collapsed ? label : undefined}
      aria-pressed={active}
      className={cn(navItemBase, collapsed ? navItemCollapsed : cn(navItemExpanded, 'w-full'), active ? 'bg-surface-raised text-fg' : navItemIdle)}
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
  // Each proxy once: the API also counts quarantined proxies inside unhealthy.
  const { healthy, quarantined, unhealthy } = poolBreakdown(pool)
  // Phones drop the separators and the word "live" so the ticker fits one line; it wraps rather than clips past that.
  return (
    <div className="ml-auto flex flex-wrap justify-end items-center gap-x-2 gap-y-1 sm:gap-x-2.5 text-xs text-fg-muted tabular-nums whitespace-nowrap">
      <TickerDot className="bg-success" value={healthy} title="Healthy" />
      <TickerDot className="bg-orange-500" value={quarantined} title="Quarantined" />
      <TickerDot className="bg-danger" value={unhealthy} title="Unhealthy, initializing or unknown" />
      <span className="text-line-strong max-sm:hidden">|</span>
      <span title="Average latency of the successful requests">{pool.total_successes > 0 ? `${Math.round(pool.avg_latency_ms)} ms` : '- ms'}</span>
      {scaling && (
        <>
          <span className="text-line-strong max-sm:hidden">|</span>
          <span title="Requests per minute">{scaling.requests_per_minute.toFixed(0)} req/min</span>
        </>
      )}
      <span
        className="inline-flex items-center gap-1 px-1.5 py-px rounded-full bg-success-soft text-success text-[11px] font-medium"
        title="Refreshed every 10 seconds from Postgres and Redis, the same whichever api process answers"
      >
        <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse-soft" />
        <span className="max-sm:hidden">live</span>
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
