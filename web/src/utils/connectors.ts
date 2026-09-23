// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { Connector } from '../api/client'

/** The number of proxies a connector aims for, or null when it has no target (static, vendor lists). */
export const targetTotal = (c: Connector): number | null => c.target?.total ?? null

/** Dynamic sessions: the connector holds one gateway proxy and mints a vendor session per request. */
export const isDynamic = (c: Connector): boolean => c.target?.dynamic === true

/**
 * One line explaining the target, for a hover title. Provider connectors count
 * per country, so "8" is really "1 per country across 8 countries".
 */
export function describeTarget(c: Connector): string | undefined {
  const t = c.target
  if (!t || t.total == null) return undefined
  if (t.dynamic) {
    const scope = t.countries.length > 0 ? `countries: ${t.countries.join(', ')}` : 'any country'
    return `Dynamic sessions · one gateway proxy, a vendor session per request · ${scope}`
  }
  if (t.per_country == null || t.countries.length === 0) return `${t.total} configured`
  // total is per_country times the number of groups; an "all countries" pool also has an ungeo-targeted group.
  const groups = Math.round(t.total / t.per_country)
  const parts = [`${t.per_country} per country`, `${groups} ${groups === 1 ? 'group' : 'groups'}`]
  if (t.on_demand.length > 0) parts.push(`${t.on_demand.length} on demand: ${t.on_demand.join(', ')}`)
  else parts.push(t.countries.join(', '))
  return parts.join(' · ')
}
