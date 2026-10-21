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

export type RowKind = 'fixed' | 'pooled' | 'dynamic'
export type Ask = 'none' | 'country' | 'place'

export interface ScenarioInput {
  kind: RowKind
  ask: Ask
  session: boolean
  declared: boolean
  preflight: boolean
  contradicted: ContradictedAction
  unknown: UnknownAction
  strategy: string
  maxAttempts: number
}

export interface ScenarioStep {
  title: string
  text: string
  warn?: boolean
}

/** What happens to one request, step by step, for the chosen row kind and the current settings. Pure. */
export function scenarioSteps(i: ScenarioInput): ScenarioStep[] {
  const steps: ScenarioStep[] = []
  const sticky = i.strategy === 'sticky'
  const holdsVendor = i.contradicted === 'refuse'
  const refusesUnknown = i.unknown === 'refuse'
  const canRefuseAsk = i.contradicted !== 'vendor' || refusesUnknown
  // A session-less dynamic request that names nothing is only checked against something when the connector picks a country for it.
  const promised = i.kind === 'dynamic' && i.ask === 'none' && i.declared

  if (i.kind === 'pooled' && i.ask === 'place') {
    steps.push({
      title: `Not served`,
      text: `Pooled slots are provisioned per country and carry no state or city, so this request never reaches one. A dynamic gateway that renders the place, or a fixed exit whose known place matches, serves it; with neither in the project the client gets 502 naming the place. Nothing is echoed or replaced.`,
      warn: true,
    })
    return steps
  }

  if (i.ask !== 'none') {
    steps.push(i.declared
      ? { title: `Candidate connectors`, text: `Only connectors that declare the requested country are considered. If none does, the client gets 502 naming the country and nothing is echoed.` }
      : { title: `Candidate connectors`, text: i.kind === 'dynamic'
        ? `The connector declares no countries, so it serves whatever country the client asks for by rendering it into the vendor session.`
        : i.kind === 'pooled'
          ? `The connector declares no countries, so the first request for a country provisions a slot group for it, and later requests reuse it.`
          : `The connector declares no countries, so the request is served only by a fixed exit whose resolved country matches.` })
  }

  // Where the verdict comes from.
  if (i.kind === 'fixed') {
    steps.push({ title: `Judged by`, text: `Discovery places the exit and every health check re-reads it; the vendor's claim is re-judged whenever the exit moves. Preflight never runs on fixed exits, so the two decisions act only through the verdict stored on the row.` })
  } else if (i.kind === 'pooled') {
    steps.push({ title: `Judged by`, text: `The health check every minute and every preflight echo through the slot, against the country the slot was provisioned for. The two decisions act on that stored verdict, and also at preflight when the client names a country.` })
  } else {
    steps.push({ title: `Judged by`, text: `Preflight only. Every request gets its own vendor session, so the gateway row stores no verdict and is not health-checked. With preflight off, the two decisions do nothing on this connector.` })
  }

  // The echo for this request.
  if (i.kind === 'fixed') {
    steps.push({ title: `Preflight on this request`, text: `Never. The exit is already known, and an echo would only repeat what the health check sees.` })
  } else if (!i.preflight) {
    steps.push(i.kind === 'dynamic'
      ? { title: `Preflight on this request`, text: `Off for this project: no echo. Any place the client asked for is still rendered into the vendor session, but nothing checks that the vendor honoured it, and the connector collects no accuracy data.`, warn: true }
      : { title: `Preflight on this request`, text: `Off for this project: no echo. The slot is trusted as the last health check judged it, so a session that moved since then keeps serving until the next check.` })
  } else if (i.kind === 'pooled') {
    steps.push(i.ask === 'country'
      ? { title: `Preflight on this request`, text: `One echo per session, compared with the requested country. The verdict is cached for the preflight TTL (10 minutes by default, set under Settings, IP attribution; not tied to the sticky session) and dropped early when a health check sees the exit move.` }
      : { title: `Preflight on this request`, text: `Not echoed: the client named no place, so there is nothing to compare the exit with. The slot's own claim is still judged on the row, by the health check and by echoes from requests that do name a country.` })
  } else if (i.session) {
    steps.push({ title: `Preflight on this request`, text: `One echo per vendor session, cached for the preflight TTL (10 minutes by default, not extended by use). The vendor session comes from the -sessid- and the place together, so the same -sessid- asking for another place gets a new session and a new echo.${promised ? ` With nothing asked, the country picked from the connector's list is the vendor's promise.` : ``}` })
  } else if (i.ask !== 'none') {
    steps.push(canRefuseAsk
      ? { title: `Preflight on this request`, text: `Every request is echoed: without a -sessid- each request gets a fresh vendor session, so there is no cached verdict to reuse, and sampling would let misplaced exits through.` }
      : { title: `Preflight on this request`, text: `Sampled at the connector's exit sampling rate, for the accuracy figures only: these settings refuse nothing.` })
  } else if (!i.declared) {
    steps.push({ title: `Preflight on this request`, text: `Sampled at the connector's exit sampling rate, for the accuracy figures only: nothing was asked or promised, so nothing is refused.` })
  } else if (holdsVendor) {
    steps.push({ title: `Preflight on this request`, text: `Every request is echoed: the country picked from the connector's list is the vendor's promise, and refusing contradictions holds it exactly.` })
  } else if (refusesUnknown) {
    steps.push({ title: `Preflight on this request`, text: `Every request is echoed: an exit nobody can place is refused, and a sampled refusal would be no guarantee.` })
  } else if (i.contradicted === 'evidence') {
    steps.push({ title: `Preflight on this request`, text: `Sampled at the connector's exit sampling rate. A sampled exit outside the connector's countries is refused; an unsampled one goes unseen. Set the rate to 100 to make the list a full guarantee.` })
  } else {
    steps.push({ title: `Preflight on this request`, text: `Sampled at the connector's exit sampling rate, for the accuracy figures only: the vendor's pick is trusted, so nothing is refused.` })
  }

  // The contradicted decision.
  const kept = i.kind === 'pooled'
    ? `The slot is kept, and nothing is provisioned.`
    : i.kind === 'fixed'
      ? `A fixed exit is never replaced.`
      : i.session
        ? `Nothing is written on the gateway. The same -sessid- asking for the same place gets the same vendor session, and the cached refusal answers 502 for the preflight TTL; a new -sessid- or another place gets a fresh session.`
        : `Nothing is written on the gateway; the next request mints another session.`
  const quiet = i.kind === 'dynamic' && i.ask === 'none' && !i.declared
  const unchecked = i.kind === 'dynamic' && !i.preflight
  if (unchecked) {
    steps.push({ title: `Evidence contradicts the vendor`, text: `No effect: with preflight off nothing judges a dynamic session, so the request is forwarded whatever the decisions say.` })
  } else if (quiet) {
    steps.push({ title: `Evidence contradicts the vendor`, text: `Cannot happen on this request: nothing was asked and the connector picked no country, so there is no claim to contradict.` })
  } else if (i.contradicted === 'refuse') {
    steps.push({ title: `Evidence contradicts the vendor`, text: i.kind === 'pooled'
      ? i.declared
        ? `The slot is removed, and the provider sync mints a fresh session in its place.`
        : `The slot is removed. While the country has other slots, the provider sync mints a fresh session in its place; once its last slot is gone, the next request for the country asks for a new group and waits a few seconds for it.`
      : i.kind === 'fixed'
        ? `The row is skipped for every request. ${kept}`
        : `The request is refused. ${kept}` })
  } else if (i.contradicted === 'vendor') {
    steps.push({ title: `Evidence contradicts the vendor`, text: i.kind === 'dynamic'
      ? `The request is forwarded on the vendor's word, and the contradiction is recorded.`
      : `The row is routed on the vendor's claim and shown as contradicted. ${kept}` })
  } else {
    steps.push({ title: `Evidence contradicts the vendor`, text: i.kind === 'dynamic'
      ? `${i.ask === 'none' ? `Forwarded when the exit lands inside the connector's countries, refused when it lands outside them` : `Forwarded when the evidence puts the exit in the requested place, refused when it puts it elsewhere`}${i.session ? `` : `; the retry mints a fresh session`}. ${kept}`
      : i.declared
        ? `The row is routed on the country the evidence found and shown as contradicted. Inside the connector's list it serves that country and untargeted traffic; outside it, it is skipped for everything and flagged on the Proxies page. ${kept}`
        : `The row is routed on the country the evidence found and shown as contradicted. The connector declares no countries, so it serves that country and untargeted traffic. ${kept}` })
  }

  // The unknown decision.
  if (unchecked) {
    steps.push({ title: `Nothing places the exit`, text: `No effect either, for the same reason.` })
  } else if (quiet) {
    steps.push({ title: `Nothing places the exit`, text: `Recorded only: with nothing asked or promised there is nothing to confirm.` })
  } else if (refusesUnknown) {
    steps.push({ title: `Nothing places the exit`, text: i.kind === 'dynamic'
      ? `The request is refused as not confirmed.${i.ask === 'place' ? ` A state or city the client named must be placed too, which needs a city-level database.` : ``}`
      : `The row is skipped until a later sighting places the exit, and is not replaced meanwhile. A hand-pinned country is exempt. Needs an IP database loaded.${i.kind === 'pooled' && i.preflight && i.ask === 'country' ? ` At preflight, this request is refused as not confirmed.` : ``}` })
  } else {
    steps.push({ title: `Nothing places the exit`, text: i.kind === 'dynamic'
      ? `The request is forwarded on the vendor's word.`
      : `The row is routed on the vendor's word and marked open.` })
  }

  // What a refusal does to the request.
  const preflighted = i.preflight && i.kind !== 'fixed' && !(i.kind === 'pooled' && i.ask === 'none')
  const refusable = i.ask === 'none'
    ? promised && (holdsVendor || refusesUnknown || i.contradicted === 'evidence')
    : canRefuseAsk
  if (preflighted && refusable) {
    if (sticky && i.session) {
      steps.push({ title: `On a refused request`, text: i.kind === 'pooled'
        ? `502 at once, since sticky promised the session one exit. If the slot was replaced, the next request with this -sessid- binds to another slot; otherwise the binding holds until the re-judged slot no longer serves the country.`
        : `502 at once, since sticky promised the session one exit. The refusal is cached, so the same -sessid- and place keep getting 502 for the preflight TTL: switch to a new -sessid-.` })
    } else {
      steps.push({ title: `On a refused request`, text: i.kind === 'pooled'
        ? `The slot is excluded and another one for the country is tried, up to ${i.maxAttempts} attempts in all, then 502. If no other slot serves the country, that is a 502 right away.${i.declared ? `` : ` The refused slot still counts as the country's group, so the retry asks for no new one.`}`
        : i.session
          ? `The gateway is excluded and another connector is tried with the same -sessid-, up to ${i.maxAttempts} attempts in all; with no other connector, 502.`
          : `The same gateway is tried again with a fresh vendor session, up to ${i.maxAttempts} attempts in all, then 502.` })
    }
  }
  return steps
}

export interface RowKindFact {
  topic: string
  fixed: string
  pooled: string
  dynamic: string
}

/** What matters for which kind of proxy, as a fixed reference table. */
export const ROW_KIND_FACTS: RowKindFact[] = [
  { topic: 'Examples', fixed: 'Static lists, ISP and datacenter ports, cloud instances', pooled: 'Residential and mobile with Sessions: Fixed pool', dynamic: 'Residential and mobile with Sessions: Dynamic' },
  { topic: 'Exit learned by', fixed: 'Discovery, re-read by every health check', pooled: 'Health check, and preflight echoes', dynamic: 'Preflight echoes only' },
  { topic: 'The two decisions act', fixed: 'On the row verdict', pooled: 'On the row verdict, and at preflight for a country the client named', dynamic: 'At preflight only; nothing without it' },
  { topic: 'Preflight', fixed: 'Never', pooled: 'When the client names a country', dynamic: 'Always on: once per session; session-less requests are sampled unless the settings can refuse them' },
  { topic: 'State and city', fixed: 'Served when the known place matches', pooled: 'Never served', dynamic: 'Rendered into the session' },
  { topic: 'Replaced on refuse', fixed: 'No, skipped', pooled: 'Yes, a fresh session', dynamic: 'Nothing to replace' },
]
