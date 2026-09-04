// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { createContext, useContext, useState, useEffect, useCallback, useRef, ReactNode } from 'react'
import {
  THEMES,
  ThemeId,
  getThemeMeta,
  isValidTheme,
} from '../themes'
import { updateSelf } from '../api/client'

interface ThemeContextType {
  theme: ThemeId
  setTheme: (id: ThemeId) => void
  applyServerTheme: (id: ThemeId | null | undefined) => void
  isDark: boolean
  availableThemes: typeof THEMES
}

const THEME_KEY = 'octoprox_theme'

const ThemeContext = createContext<ThemeContextType | undefined>(undefined)

function readInitialTheme(): ThemeId {
  const stored = localStorage.getItem(THEME_KEY)
  if (isValidTheme(stored)) return stored
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
  return prefersDark ? 'dark' : 'light'
}

function applyToDocument(id: ThemeId) {
  const meta = getThemeMeta(id)
  const root = document.documentElement
  // Remove any existing theme-* classes.
  Array.from(root.classList)
    .filter((c) => c.startsWith('theme-'))
    .forEach((c) => root.classList.remove(c))
  root.classList.add(`theme-${id}`)
  if (meta.mode === 'dark') {
    root.classList.add('dark')
  } else {
    root.classList.remove('dark')
  }
  root.setAttribute('data-theme-mode', meta.mode)
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeId>(readInitialTheme)
  // Track whether the last applied theme came from the server so we don't echo
  // it back. Also use it to suppress the initial mount PATCH (no-op anyway).
  const skipNextSync = useRef(true)
  // Last value pushed (or received from) the server, so re-running effects
  // (StrictMode, remounts) never repeat the same PATCH.
  const lastSynced = useRef<ThemeId | null>(null)

  useEffect(() => {
    applyToDocument(theme)
    localStorage.setItem(THEME_KEY, theme)

    if (skipNextSync.current) {
      skipNextSync.current = false
      lastSynced.current = theme
      return
    }
    if (lastSynced.current === theme) return
    lastSynced.current = theme

    // Best-effort sync to server. Silently ignore failures (e.g. unauthenticated).
    updateSelf({ theme_preference: theme }).catch((err) => {
      // eslint-disable-next-line no-console
      console.debug('Theme sync failed', err)
    })
  }, [theme])

  const setTheme = useCallback((id: ThemeId) => {
    setThemeState(id)
  }, [])

  const applyServerTheme = useCallback((id: ThemeId | null | undefined) => {
    if (!isValidTheme(id)) return
    setThemeState((current) => {
      if (current === id) return current
      skipNextSync.current = true
      return id
    })
  }, [])

  const isDark = getThemeMeta(theme).mode === 'dark'

  return (
    <ThemeContext.Provider value={{ theme, setTheme, applyServerTheme, isDark, availableThemes: THEMES }}>
      {children}
    </ThemeContext.Provider>
  )
}

export function useTheme() {
  const context = useContext(ThemeContext)
  if (context === undefined) {
    throw new Error('useTheme must be used within a ThemeProvider')
  }
  return context
}
