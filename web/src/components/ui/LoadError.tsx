// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { cn } from '../../utils/cn'
import { Alert } from './Alert'
import { Button } from './Button'
import { Reloading } from './Reloading'

/** The parts of a query that tell a failed load and ask again. */
export interface LoadQuery {
  data: unknown
  error: Error | null
  isLoadingError: boolean
  isFetchedAfterMount: boolean
  refetch: () => Promise<unknown>
}

/**
 * The load failed and there is nothing to show. True from a failure seen here until data arrives, through any
 * retry, even one paused offline: a query with no data drops its error when it fetches again, and would otherwise
 * swap the message for its loader. A failure from an earlier visit is not one: its fresh load shows the loader.
 */
export function loadFailed(query: LoadQuery): boolean {
  return query.data === undefined && (query.isLoadingError || query.isFetchedAfterMount)
}

/** A load that failed, in place of what it would have shown: the server's reason and a way to ask again. */
export function LoadError({ what, query, className }: { what: string; query: LoadQuery; className?: string }) {
  // The last reason stays up while a retry is out, since the query has dropped it meanwhile.
  const [lastError, setLastError] = useState(query.error)
  if (query.error && query.error !== lastError) setLastError(query.error)
  const error = query.error ?? lastError
  // Only a retry asked for here dims the message; the query's own polling goes on quietly.
  const [retrying, setRetrying] = useState(false)
  const retry = async () => {
    setRetrying(true)
    await query.refetch()
    setRetrying(false)
  }
  // A request ID wraps on a phone, and the retry is a 44 px target on touch.
  return (
    <Reloading active={retrying} size="sm">
      <Alert variant="error" role="alert" className={cn('flex items-center justify-between gap-x-3 gap-y-2 flex-wrap', className)}>
        <span className="min-w-0 wrap-break-word">Could not load {what}{error?.message ? `: ${error.message}` : ''}.</span>
        <Button type="button" size="sm" variant="outline" onClick={retry} className="flex-none pointer-coarse:h-11">Try again</Button>
      </Alert>
    </Reloading>
  )
}
