// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from 'react'
import { LoadError, loadFailed, type LoadQuery } from './LoadError'
import { LogoLoader } from './LogoLoader'

interface QueryStatusProps {
  query: LoadQuery
  /** What failed to load, as it reads after "Could not load": "the connectors". */
  what: string
  /** Shown while the first answer is on its way; the page-sized loader by default. */
  loader?: ReactNode
}

/** What a query shows before it has data: the failure and a retry once its load has failed, else the loader. */
export function QueryStatus({ query, what, loader = <LogoLoader className="py-10" /> }: QueryStatusProps) {
  return loadFailed(query) ? <LoadError what={what} query={query} /> : loader
}

interface QueryContentProps<T> extends QueryStatusProps {
  query: LoadQuery & { data: T | undefined }
  /** The content once the data is in: as a function when it reads the data itself. */
  children: ReactNode | ((data: T) => ReactNode)
}

/** A query's content once it has data, and its loader or failure before that, so every page checks them in one order. */
export function QueryContent<T>({ query, what, loader, children }: QueryContentProps<T>) {
  if (query.data === undefined) return <QueryStatus query={query} what={what} loader={loader} />
  return typeof children === 'function' ? children(query.data) : children
}
