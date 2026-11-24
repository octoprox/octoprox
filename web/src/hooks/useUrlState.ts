// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * View state kept in the address bar's query, so a copied link opens the same tab, panel or filter.
 *
 * Every write replaces the history entry: Back leaves the page, as it always has. A write starts from the live
 * address rather than the query a component rendered with, so several components writing in one render keep each
 * other's changes, and a write that changes nothing is skipped (a replace still makes a new location).
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { useToast } from '../contexts/ToastContext'

/** Query keys to set (a value) or remove (null, undefined or ''). */
export type UrlPatch = Record<string, string | null | undefined>

/** The writer for the address bar's query: applies `patch` to the live query, replacing the history entry. */
export function useSetUrlParams(): (patch: UrlPatch) => void {
  const navigate = useNavigate()
  return useCallback((patch: UrlPatch) => {
    const current = new URLSearchParams(window.location.search)
    const next = new URLSearchParams(current)
    for (const [key, value] of Object.entries(patch)) {
      if (value === null || value === undefined || value === '') next.delete(key)
      else next.set(key, value)
    }
    const search = next.toString()
    if (search === current.toString()) return
    navigate({ pathname: window.location.pathname, search: search ? `?${search}` : '', hash: window.location.hash }, { replace: true })
  }, [navigate])
}

/** The address bar's query and its writer. */
export function useUrlParams(): [URLSearchParams, (patch: UrlPatch) => void] {
  const { search } = useLocation()
  const params = useMemo(() => new URLSearchParams(search), [search])
  return [params, useSetUrlParams()]
}

/** One query param as free text (an id, a site): null when absent. */
export function useUrlParam(key: string): [string | null, (value: string | null, also?: UrlPatch) => void] {
  const [params, set] = useUrlParams()
  const setValue = useCallback((value: string | null, also?: UrlPatch) => set({ ...also, [key]: value }), [key, set])
  return [params.get(key), setValue]
}

/**
 * One query param naming a value from a fixed list. The fallback is what an absent or unknown value reads as, and
 * is left out of the address, so a plain link stays plain; an unknown value is dropped from it.
 */
export function useUrlChoice<T extends string>(key: string, allowed: readonly T[], fallback: T): [T, (value: T, also?: UrlPatch) => void] {
  const [params, set] = useUrlParams()
  const raw = params.get(key)
  const known = raw !== null && (allowed as readonly string[]).includes(raw)
  useEffect(() => {
    if (raw !== null && !known) set({ [key]: null })
  }, [raw, known, key, set])
  const setValue = useCallback(
    (value: T, also?: UrlPatch) => set({ ...also, [key]: value === fallback ? null : value }),
    [key, fallback, set],
  )
  return [known ? (raw as T) : fallback, setValue]
}

/**
 * A page's tabs in `tab=` (or another key), plus the `reveal` request for the tab strip: set once when a link opened
 * the page on a tab, so a phone's sideways-scrolling strip shows it. Later changes scroll the strip by themselves.
 */
export function useUrlTab<T extends string>(tabs: readonly T[], fallback: T, key = 'tab'): [T, (tab: T, also?: UrlPatch) => void, object | null] {
  const [tab, setTab] = useUrlChoice(key, tabs, fallback)
  const [reveal] = useState<object | null>(() => (tab !== fallback ? {} : null))
  return [tab, setTab, reveal]
}

/**
 * Drops linked params whose thing is gone (a deleted connector, a rotated proxy) and says so in a toast, so a
 * colleague can tell a dead link from a wrong one. `gone` must only turn true once the data that would hold the
 * thing has loaded.
 */
export function useDropGone(keys: string | string[], gone: boolean, message: string): void {
  const set = useSetUrlParams()
  const toast = useToast()
  useEffect(() => {
    if (!gone) return
    set(Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map((key) => [key, null])))
    toast.show(message, 'error')
  }, [gone])
}

/** Names the browser tab, so tabs, history and bookmarks say which page they hold. */
export function useDocumentTitle(...parts: (string | null | undefined)[]): void {
  const title = [...parts.filter(Boolean), 'Octoprox'].join(' · ')
  useEffect(() => { document.title = title }, [title])
}
