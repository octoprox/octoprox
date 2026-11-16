// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useLayoutEffect, useRef, useState, type RefObject } from 'react'

export interface ScrollEdges { above: boolean; below: boolean }

/**
 * Whether a scroll box has content hidden above or below it, kept current as it scrolls, resizes or its content
 * changes. The box holds its content in one element, whose size changes as entries come and go.
 */
export function useScrollEdges<T extends HTMLElement>(): [RefObject<T | null>, ScrollEdges] {
  const ref = useRef<T>(null)
  const [edges, setEdges] = useState<ScrollEdges>({ above: false, below: false })
  useLayoutEffect(() => {
    const box = ref.current
    if (!box) return
    // A pixel of slack: a zoomed page scrolls to fractional offsets that never quite reach the end.
    const read = () => {
      const above = box.scrollTop > 1
      const below = box.scrollTop + box.clientHeight < box.scrollHeight - 1
      setEdges((e) => (e.above === above && e.below === below ? e : { above, below }))
    }
    read()
    box.addEventListener('scroll', read, { passive: true })
    const observer = new ResizeObserver(read)
    observer.observe(box)
    if (box.firstElementChild) observer.observe(box.firstElementChild)
    return () => {
      box.removeEventListener('scroll', read)
      observer.disconnect()
    }
  }, [])
  return [ref, edges]
}
