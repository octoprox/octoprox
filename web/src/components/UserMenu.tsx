// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * The signed-in user's menu: their account, their appearance and signing out.
 * Opens from the avatar at the foot of the sidebar or in the projects page
 * header.
 */

import { type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { ChevronDown, ChevronsUpDown, LogOut, Palette, User as UserIcon } from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'
import { useNavigationGuardContext } from '../contexts/NavigationGuardContext'
import { setSettingsOrigin } from '../utils/settingsOrigin'
import { cn } from '../utils/cn'
import { MENU_GUTTER, PopoverMenu, besideSidebarFoot, usePopoverMenu } from './ui/PopoverMenu'

const MENU_WIDTH = 224

interface UserMenuProps {
  /** `sidebar`: the foot of the sidebar, opening beside it. `header`: a page header, opening below. */
  placement: 'sidebar' | 'header'
  collapsed?: boolean
  onSignOut: () => void
}

export function UserMenu({ placement, collapsed = false, onSignOut }: UserMenuProps) {
  const { authStatus } = useAuth()
  const location = useLocation()
  const routerNavigate = useNavigate()
  const guard = useNavigationGuardContext()
  const username = authStatus?.username ?? ''
  const role = authStatus?.role ?? ''
  // In a header below the button, right aligned; in the sidebar beside its foot.
  const menu = usePopoverMenu({
    moves: collapsed,
    place: (button) => placement === 'header'
      ? { top: button.bottom + 4, right: Math.max(MENU_GUTTER, window.innerWidth - button.right), width: Math.min(MENU_WIDTH, window.innerWidth - 2 * MENU_GUTTER) }
      : besideSidebarFoot(button, MENU_WIDTH, collapsed),
  })

  const go = (to: string) => {
    menu.close()
    guard.request(() => {
      setSettingsOrigin(location.pathname)
      routerNavigate(to)
    })
  }

  const avatar = (
    <span className="w-7 h-7 rounded-full bg-primary-soft text-primary-soft-fg flex items-center justify-center text-[11px] font-semibold flex-none uppercase">
      {(username || '?').slice(0, 2)}
    </span>
  )
  const nameAndRole = (
    <span className="flex-1 min-w-0 leading-tight text-left">
      <span className="block text-[13px] font-medium text-fg truncate">{username}</span>
      <span className="block text-[11px] text-fg-subtle">{role}</span>
    </span>
  )

  return (
    <>
      <button
        {...menu.buttonProps}
        type="button"
        title={collapsed ? `${username} (account)` : 'Your account'}
        aria-label={`Your account (${username})`}
        className={cn(
          'flex items-center gap-2.5 rounded-md transition-colors hover:bg-surface-raised',
          menu.open && 'bg-surface-raised',
          placement === 'header' ? 'p-1 pr-1.5' : collapsed ? 'justify-center w-10 h-10' : 'w-full px-1 py-1.5'
        )}
      >
        {avatar}
        {placement === 'sidebar' && !collapsed && (
          <>
            {nameAndRole}
            <ChevronsUpDown className="w-3.5 h-3.5 text-fg-subtle flex-none mr-1" />
          </>
        )}
        {placement === 'header' && (
          <>
            <span className="hidden sm:flex min-w-0">{nameAndRole}</span>
            <ChevronDown className="w-3.5 h-3.5 text-fg-subtle flex-none" />
          </>
        )}
      </button>

      <PopoverMenu state={menu} label="Your account" className="py-1 overflow-y-auto overscroll-contain">
        <div className="flex items-center gap-2.5 px-3 py-2">
          {avatar}
          {nameAndRole}
        </div>
        <div className="h-px bg-line my-1" aria-hidden />
        <MenuItem icon={<UserIcon />} onClick={() => go('/account')}>Account</MenuItem>
        <MenuItem icon={<Palette />} onClick={() => go('/account/appearance')}>Appearance</MenuItem>
        <div className="h-px bg-line my-1" aria-hidden />
        <MenuItem icon={<LogOut />} onClick={() => { menu.close(); onSignOut() }}>Sign out</MenuItem>
      </PopoverMenu>
    </>
  )
}

function MenuItem({ icon, onClick, children }: { icon: ReactNode; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className="w-full flex items-center gap-2.5 px-3 h-8 pointer-coarse:h-11 text-left text-fg-muted outline-none transition-colors hover:bg-surface-raised hover:text-fg focus-visible:bg-surface-raised [&>svg]:w-4 [&>svg]:h-4 [&>svg]:flex-none [&>svg]:text-fg-subtle"
    >
      {icon}
      <span className="truncate">{children}</span>
    </button>
  )
}
