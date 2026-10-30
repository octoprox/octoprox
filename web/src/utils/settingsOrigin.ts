// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

// Tracks the page the user was on before entering Account or Administration,
// so the back button can return them there instead of stepping back through
// the pages they visited inside.

const ORIGIN_KEY = 'octoprox_settings_origin'

/** Account and Administration: the areas outside any project that Back leaves. Routes match without regard to case. */
const inArea = (path: string) => /^\/(account|admin)(\/|$)/i.test(path)

/** Remember `path` to go back to, unless it is inside an area itself: moving between areas keeps the first origin. */
export function setSettingsOrigin(path: string): void {
  if (inArea(path)) return
  try {
    sessionStorage.setItem(ORIGIN_KEY, path)
  } catch {
    // sessionStorage unavailable - back button will fall back to default.
  }
}

export function popSettingsOrigin(): string | null {
  try {
    const value = sessionStorage.getItem(ORIGIN_KEY)
    if (value) sessionStorage.removeItem(ORIGIN_KEY)
    return value
  } catch {
    return null
  }
}
