// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Whether the server runs another release than this UI: every API response
 * names the server's release, and one that differs from the release this UI
 * was built from means a reload loads a new UI.
 */

import { useSyncExternalStore } from 'react'

export const VERSION_HEADER = 'X-Octoprox-Version'

let serverVersion: string | null = null
const listeners = new Set<() => void>()

/** Record the release an API response named. The dev server is left out: it reloads the UI by itself. */
export function noteServerVersion(version: unknown): void {
  if (import.meta.env.DEV || typeof version !== 'string' || !version || version === serverVersion) return
  serverVersion = version
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

function newVersion(): string | null {
  return serverVersion !== null && serverVersion !== __OCTOPROX_VERSION__ ? serverVersion : null
}

/** The server's release while it differs from this UI's, else null. */
export function useNewVersion(): string | null {
  return useSyncExternalStore(subscribe, newVersion)
}
