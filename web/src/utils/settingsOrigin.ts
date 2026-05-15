// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

// Tracks the page the user was on before entering Settings, so the back
// button can return them there instead of stepping back through the
// settings sub-pages they visited.

const ORIGIN_KEY = 'octoprox_settings_origin'

export function setSettingsOrigin(path: string): void {
  try {
    sessionStorage.setItem(ORIGIN_KEY, path)
  } catch {
    // sessionStorage unavailable — back button will fall back to default.
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
