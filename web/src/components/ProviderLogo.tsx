// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useTheme } from '../contexts/ThemeContext'
import { useProviders } from '../hooks/useProviders'
import { providerInitials } from '../utils/credentials'
import { cn } from '../utils/cn'

/** Provider logo with an initials fallback so custom providers look at home next to shipped ones. */
export function ProviderLogo({ type, name, className }: { type: string | null | undefined; name?: string; className?: string }) {
  const { isDark } = useTheme()
  const { logoFor, labelFor } = useProviders()
  const src = logoFor(type, isDark)
  const label = name ?? labelFor(type)
  if (src) return <img src={src} alt="" className={cn('object-contain flex-none', className)} />
  return (
    <span
      className={cn('rounded-md bg-primary-soft text-primary-soft-fg flex items-center justify-center font-semibold flex-none text-[0.42em] leading-none', className)}
      aria-hidden
    >
      {providerInitials(label)}
    </span>
  )
}
