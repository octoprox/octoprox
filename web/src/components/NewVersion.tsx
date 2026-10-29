// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * A new release while the UI is open: a bar offers to load it now, and the
 * next move to another page loads it anyway, so an open form is never lost.
 */

import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router'
import { RefreshCw } from 'lucide-react'
import { useNewVersion } from '../utils/appVersion'
import { Button } from './ui'

/** A slim bar across the top of the page while the server runs another release. */
export function NewVersionBanner() {
  const version = useNewVersion()
  if (!version) return null
  return (
    <div
      role="status"
      className="flex-none flex flex-wrap items-center justify-center gap-x-3 gap-y-1 px-4 py-1.5 border-b border-primary/30 bg-primary-soft text-primary-soft-fg text-[13px]"
    >
      <span className="inline-flex items-center gap-2 min-w-0">
        <RefreshCw className="w-3.5 h-3.5 flex-none" />
        <span>
          <span className="font-medium">Octoprox {version} is ready.</span>
          <span className="max-sm:hidden"> The next page you open loads it.</span>
        </span>
      </span>
      <Button size="sm" onClick={() => window.location.reload()} className="h-auto px-2.5 py-0.5 rounded-md">
        Refresh
      </Button>
    </div>
  )
}

/** Loads the new release from the server, instead of the page in hand, when the user moves to another page. */
export function ReloadOnNewVersion() {
  const { pathname } = useLocation()
  const version = useNewVersion()
  const shown = useRef(pathname)
  useEffect(() => {
    if (pathname === shown.current) return
    shown.current = pathname
    if (version) window.location.reload()
  }, [pathname, version])
  return null
}
