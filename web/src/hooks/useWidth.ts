// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useLayoutEffect, useRef, useState, type RefObject } from 'react'

/** An element's width, kept current as it resizes; measured before the first paint. */
export function useWidth(): [RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const read = () => setWidth(el.clientWidth)
    read()
    const observer = new ResizeObserver(read)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return [ref, width]
}
