// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Check } from 'lucide-react'
import { useTheme } from '../../contexts/ThemeContext'
import { Card } from '../../components/ui'
import { cn } from '../../utils/cn'

export default function AppearanceSection() {
  const { theme, setTheme, availableThemes } = useTheme()

  return (
    <div className="space-y-6">
      <header>
        <h2 className="text-xl font-semibold text-fg">Appearance</h2>
        <p className="text-sm text-fg-muted mt-1">
          Pick a theme. Saved automatically and synced across your devices.
        </p>
      </header>

      <Card className="p-6">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {availableThemes.map((t) => {
            const selected = t.id === theme
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => setTheme(t.id)}
                aria-pressed={selected}
                className={cn(
                  'group flex flex-col gap-3 p-3 rounded-lg border-2 text-left transition-colors',
                  selected
                    ? 'border-primary bg-primary/5'
                    : 'border-line hover:border-line-strong'
                )}
              >
                <div className="flex h-10 rounded overflow-hidden border border-line">
                  <span style={{ background: t.swatches.bg }} className="flex-1" />
                  <span style={{ background: t.swatches.surface }} className="flex-1" />
                  <span style={{ background: t.swatches.primary }} className="flex-1" />
                  <span style={{ background: t.swatches.fg }} className="flex-1" />
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-fg">{t.label}</span>
                  {selected && <Check className="w-4 h-4 text-primary" />}
                </div>
              </button>
            )
          })}
        </div>
      </Card>
    </div>
  )
}
