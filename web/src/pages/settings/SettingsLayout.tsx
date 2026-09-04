// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { ArrowLeft, DatabaseBackup, Palette, User as UserIcon, Users as UsersIcon } from 'lucide-react'
import { useAuth } from '../../contexts/AuthContext'
import { cn } from '../../utils/cn'
import { popSettingsOrigin } from '../../utils/settingsOrigin'

export default function SettingsLayout() {
  const { isAdmin } = useAuth()
  const navigate = useNavigate()

  const handleBack = () => {
    const from = popSettingsOrigin()
    navigate(from || '/')
  }

  const items: { to: string; icon: React.ReactNode; label: string; show: boolean }[] = [
    { to: 'account', icon: <UserIcon className="w-4 h-4" />, label: 'Account', show: true },
    { to: 'appearance', icon: <Palette className="w-4 h-4" />, label: 'Appearance', show: true },
    { to: 'users', icon: <UsersIcon className="w-4 h-4" />, label: 'Users', show: isAdmin },
    { to: 'backup', icon: <DatabaseBackup className="w-4 h-4" />, label: 'Backup & Migration', show: isAdmin },
  ]

  return (
    <div className="min-h-screen bg-bg">
      <div className="max-w-6xl mx-auto p-8">
        <div className="flex items-center gap-3 mb-6">
          <button
            onClick={handleBack}
            className="p-2 text-fg-muted hover:text-fg hover:bg-surface-raised rounded-lg transition-colors"
            title="Back"
          >
            <ArrowLeft className="w-5 h-5" />
          </button>
          <h1 className="text-2xl font-semibold text-fg">Settings</h1>
        </div>

        <div className="flex gap-8">
          <nav className="w-56 shrink-0">
            <ul className="space-y-1">
              {items.filter((i) => i.show).map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    className={({ isActive }) =>
                      cn(
                        'flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors',
                        isActive
                          ? 'bg-primary/10 text-primary'
                          : 'text-fg-muted hover:text-fg hover:bg-surface-raised'
                      )
                    }
                  >
                    {item.icon}
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>

          <main className="flex-1 min-w-0">
            <Outlet />
          </main>
        </div>
      </div>
    </div>
  )
}
