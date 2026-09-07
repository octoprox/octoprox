// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ConfirmDialog } from '../components/ui'

/**
 * In-app navigation guard. `BrowserRouter` has no blocker API, so editors with
 * unsaved work register here and the shell's links/buttons ask before leaving.
 * Browser reloads are covered separately by each editor's beforeunload handler.
 */
interface NavigationGuardValue {
  /** True while some editor has unsaved work. */
  blocked: boolean
  /** Run `proceed` now, or after the user confirms leaving when blocked. */
  request: (proceed: () => void) => void
  /** Register/unregister a guard. Only one editor is expected to be dirty at a time. */
  setGuard: (active: boolean, message?: string) => void
}

const NavigationGuardContext = createContext<NavigationGuardValue>({ blocked: false, request: (p) => p(), setGuard: () => {} })

const DEFAULT_MESSAGE = 'You have unsaved changes. Leaving this page discards what is on screen.'

export function NavigationGuardProvider({ children }: { children: ReactNode }) {
  const [blocked, setBlocked] = useState(false)
  const [message, setMessage] = useState(DEFAULT_MESSAGE)
  const pending = useRef<(() => void) | null>(null)
  const [open, setOpen] = useState(false)

  const setGuard = useCallback((active: boolean, msg?: string) => {
    setBlocked(active)
    if (msg) setMessage(msg)
  }, [])
  const request = useCallback((proceed: () => void) => {
    if (!blocked) { proceed(); return }
    pending.current = proceed
    setOpen(true)
  }, [blocked])

  const value = useMemo(() => ({ blocked, request, setGuard }), [blocked, request, setGuard])
  return (
    <NavigationGuardContext.Provider value={value}>
      {children}
      {open && (
        <ConfirmDialog
          title="Leave without saving?"
          message={message}
          confirmLabel="Leave"
          onCancel={() => { pending.current = null; setOpen(false) }}
          onConfirm={() => { const go = pending.current; pending.current = null; setOpen(false); setBlocked(false); go?.() }}
        />
      )}
    </NavigationGuardContext.Provider>
  )
}

export function useNavigationGuardContext() {
  return useContext(NavigationGuardContext)
}

/** Register a guard while `active`; cleared automatically on unmount. */
export function useNavigationGuard(active: boolean, message?: string) {
  const { setGuard } = useNavigationGuardContext()
  useEffect(() => { setGuard(active, message) }, [active, message, setGuard])
  useEffect(() => () => setGuard(false), [setGuard])
}
