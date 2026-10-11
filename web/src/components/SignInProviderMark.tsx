// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { AuthProviderKind } from '../api/client'

/**
 * A small monochrome mark per sign-in provider, drawn inline so the login
 * page needs no image fetch before it can show its buttons. Monochrome on
 * purpose: it takes the text colour, so it reads in every theme.
 */
export function ProviderMark({ kind, className }: { kind: AuthProviderKind; className?: string }) {
  switch (kind) {
    case 'google':
      return (
        <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden="true">
          <path d="M21.6 12.23c0-.68-.06-1.33-.17-1.96H12v3.7h5.38a4.6 4.6 0 0 1-2 3.02v2.5h3.24c1.9-1.75 2.98-4.32 2.98-7.26z" />
          <path d="M12 21.75c2.7 0 4.96-.9 6.62-2.42l-3.24-2.5c-.9.6-2.04.96-3.38.96-2.6 0-4.8-1.76-5.59-4.12H3.07v2.58A9.99 9.99 0 0 0 12 21.75z" />
          <path d="M6.41 13.67a6 6 0 0 1 0-3.84V7.25H3.07a10 10 0 0 0 0 8.99l3.34-2.57z" />
          <path d="M12 6.21c1.47 0 2.79.5 3.83 1.5l2.87-2.87C16.96 3.22 14.7 2.25 12 2.25a9.99 9.99 0 0 0-8.93 5l3.34 2.58C7.2 7.97 9.4 6.21 12 6.21z" />
        </svg>
      )
    case 'microsoft':
      return (
        <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden="true">
          <path d="M3 3h8.5v8.5H3zM12.5 3H21v8.5h-8.5zM3 12.5h8.5V21H3zM12.5 12.5H21V21h-8.5z" />
        </svg>
      )
    case 'github':
      return (
        <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden="true">
          <path d="M12 2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.45-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.6.07-.6 1 .07 1.53 1.03 1.53 1.03.9 1.52 2.34 1.08 2.91.83.09-.65.35-1.09.63-1.34-2.22-.25-4.56-1.11-4.56-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.65 0 0 .84-.27 2.75 1.02a9.6 9.6 0 0 1 5 0c1.91-1.29 2.75-1.02 2.75-1.02.55 1.38.2 2.4.1 2.65.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.85v2.74c0 .27.18.58.69.48A10 10 0 0 0 12 2z" />
        </svg>
      )
  }
}
