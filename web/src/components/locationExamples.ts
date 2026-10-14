// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * The worked example under a project's Location settings: how one pooled
 * residential slot provisioned for Germany fares under the chosen conflict
 * rule and the two location decisions. Pure, so the form only renders it.
 *
 * The cases are the ones docs/location-routing.md walks through: two
 * databases (MaxMind ranked above DB-IP) that agree with the vendor, agree
 * against it, disagree with each other, or do not know the IP. The echo
 * endpoint is taken to report the IP alone, which is the common setup; an
 * echo that reports a country counts as one more independent source. The
 * routing value is the databases in their priority, then the echo, then the
 * vendor filling only what nothing independent answered; a confirmed claim
 * is routed on the answer that confirmed it, and a contradicted one carries
 * the vendor's claim only when the project routes on the vendor's claim.
 */

import type { ConflictRule, ContradictedAction, UnknownAction } from '../api/client'

export type Verdict = 'confirmed' | 'contradicted' | 'open'

export interface ExampleRow {
  case: string
  evidence: string
  /** Country routing matches -cc- against; null when the slot is skipped. */
  routedAs: string | null
  verdict: Verdict
  /** What the two decisions do with the row. */
  outcome: string
}

const VENDOR = 'DE'

const CASES: { case: string; maxmind: string | null; dbip: string | null }[] = [
  { case: 'Databases agree with the vendor', maxmind: 'DE', dbip: 'DE' },
  { case: 'Databases agree against the vendor', maxmind: 'FR', dbip: 'FR' },
  { case: 'Databases disagree with each other', maxmind: 'FR', dbip: 'DE' },
  { case: 'No database knows the IP', maxmind: null, dbip: null },
]

function verdictOf(rule: ConflictRule, maxmind: string | null, dbip: string | null): Verdict {
  if (!maxmind && !dbip) return 'open'
  if (rule === 'first') return maxmind !== VENDOR ? 'contradicted' : 'confirmed'
  // any: a single agreeing database confirms; none agreeing contradicts. Never open while a database answers.
  if (rule === 'any') return maxmind === VENDOR || dbip === VENDOR ? 'confirmed' : 'contradicted'
  if (maxmind !== dbip) return 'open'
  return maxmind === VENDOR ? 'confirmed' : 'contradicted'
}

/**
 * The country the row is routed on. A confirmed claim is routed on the answer
 * that confirmed it (the vendor's value); a contradicted one on the top
 * database, or on the claim when the project routes on the vendor's claim; an
 * open one on the top database when any knows the IP, else the vendor's word.
 */
function routedAs(contradicted: ContradictedAction, unknown: UnknownAction, verdict: Verdict, maxmind: string | null): string {
  if (verdict === 'confirmed') return VENDOR
  if (verdict === 'contradicted') return contradicted === 'vendor' ? VENDOR : maxmind ?? VENDOR
  // Open: the vendor's word when unknown exits are trusted (also when the databases disagree), else the top database.
  return unknown === 'vendor' ? VENDOR : maxmind ?? VENDOR
}

/** Whether the decisions skip the row for this verdict. */
function isSkipped(contradicted: ContradictedAction, unknown: UnknownAction, verdict: Verdict): boolean {
  return (verdict === 'contradicted' && contradicted === 'refuse') || (verdict === 'open' && unknown === 'refuse')
}

function outcomeOf(contradicted: ContradictedAction, unknown: UnknownAction, verdict: Verdict, routed: string): string {
  if (verdict === 'contradicted') {
    if (contradicted === 'refuse') return 'skipped; the slot is replaced with a fresh session'
    if (contradicted === 'vendor') return `routed to as ${routed} on the vendor's word, flagged`
    return `routed to as ${routed}, where the databases put it`
  }
  if (verdict === 'open') {
    if (unknown === 'refuse') return 'skipped until a later sighting places the exit'
    return routed === VENDOR ? `routed to as ${routed} on the vendor's word, marked open` : `routed to as ${routed} on the top database, marked open`
  }
  return `routed to as ${routed}`
}

export function locationExamples(rule: ConflictRule, contradicted: ContradictedAction, unknown: UnknownAction): ExampleRow[] {
  return CASES.map(({ case: name, maxmind, dbip }) => {
    const verdict = verdictOf(rule, maxmind, dbip)
    const routed = routedAs(contradicted, unknown, verdict, maxmind)
    const outcome = outcomeOf(contradicted, unknown, verdict, routed)
    const evidence = maxmind ? `MaxMind ${maxmind}, DB-IP ${dbip}` : 'no answer'
    return { case: name, evidence, routedAs: isSkipped(contradicted, unknown, verdict) ? null : routed, verdict, outcome }
  })
}

/** One sentence on what preflight does to a -cc-de request through the example slot. */
export function preflightExample(on: boolean, contradicted: ContradictedAction, unknown: UnknownAction, strategy: string, maxAttempts: number): string {
  if (!on) {
    return 'No echo before forwarding. The slot\'s location is whatever the last health check found, so a session that moved since then is trusted until the next check.'
  }
  const refusesMismatch = contradicted !== 'vendor'
  const refusesUnplaced = unknown === 'refuse'
  if (!refusesMismatch && !refusesUnplaced) {
    return 'One echo through the slot per session, cached for the session TTL. The result is recorded for provider accuracy; the request is forwarded even when the exit is somewhere else.'
  }
  const refused = refusesMismatch && refusesUnplaced
    ? 'When the exit is somewhere other than the requested place, or nobody can place it,'
    : refusesMismatch
      ? 'When the exit is somewhere else,'
      : 'When nobody can place the exit,'
  const slot = contradicted === 'refuse'
    ? 'A misplaced slot is replaced.'
    : 'A misplaced slot is kept; only refusing the proxy on a contradiction replaces it.'
  if (strategy === 'sticky') {
    return `One echo per session. ${refused} a request with a -sessid- is refused with 502 at once, since sticky promised it one exit; a request without one moves to another eligible proxy, up to ${maxAttempts} in all. ${slot}`
  }
  return `One echo per session. ${refused} the request moves to another eligible proxy, up to ${maxAttempts} in all, then 502. ${slot}`
}
