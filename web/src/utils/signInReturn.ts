// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

// The page a sign-in through a provider started from, so a shared link opened while signed out still lands on its
// view: the provider's round trip always ends at /sso/complete, and a refused one at /. Kept in sessionStorage, which
// the same-tab round trip keeps and no other tab sees.

const RETURN_KEY = 'octoprox_sign_in_return'

/** An address inside this app: one leading slash, so never another host ('//evil', '/\evil'). */
const isAppPath = (path: string) => path.startsWith('/') && !path.startsWith('//') && !path.startsWith('/\\')

/** Remember `path` to come back to after the provider; the project list needs no remembering. */
export function saveSignInReturn(path: string): void {
  try {
    if (path === '/' || !isAppPath(path)) sessionStorage.removeItem(RETURN_KEY)
    else sessionStorage.setItem(RETURN_KEY, path)
  } catch {
    // sessionStorage unavailable: the sign-in lands on the project list.
  }
}

/** The remembered page, if any, kept for a retry after a refused sign-in. */
export function peekSignInReturn(): string | null {
  try {
    const path = sessionStorage.getItem(RETURN_KEY)
    return path && isAppPath(path) ? path : null
  } catch {
    return null
  }
}

/** The remembered page, if any, forgotten once read. */
export function takeSignInReturn(): string | null {
  const path = peekSignInReturn()
  try { sessionStorage.removeItem(RETURN_KEY) } catch { /* ignore */ }
  return path
}
