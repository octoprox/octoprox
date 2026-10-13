// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * The worked example under a project's Location settings: how one pooled
 * residential slot provisioned for Germany fares under the chosen source
 * order, conflict rule and assurance level. Pure, so the form only renders it.
 *
 * The cases are the ones docs/location-routing.md walks through: two
 * databases (MaxMind ranked above DB-IP) that agree with the vendor, agree
 * against it, disagree with each other, or do not know the IP. The echo
 * endpoint is taken to report the IP alone, which is the common setup; an
 * echo that reports a country counts as one more independent source.
 */

import type { ConflictRule, GeoSourceKind, LocationAssurance } from '../api/client'

export type Verdict = 'confirmed' | 'contradicted' | 'open'

export interface ExampleRow {
  case: string
  evidence: string
  /** Country routing matches -cc- against; null when the row has no country. */
  routedAs: string | null
  verdict: Verdict
  /** What the assurance level does with the row. */
  outcome: string
}

const VENDOR = 'DE'

const CASES: { case: string; maxmind: string | null; dbip: string | null }[] = [
  { case: 'Databases agree with the vendor', maxmind: 'DE', dbip: 'DE' },
  { case: 'Databases agree against the vendor', maxmind: 'FR', dbip: 'FR' },
  { case: 'Databases disagree with each other', maxmind: 'FR', dbip: 'DE' },
  { case: 'No database knows the IP', maxmind: null, dbip: null },
]

function routedAs(sources: GeoSourceKind[], top: string | null): string | null {
  for (const kind of sources) {
    if (kind === 'database' && top) return top
    if (kind === 'vendor') return VENDOR
    // endpoint: a plain IP echo reports no country
  }
  return null
}

function verdictOf(rule: ConflictRule, maxmind: string | null, dbip: string | null): Verdict {
  if (!maxmind && !dbip) return 'open'
  if (rule === 'first') return maxmind !== VENDOR ? 'contradicted' : 'confirmed'
  if (maxmind !== dbip) return 'open'
  return maxmind === VENDOR ? 'confirmed' : 'contradicted'
}

function outcomeOf(assurance: LocationAssurance, verdict: Verdict, routed: string | null): string {
  // The refusals first: above observe a contradicted claim never serves, and
  // confirmed refuses an open one as well. Everything else is routed as usual.
  if (assurance !== 'observe' && verdict === 'contradicted') return 'skipped; the slot is replaced with a fresh session'
  if (assurance === 'confirmed' && verdict === 'open') return 'skipped until a later sighting settles the claim'
  if (routed === null) return 'routed to, only through the connector\'s country list'
  if (verdict === 'contradicted') return `routed to as ${routed}, flagged`
  if (verdict === 'open') {
    return assurance === 'verified' && routed === VENDOR ? `routed to as ${routed} on the vendor's word` : `routed to as ${routed}, marked open`
  }
  return `routed to as ${routed}`
}

export function locationExamples(sources: GeoSourceKind[], rule: ConflictRule, assurance: LocationAssurance): ExampleRow[] {
  return CASES.map(({ case: name, maxmind, dbip }) => {
    const routed = routedAs(sources, maxmind)
    const verdict = verdictOf(rule, maxmind, dbip)
    const evidence = maxmind ? `MaxMind ${maxmind}, DB-IP ${dbip}` : 'no answer'
    return { case: name, evidence, routedAs: routed, verdict, outcome: outcomeOf(assurance, verdict, routed) }
  })
}

/** One sentence on what preflight does to a -cc-de request through the example slot. */
export function preflightExample(on: boolean, assurance: LocationAssurance, strategy: string, maxAttempts: number): string {
  if (!on) {
    return 'No echo before forwarding. The slot\'s location is whatever the last health check found, so a session that moved since then is trusted until the next check.'
  }
  if (assurance === 'observe') {
    return 'One echo through the slot per session, cached for the session TTL. The result is recorded for provider accuracy; the request is forwarded even when the exit is somewhere else.'
  }
  const misplaced = assurance === 'confirmed'
    ? 'When the exit is somewhere else, or no database can place it,'
    : 'When the exit is somewhere else,'
  if (strategy === 'sticky') {
    return `One echo per session. ${misplaced} a request with a -sessid- is refused with 502 at once, since sticky promised it one exit; a request without one moves to another eligible proxy, up to ${maxAttempts} in all. A misplaced slot is replaced.`
  }
  return `One echo per session. ${misplaced} the request moves to another eligible proxy, up to ${maxAttempts} in all, then 502. A misplaced slot is replaced.`
}
