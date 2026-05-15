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
      bg: 'rgb(249 250 251)',
      surface: 'rgb(255 255 255)',
      primary: 'rgb(37 99 235)',
      fg: 'rgb(17 24 39)',
    },
  },
  {
    id: 'dark',
    label: 'Dark',
    mode: 'dark',
    swatches: {
      bg: 'rgb(17 24 39)',
      surface: 'rgb(31 41 55)',
      primary: 'rgb(124 58 237)',
      fg: 'rgb(243 244 246)',
    },
  },
  {
    id: 'solarized-light',
    label: 'Solarized Light',
    mode: 'light',
    swatches: {
      bg: 'rgb(253 246 227)',
      surface: 'rgb(238 232 213)',
      primary: 'rgb(38 139 210)',
      fg: 'rgb(101 123 131)',
    },
  },
  {
    id: 'solarized-dark',
    label: 'Solarized Dark',
    mode: 'dark',
    swatches: {
      bg: 'rgb(0 43 54)',
      surface: 'rgb(7 54 66)',
      primary: 'rgb(38 139 210)',
      fg: 'rgb(131 148 150)',
    },
  },
  {
    id: 'dracula',
    label: 'Dracula',
    mode: 'dark',
    swatches: {
      bg: 'rgb(40 42 54)',
      surface: 'rgb(68 71 90)',
      primary: 'rgb(189 147 249)',
      fg: 'rgb(248 248 242)',
    },
  },
  {
    id: 'nord',
    label: 'Nord',
    mode: 'dark',
    swatches: {
      bg: 'rgb(46 52 64)',
      surface: 'rgb(59 66 82)',
      primary: 'rgb(136 192 208)',
      fg: 'rgb(236 239 244)',
    },
  },
  {
    id: 'high-contrast',
    label: 'High Contrast',
    mode: 'dark',
    swatches: {
      bg: 'rgb(0 0 0)',
      surface: 'rgb(20 20 20)',
      primary: 'rgb(255 212 0)',
      fg: 'rgb(255 255 255)',
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
