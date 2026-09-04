// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Format bytes as human-readable string (e.g., "1.5 MB")
 */
export function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`
}

/**
 * The API serialises timestamps as naive UTC ("2026-05-19T20:10:52.001790",
 * no zone suffix). `new Date()` would read that as local time, shifting every
 * date by the viewer's UTC offset. Treat zone-less strings as UTC.
 */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
  const d = new Date(hasZone ? value : `${value}Z`)
  return isNaN(d.getTime()) ? null : d
}

export function formatDate(value: string | null | undefined): string {
  const d = parseApiDate(value)
  return d ? d.toLocaleDateString() : '—'
}

export function formatDateTime(value: string | null | undefined): string {
  const d = parseApiDate(value)
  return d ? d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—'
}

export function formatTime(value: string | null | undefined): string {
  const d = parseApiDate(value)
  return d ? d.toLocaleTimeString() : '—'
}

/** "just now", "5 min ago", "3 h ago", "2 d ago" — relative to the viewer's clock. */
export function relativeTime(value: string | null | undefined): string {
  const d = parseApiDate(value)
  if (!d) return '—'
  const diff = Date.now() - d.getTime()
  const m = Math.round(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h} h ago`
  const days = Math.round(h / 24)
  if (days < 30) return `${days} d ago`
  return d.toLocaleDateString()
}
