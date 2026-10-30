// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Help entry for the sidebar: a popover with What's New, the guide for the
 * page the user is on, the documentation site, the API reference and where to
 * report a problem. A dot on the button points out release notes the user has
 * not opened, and for admins a newer release.
 */

import { useState, type ReactNode } from 'react'
import { useLocation } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUpCircle, BookOpen, Braces, Bug, ExternalLink, HelpCircle, LifeBuoy, Sparkles } from 'lucide-react'
import { fetchReleases, markReleasesSeen, type ReleaseInfo } from '../api/client'
import { useAuth } from '../contexts/AuthContext'
import { cn } from '../utils/cn'
import { navItemBase, navItemCollapsed, navItemExpanded, navItemIdle } from './layout/navStyles'
import { ProviderMark } from './SignInProviderMark'
import { DOCS_URL } from '../utils/docs'
import { WhatsNewDialog } from './WhatsNew'
import { PopoverMenu, besideSidebarFoot, usePopoverMenu } from './ui/PopoverMenu'

const REPO_URL = 'https://github.com/octoprox/octoprox'

interface DocLink { title: string; path: string }

/** The guide for each page, keyed by the last path segment. */
const PAGE_DOCS: Record<string, DocLink[]> = {
  overview: [{ title: 'Metrics and request counting', path: 'metrics' }, { title: 'Routing strategies', path: 'routing-strategies' }],
  hosts: [{ title: 'Hosts: where the traffic goes', path: 'metrics#hosts-where-the-traffic-goes' }],
  failures: [{ title: 'Failures: why requests failed', path: 'metrics#failures-why-requests-failed' }],
  proxies: [{ title: 'Rate limiting and quarantine', path: 'rate-limiting' }, { title: 'Location routing', path: 'location-routing' }],
  connectors: [{ title: 'Providers and connectors', path: 'providers' }, { title: 'Traffic limits and billing', path: 'traffic-limits' }, { title: 'Domain filtering', path: 'domain-filtering' }],
  credentials: [{ title: 'Providers and credentials', path: 'providers' }],
  connect: [{ title: 'Connecting clients', path: 'connecting' }, { title: 'Country, state and city routing', path: 'routing-strategies#country-routing' }],
  'mitm-inspector': [{ title: 'TLS interception', path: 'tls-interception' }],
  locations: [{ title: 'IP attribution and exit verification', path: 'ip-attribution' }],
  devices: [{ title: 'WireGuard devices', path: 'wireguard' }, { title: 'OpenVPN devices', path: 'openvpn' }],
  account: [{ title: 'Personal access tokens', path: 'sso#personal-access-tokens' }],
  users: [{ title: 'Authentication', path: 'configuration#authentication' }],
  'sign-in': [{ title: 'Single sign-on', path: 'sso' }],
  providers: [{ title: 'Providers and the provider SDK', path: 'providers' }],
  geo: [{ title: 'IP attribution and exit verification', path: 'ip-attribution' }],
  backup: [{ title: 'Backup and migration', path: 'api#backup--migration' }],
  system: [{ title: 'Deployment and scaling', path: 'deployment' }],
  wireguard: [{ title: 'WireGuard devices', path: 'wireguard' }],
  openvpn: [{ title: 'OpenVPN devices', path: 'openvpn' }],
}

export function docsForPath(pathname: string): DocLink[] {
  const segment = pathname.split('/').filter(Boolean).pop() ?? ''
  return PAGE_DOCS[segment] ?? []
}

/** The release notes change only with a deploy, and the update check once a day. */
const RELEASES_STALE_MS = 10 * 60_000

const MENU_WIDTH = 272

export function HelpMenu({ collapsed }: { collapsed: boolean }) {
  const location = useLocation()
  const menu = usePopoverMenu({ moves: collapsed, place: (button) => besideSidebarFoot(button, MENU_WIDTH, collapsed) })
  // What's New as it stood when opened, so its New marks outlast marking the notes seen.
  const [whatsNew, setWhatsNew] = useState<ReleaseInfo | null>(null)

  // Per user: what is unseen, and the admins-only update, are the signed-in user's, and sign-out keeps the cache.
  const { authStatus } = useAuth()
  const releasesKey = ['releases', authStatus?.user_id]
  const queryClient = useQueryClient()
  const { data: releases } = useQuery({ queryKey: releasesKey, queryFn: fetchReleases, staleTime: RELEASES_STALE_MS })
  const markSeen = useMutation({
    mutationFn: markReleasesSeen,
    onSuccess: (info) => queryClient.setQueryData(releasesKey, info),
  })
  const unseen = releases?.unseen ?? 0
  const update = releases?.update ?? null
  const attention = unseen > 0 || update !== null

  const openWhatsNew = () => {
    if (!releases) return
    menu.close()
    setWhatsNew(releases)
    if (releases.unseen > 0) markSeen.mutate()
  }

  const pageDocs = docsForPath(location.pathname)

  return (
    <>
      <button
        {...menu.buttonProps}
        type="button"
        title={collapsed ? (attention ? 'Help: something new' : 'Help') : undefined}
        aria-label={attention ? 'Help, something new' : undefined}
        className={cn('relative', navItemBase, collapsed ? navItemCollapsed : cn(navItemExpanded, 'w-full'), menu.open ? 'bg-surface-raised text-fg' : navItemIdle)}
      >
        <HelpCircle />
        {!collapsed && <span className="truncate flex-1 text-left">Help</span>}
        {attention && (
          collapsed
            ? <span className="absolute top-2 right-2 w-2 h-2 rounded-full bg-primary ring-2 ring-surface" aria-hidden />
            : <span className="flex-none text-[10.5px] font-semibold leading-none px-1.5 py-1 rounded-full bg-primary-soft text-primary-soft-fg" aria-hidden>New</span>
        )}
      </button>

      <PopoverMenu state={menu} label="Help" className="py-1.5 overflow-y-auto overscroll-contain">
        {update && (
          <MenuButton onClick={openWhatsNew} icon={<ArrowUpCircle className="text-primary!" />}>
            <span className="truncate flex-1">Octoprox {update.version} is available</span>
          </MenuButton>
        )}
        {releases && (
          <MenuButton onClick={openWhatsNew} icon={<Sparkles />}>
            <span className="truncate flex-1">What's New</span>
            {unseen > 0 && <span className="flex-none text-[11px] font-medium text-primary-soft-fg bg-primary-soft rounded-full px-1.5 py-0.5">{unseen} new</span>}
          </MenuButton>
        )}
        {releases && <MenuDivider />}
        {pageDocs.length > 0 && (
          <>
            <MenuLabel>About this page</MenuLabel>
            {pageDocs.map((d) => (
              <MenuLink key={d.path} href={`${DOCS_URL}/${d.path}`} icon={<BookOpen />}>{d.title}</MenuLink>
            ))}
            <MenuDivider />
          </>
        )}
        <MenuLabel>Documentation</MenuLabel>
        <MenuLink href={`${DOCS_URL}/getting-started`} icon={<LifeBuoy />}>Getting started</MenuLink>
        <MenuLink href={`${DOCS_URL}/configuration`} icon={<BookOpen />}>Configuration reference</MenuLink>
        <MenuLink href={`${DOCS_URL}/api`} icon={<Braces />}>API reference</MenuLink>
        <MenuDivider />
        <MenuLink href={REPO_URL} icon={<ProviderMark kind="github" />}>Source on GitHub</MenuLink>
        <MenuLink href={`${REPO_URL}/issues`} icon={<Bug />}>Report a problem</MenuLink>
      </PopoverMenu>

      {whatsNew && <WhatsNewDialog info={whatsNew} onClose={() => setWhatsNew(null)} />}
    </>
  )
}

function MenuLabel({ children }: { children: ReactNode }) {
  return <div className="px-3 pt-1.5 pb-1 text-[11px] font-semibold uppercase tracking-[0.06em] text-fg-subtle">{children}</div>
}

function MenuDivider() {
  return <div className="h-px bg-line my-1.5" aria-hidden />
}

const menuItemClasses = 'flex items-center gap-2.5 px-3 h-8 text-fg-muted outline-none hover:bg-surface-raised hover:text-fg focus-visible:bg-surface-raised focus-visible:text-fg transition-colors [&>svg]:w-4 [&>svg]:h-4 [&>svg]:flex-none [&>svg]:text-fg-subtle'

function MenuButton({ onClick, icon, children }: { onClick: () => void; icon: ReactNode; children: ReactNode }) {
  return (
    <button type="button" role="menuitem" onClick={onClick} className={cn(menuItemClasses, 'w-full text-left')}>
      {icon}
      {children}
    </button>
  )
}

function MenuLink({ href, icon, children }: { href: string; icon: ReactNode; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      role="menuitem"
      className={menuItemClasses}
    >
      {icon}
      <span className="truncate flex-1">{children}</span>
      <ExternalLink className="w-3! h-3! opacity-60" />
    </a>
  )
}
