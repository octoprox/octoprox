// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import octoproxLogoOnly from '../../assets/logos/octoprox_logo_only.svg'
import octoproxLogoOnlyDark from '../../assets/logos/octoprox_logo_only_dark.svg'
import { useTheme } from '../../contexts/ThemeContext'
import { cn } from '../../utils/cn'

const SIZES = { md: 'size-12', lg: 'size-20' }

// The build prefixes mask properties in the stylesheet for Chromium before 120; an inline style needs both by hand.
const LOGO_SHAPE = { maskImage: `url(${octoproxLogoOnly})`, WebkitMaskImage: `url(${octoproxLogoOnly})` }

interface LogoLoaderProps {
  size?: keyof typeof SIZES
  /** Read out by screen readers; the logo is the only visible sign of loading. */
  label?: string
  className?: string
}

/** The logo in grey with its colour flowing down from the head to the tentacle tips and draining again, while a page or the app loads. */
export function LogoLoader({ size = 'md', label = 'Loading…', className }: LogoLoaderProps) {
  const { isDark } = useTheme()
  return (
    <div role="status" className={cn('flex justify-center', className)}>
      <div className={cn('relative animate-logo-loader-in', SIZES[size])}>
        {/* The logo's shape in a flat grey, so it suits every theme. */}
        <div className="absolute inset-0 bg-line-strong mask-contain mask-center mask-no-repeat" style={LOGO_SHAPE} />
        <img src={isDark ? octoproxLogoOnlyDark : octoproxLogoOnly} alt="" className="absolute inset-0 size-full object-contain animate-logo-loader-flow" />
      </div>
      <span className="sr-only">{label}</span>
    </div>
  )
}
