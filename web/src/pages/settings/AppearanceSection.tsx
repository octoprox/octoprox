// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Check } from 'lucide-react'
import { useTheme } from '../../contexts/ThemeContext'
import { Page } from '../../components/layout/Page'
import { Card } from '../../components/ui'
import { cn } from '../../utils/cn'

export default function AppearanceSection() {
  const { theme, setTheme, availableThemes } = useTheme()

  return (
    <Page title="Appearance" subtitle="Theme is saved to your account and follows you across devices.">
      <Card className="p-5 max-w-3xl">
        <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-3">
          {availableThemes.map((t) => {
            const selected = t.id === theme
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => setTheme(t.id)}
                aria-pressed={selected}
                className={cn(
                  'flex flex-col gap-2 p-2.5 rounded-[10px] border text-left transition-colors',
                  selected ? 'border-primary ring-[3px] ring-primary-soft' : 'border-line hover:border-line-strong'
                )}
              >
                <div className="flex h-9 rounded-md overflow-hidden border border-line">
                  <span style={{ background: t.swatches.bg }} className="flex-1" />
                  <span style={{ background: t.swatches.surface }} className="flex-1" />
                  <span style={{ background: t.swatches.primary }} className="flex-1" />
                  <span style={{ background: t.swatches.fg }} className="flex-1" />
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[12.5px] font-medium text-fg">{t.label}</span>
                  {selected && <Check className="w-3.5 h-3.5 text-primary" />}
                </div>
              </button>
            )
          })}
        </div>
      </Card>
    </Page>
  )
}
