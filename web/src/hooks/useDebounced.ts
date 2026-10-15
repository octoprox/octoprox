// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'

/** `value`, once it has stopped changing for `delayMs`: waits for typing to pause before a query uses it. */
export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(handle)
  }, [value, delayMs])
  return debounced
}
