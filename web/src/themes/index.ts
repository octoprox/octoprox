// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

// Keep the theme id set in sync with ALLOWED_THEMES in api/models/user.py
// and the .theme-* blocks in web/src/index.css.

export type ThemeId =
  | 'light'
  | 'dark'
  | 'solarized-light'
  | 'solarized-dark'
  | 'dracula'
  | 'nord'
  | 'high-contrast'

export type ThemeMode = 'light' | 'dark'

export interface ThemeMeta {
  id: ThemeId
  label: string
  mode: ThemeMode
  swatches: {
    bg: string
    surface: string
    primary: string
    fg: string
  }
}

export const THEMES: ThemeMeta[] = [
  {
    id: 'light',
    label: 'Light',
    mode: 'light',
    swatches: {
      bg: '#f9fafb',
      surface: '#ffffff',
      primary: '#2563eb',
      fg: '#111827',
    },
  },
  {
    id: 'dark',
    label: 'Dark',
    mode: 'dark',
    swatches: {
      bg: '#111827',
      surface: '#1f2937',
      primary: '#7c3aed',
      fg: '#f3f4f6',
    },
  },
  {
    id: 'solarized-light',
    label: 'Solarized Light',
    mode: 'light',
    swatches: {
      bg: '#fdf6e3',
      surface: '#eee8d5',
      primary: '#268bd2',
      fg: '#657b83',
    },
  },
  {
    id: 'solarized-dark',
    label: 'Solarized Dark',
    mode: 'dark',
    swatches: {
      bg: '#002b36',
      surface: '#073642',
      primary: '#268bd2',
      fg: '#839496',
    },
  },
  {
    id: 'dracula',
    label: 'Dracula',
    mode: 'dark',
    swatches: {
      bg: '#282a36',
      surface: '#44475a',
      primary: '#bd93f9',
      fg: '#f8f8f2',
    },
  },
  {
    id: 'nord',
    label: 'Nord',
    mode: 'dark',
    swatches: {
      bg: '#2e3440',
      surface: '#3b4252',
      primary: '#88c0d0',
      fg: '#eceff4',
    },
  },
  {
    id: 'high-contrast',
    label: 'High Contrast',
    mode: 'dark',
    swatches: {
      bg: '#000000',
      surface: '#141414',
      primary: '#ffd400',
      fg: '#ffffff',
    },
  },
]

export const DEFAULT_THEME: ThemeId = 'light'

const THEME_IDS = new Set<string>(THEMES.map((t) => t.id))

export function isValidTheme(value: unknown): value is ThemeId {
  return typeof value === 'string' && THEME_IDS.has(value)
}

export function getThemeMeta(id: ThemeId): ThemeMeta {
  return THEMES.find((t) => t.id === id) ?? THEMES[0]
}
