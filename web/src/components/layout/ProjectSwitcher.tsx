// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Project dropdown at the top of the sidebar: switch project in place, open the
 * project's settings or go to the project list. Rendered fixed in a portal so
 * neither the collapsed rail nor the phone overlay clips it.
 */

import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useLocation } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, FolderOpen, LayoutGrid, Search, Settings } from 'lucide-react'
import { fetchProjects, type ProjectSummary } from '../../api/client'
import { cn } from '../../utils/cn'

const MENU_WIDTH = 272
/** Space kept between the menu and the edges of the screen (or the on-screen keyboard). */
const GUTTER = 8
/** The filter box appears once the list is longer than this. */
const FILTER_ABOVE = 7

interface Placement { left: number; top: number; width: number; maxHeight: number }

interface ProjectSwitcherProps {
  collapsed: boolean
  /** The project open now; none in global settings. */
  projectId?: string
  label: string
  onSwitch: (project: ProjectSummary) => void
  onAllProjects: () => void
  /** Shown only when set: inside a project, for users who may edit it. */
  onProjectSettings?: () => void
}

export function ProjectSwitcher({ collapsed, projectId, label, onSwitch, onAllProjects, onProjectSettings }: ProjectSwitcherProps) {
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const [placement, setPlacement] = useState<Placement | null>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const backdropRef = useRef<HTMLDivElement>(null)
  const filterRef = useRef<HTMLInputElement>(null)

  // Loaded when the menu opens; the project list page shares the cache.
  const { data, isLoading, error } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects, enabled: open, staleTime: 30_000 })
  const projects = data?.projects ?? []
  const showFilter = projects.length > FILTER_ABOVE
  const needle = filter.trim().toLowerCase()
  const shown = needle ? projects.filter((p) => p.name.toLowerCase().includes(needle)) : projects

  // Close on navigation and when the sidebar collapses or expands: the button moves, so the menu would be left behind.
  useEffect(() => { setOpen(false) }, [location.pathname, collapsed])
  useEffect(() => { if (!open) setFilter('') }, [open])

  // Below the button in the open sidebar, beside it on the rail. The height follows the visual viewport,
  // so on a phone the bottom entries stay above the on-screen keyboard.
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const rect = buttonRef.current?.getBoundingClientRect()
      if (!rect) return
      const left = collapsed ? rect.right + GUTTER : rect.left
      const top = collapsed ? rect.top : rect.bottom + 4
      const viewHeight = window.visualViewport?.height ?? window.innerHeight
      setPlacement({
        left,
        top,
        width: Math.min(Math.max(MENU_WIDTH, collapsed ? 0 : rect.width), window.innerWidth - left - GUTTER),
        maxHeight: Math.max(160, viewHeight - top - GUTTER),
      })
    }
    place()
    window.addEventListener('resize', place)
    window.visualViewport?.addEventListener('resize', place)
    return () => {
      window.removeEventListener('resize', place)
      window.visualViewport?.removeEventListener('resize', place)
    }
  }, [open, collapsed])

  useEffect(() => {
    if (!open) return
    const onPointer = (e: PointerEvent) => {
      const t = e.target as Node
      if (menuRef.current?.contains(t) || buttonRef.current?.contains(t) || t === backdropRef.current) return
      setOpen(false)
    }
    document.addEventListener('pointerdown', onPointer)
    return () => document.removeEventListener('pointerdown', onPointer)
  }, [open])

  const items = () => Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[data-menu-item]') ?? [])

  // Focus moves into the menu once the list is there (or failed): the filter with a mouse, the current project otherwise.
  // A touch screen never gets the filter focused for it, since that would open the keyboard over the list.
  const focused = useRef(false)
  useEffect(() => {
    if (!open) { focused.current = false; return }
    if (focused.current || !placement || (!data && !error)) return
    const target = showFilter && window.matchMedia('(pointer: fine)').matches
      ? filterRef.current
      : menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]') ?? items()[0]
    if (target) { target.focus(); focused.current = true }
  }, [open, placement, data, error, showFilter])

  const closeToButton = () => {
    setOpen(false)
    buttonRef.current?.focus()
  }

  const onMenuKey = (e: KeyboardEvent) => {
    // The menu sits at the end of the page, so Tab would leave the sidebar behind: it closes like Escape.
    if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); closeToButton(); return }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
    if ((e.key === 'Home' || e.key === 'End') && e.target === filterRef.current) return
    e.preventDefault()
    const list = items()
    if (list.length === 0) return
    const at = list.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'Home' ? 0
      : e.key === 'End' ? list.length - 1
      : e.key === 'ArrowDown' ? (at + 1) % list.length
      : at <= 0 ? list.length - 1 : at - 1
    list[next].focus()
  }

  const pick = (project: ProjectSummary) => {
    setOpen(false)
    if (project.id !== projectId) onSwitch(project)
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={collapsed ? `${label} (switch project)` : 'Switch project'}
        aria-haspopup="menu"
        aria-expanded={open}
        className={cn(
          'flex items-center gap-2 text-[13px] font-medium text-fg hover:bg-surface-raised transition-colors flex-none',
          open && 'bg-surface-raised',
          collapsed ? 'mx-2 my-1 h-9 justify-center rounded-md' : 'mx-3 my-1 h-[34px] px-2.5 rounded-lg border border-line'
        )}
      >
        <FolderOpen className="w-4 h-4 text-fg-subtle flex-none" />
        {!collapsed && (
          <>
            <span className="truncate flex-1 text-left">{label}</span>
            <ChevronsUpDown className="w-3.5 h-3.5 text-fg-subtle flex-none" />
          </>
        )}
      </button>

      {open && placement && createPortal(
        <>
        {/* Touch screens: a tap beside the menu only closes it and never presses what is underneath. */}
        <div ref={backdropRef} onClick={() => setOpen(false)} className="fixed inset-0 z-50 pointer-fine:hidden" aria-hidden />
        <div
          ref={menuRef}
          role="menu"
          aria-label="Projects"
          onKeyDown={onMenuKey}
          style={{ position: 'fixed', left: placement.left, top: placement.top, width: placement.width, maxHeight: placement.maxHeight }}
          className="z-50 flex flex-col bg-surface text-fg border border-line-strong rounded-lg shadow-lg text-[13px] overflow-hidden"
        >
          {showFilter && (
            <div className="flex-none flex items-center gap-2 px-3 border-b border-line">
              <Search className="w-3.5 h-3.5 text-fg-subtle flex-none" />
              {/* 16px on touch screens: iOS zooms the page into any smaller input that takes focus. */}
              <input
                ref={filterRef}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && needle && shown[0]) { e.preventDefault(); pick(shown[0]) } }}
                placeholder="Find a project"
                aria-label="Find a project"
                className="flex-1 min-w-0 h-10 bg-transparent outline-none text-base pointer-fine:text-[13px] placeholder:text-fg-subtle"
              />
            </div>
          )}

          <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain py-1">
            {isLoading ? (
              <div className="px-3 py-2 text-fg-muted">Loading projects…</div>
            ) : error && !data ? (
              <div className="px-3 py-2 text-danger">Failed to load projects</div>
            ) : shown.length === 0 ? (
              <div className="px-3 py-2 text-fg-muted">{needle ? 'No project matches' : 'No projects yet'}</div>
            ) : shown.map((p) => (
              <ProjectItem key={p.id} project={p} current={p.id === projectId} onPick={() => pick(p)} />
            ))}
          </div>

          <div className="flex-none border-t border-line py-1">
            {onProjectSettings && (
              <MenuAction icon={<Settings />} onClick={() => { setOpen(false); onProjectSettings() }}>Project settings</MenuAction>
            )}
            <MenuAction icon={<LayoutGrid />} onClick={() => { setOpen(false); onAllProjects() }}>All projects</MenuAction>
          </div>
        </div>
        </>,
        document.body
      )}
    </>
  )
}

const itemClasses = 'w-full flex items-center gap-2.5 px-3 h-8 pointer-coarse:h-11 text-left outline-none transition-colors hover:bg-surface-raised focus-visible:bg-surface-raised'

function ProjectItem({ project, current, onPick }: { project: ProjectSummary; current: boolean; onPick: () => void }) {
  const healthy = project.healthy_proxy_count
  const total = project.proxy_count
  return (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={current}
      data-menu-item
      onClick={onPick}
      className={cn(itemClasses, current ? 'text-fg font-medium' : 'text-fg-muted hover:text-fg')}
    >
      <Check className={cn('w-3.5 h-3.5 flex-none text-primary', !current && 'invisible')} />
      <span className="truncate flex-1" title={project.name}>{project.name}</span>
      <span className="flex-none inline-flex items-center gap-1.5 text-xs text-fg-subtle tabular-nums" title={`${healthy} of ${total} proxies healthy`}>
        <span className={cn('w-2 h-2 rounded-full', total === 0 ? 'bg-fg-subtle/50' : healthy === total ? 'bg-success' : healthy === 0 ? 'bg-danger' : 'bg-warning')} />
        {healthy}/{total}
      </span>
    </button>
  )
}

function MenuAction({ icon, onClick, children }: { icon: ReactNode; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      role="menuitem"
      data-menu-item
      onClick={onClick}
      className={cn(itemClasses, 'text-fg-muted hover:text-fg [&>svg]:w-4 [&>svg]:h-4 [&>svg]:flex-none [&>svg]:text-fg-subtle')}
    >
      {icon}
      <span className="truncate">{children}</span>
    </button>
  )
}
