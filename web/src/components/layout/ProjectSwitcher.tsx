// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Project dropdown at the top of the sidebar: switch project in place, open the
 * project's settings or go to the project list.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, FolderOpen, LayoutGrid, Search, Settings } from 'lucide-react'
import { fetchProjects, type ProjectSummary } from '../../api/client'
import { cn } from '../../utils/cn'
import { MENU_GUTTER, PopoverMenu, usePopoverMenu } from '../ui/PopoverMenu'

const MENU_WIDTH = 272
/** The filter box appears once the list is longer than this. */
const FILTER_ABOVE = 7

interface ProjectSwitcherProps {
  collapsed: boolean
  /** The project open now. */
  projectId?: string
  label: string
  onSwitch: (project: ProjectSummary) => void
  onAllProjects: () => void
  /** Shown only when set: inside a project, for users who may edit it. */
  onProjectSettings?: () => void
}

export function ProjectSwitcher({ collapsed, projectId, label, onSwitch, onAllProjects, onProjectSettings }: ProjectSwitcherProps) {
  const [filter, setFilter] = useState('')
  const filterRef = useRef<HTMLInputElement>(null)
  // Below the button in the open sidebar, beside it on the rail. The height follows the visual viewport,
  // so on a phone the bottom entries stay above the on-screen keyboard.
  const menu = usePopoverMenu({
    moves: collapsed,
    place: (button) => {
      const left = collapsed ? button.right + MENU_GUTTER : button.left
      const top = collapsed ? button.top : button.bottom + 4
      const viewHeight = window.visualViewport?.height ?? window.innerHeight
      return {
        left,
        top,
        width: Math.min(Math.max(MENU_WIDTH, collapsed ? 0 : button.width), window.innerWidth - left - MENU_GUTTER),
        maxHeight: Math.max(160, viewHeight - top - MENU_GUTTER),
      }
    },
    // The filter with a mouse, the current project otherwise, once the list is there (or failed). A touch screen never
    // gets the filter focused for it, since that would open the keyboard over the list.
    initialFocus: (list) => {
      if (!data && !error) return null
      if (showFilter && window.matchMedia('(pointer: fine)').matches) return filterRef.current
      return list.querySelector<HTMLElement>('[aria-checked="true"]') ?? list.querySelector<HTMLElement>('[role^="menuitem"]')
    },
  })
  const { open } = menu

  // Loaded when the menu opens; the project list page shares the cache.
  const { data, isLoading, error } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects, enabled: open, staleTime: 30_000 })
  const projects = data?.projects ?? []
  const showFilter = projects.length > FILTER_ABOVE
  const needle = filter.trim().toLowerCase()
  const shown = needle ? projects.filter((p) => p.name.toLowerCase().includes(needle)) : projects

  useEffect(() => { if (!open) setFilter('') }, [open])

  const pick = (project: ProjectSummary) => {
    menu.close()
    if (project.id !== projectId) onSwitch(project)
  }

  return (
    <>
      <button
        {...menu.buttonProps}
        type="button"
        title={collapsed ? `${label} (switch project)` : 'Switch project'}
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

      <PopoverMenu state={menu} label="Projects" className="flex flex-col overflow-hidden">
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
            <MenuAction icon={<Settings />} onClick={() => { menu.close(); onProjectSettings() }}>Project settings</MenuAction>
          )}
          <MenuAction icon={<LayoutGrid />} onClick={() => { menu.close(); onAllProjects() }}>All projects</MenuAction>
        </div>
      </PopoverMenu>
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
      onClick={onClick}
      className={cn(itemClasses, 'text-fg-muted hover:text-fg [&>svg]:w-4 [&>svg]:h-4 [&>svg]:flex-none [&>svg]:text-fg-subtle')}
    >
      {icon}
      <span className="truncate">{children}</span>
    </button>
  )
}
