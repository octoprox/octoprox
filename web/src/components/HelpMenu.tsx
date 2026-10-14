// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Help entry for the sidebar: a popover with the guide for the page the user
 * is on, the documentation site, the API reference and where to report a
 * problem. Rendered fixed next to the sidebar so it is never clipped by the
 * collapsed rail.
 */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { BookOpen, Braces, Bug, ExternalLink, Github, HelpCircle, LifeBuoy } from 'lucide-react'
import { cn } from '../utils/cn'
import { navItemBase, navItemCollapsed, navItemExpanded, navItemIdle } from './layout/navStyles'

export const DOCS_URL = 'https://www.octoprox.com'
const REPO_URL = 'https://github.com/octoprox/octoprox'

interface DocLink { title: string; path: string }

/** The guide for each page, keyed by the last path segment. */
const PAGE_DOCS: Record<string, DocLink[]> = {
  overview: [{ title: 'Metrics and request counting', path: 'metrics' }, { title: 'Routing strategies', path: 'routing-strategies' }],
  hosts: [{ title: 'Hosts: where the traffic goes', path: 'metrics#hosts-where-the-traffic-goes' }],
  proxies: [{ title: 'Rate limiting and quarantine', path: 'rate-limiting' }, { title: 'Location routing', path: 'location-routing' }],
  connectors: [{ title: 'Providers and connectors', path: 'providers' }, { title: 'Traffic limits and billing', path: 'traffic-limits' }, { title: 'Domain filtering', path: 'domain-filtering' }],
  credentials: [{ title: 'Providers and credentials', path: 'providers' }],
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

const MENU_WIDTH = 272

export function HelpMenu({ collapsed }: { collapsed: boolean }) {
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState<{ left: number; bottom: number } | null>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  // Close on navigation and when the sidebar collapses or expands: the button
  // moves during the width transition, so the menu would be left behind.
  useEffect(() => { setOpen(false) }, [location.pathname, collapsed])

  // Place the menu beside the button, bottom aligned, before it paints, and
  // again whenever the window changes size while it is open.
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const rect = buttonRef.current?.getBoundingClientRect()
      if (rect) setAnchor({ left: rect.right + 8, bottom: Math.max(8, window.innerHeight - rect.bottom) })
    }
    place()
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onPointer = (e: MouseEvent) => {
      const t = e.target as Node
      if (menuRef.current?.contains(t) || buttonRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setOpen(false)
      buttonRef.current?.focus()
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const pageDocs = docsForPath(location.pathname)

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={collapsed ? 'Help' : undefined}
        aria-haspopup="menu"
        aria-expanded={open}
        className={cn(navItemBase, collapsed ? navItemCollapsed : cn(navItemExpanded, 'w-full'), open ? 'bg-surface-raised text-fg' : navItemIdle)}
      >
        <HelpCircle />
        {!collapsed && <span className="truncate">Help</span>}
      </button>

      {open && anchor && (
        <div
          ref={menuRef}
          role="menu"
          style={{ position: 'fixed', left: anchor.left, bottom: anchor.bottom, width: MENU_WIDTH }}
          className="z-50 bg-surface border border-line-strong rounded-lg shadow-lg py-1.5 text-[13px]"
        >
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
          <MenuLink href={REPO_URL} icon={<Github />}>Source on GitHub</MenuLink>
          <MenuLink href={`${REPO_URL}/issues`} icon={<Bug />}>Report a problem</MenuLink>
        </div>
      )}
    </>
  )
}

function MenuLabel({ children }: { children: ReactNode }) {
  return <div className="px-3 pt-1.5 pb-1 text-[11px] font-semibold uppercase tracking-[0.06em] text-fg-subtle">{children}</div>
}

function MenuDivider() {
  return <div className="h-px bg-line my-1.5" aria-hidden />
}

function MenuLink({ href, icon, children }: { href: string; icon: ReactNode; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      role="menuitem"
      className="flex items-center gap-2.5 px-3 h-8 text-fg-muted hover:bg-surface-raised hover:text-fg transition-colors [&>svg]:w-4 [&>svg]:h-4 [&>svg]:flex-none [&>svg]:text-fg-subtle"
    >
      {icon}
      <span className="truncate flex-1">{children}</span>
      <ExternalLink className="!w-3 !h-3 opacity-60" />
    </a>
  )
}
