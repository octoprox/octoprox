// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * How a connector's exit reputation for a site reads, in the Traffic split card and in the Reputation tab's places
 * table: the factor its weight is multiplied by, coloured by how far it lowers it, and the reason
 * behind it (a dynamic connector's block rate, or the IPs a connector with IPs still has for the site).
 */

import type { PlaceState, ReputationKind, ReputationPlace } from '../api/client'
import { plural } from '../utils/format'
import { cn } from '../utils/cn'

/** What a reading of a connector's reputation for a site carries, in the traffic split and in a site's cell alike. */
export interface ReputationFacts {
  reputation: number
  reputation_kind: ReputationKind | null
  reputation_place: string | null
  /** Where the reading stands at the place, as the API worked it out; missing reads as no reports. */
  reputation_state?: PlaceState | null
  blocked: number | null
  requests: number | null
  usable_ips: number | null
  eligible_ips: number | null
}

/**
 * Each share's would-be effective weight and share of the site's requests, where the project does not avoid and the
 * API answers the configured ones: weight times reputation among the connectors that take requests (``takes``), as
 * Avoid would weigh them.
 */
export function wouldBe<T extends { weight: number; reputation: number }>(items: T[], takes: (item: T) => boolean): { weight: number; share: number }[] {
  const weights = items.map((item) => (takes(item) ? item.weight * item.reputation : 0))
  const total = weights.reduce((sum, weight) => sum + weight, 0)
  return weights.map((weight) => ({ weight, share: total > 0 ? (weight / total) * 100 : 0 }))
}

/** A number with at most ``digits`` decimals and no trailing zeros: 1.95, 2, 0.6. */
const trimmed = (value: number, digits: number) => value.toFixed(digits).replace(/\.?0+$/, '')

/** A factor as the views show it: ×0.65, ×1. */
export const factorText = (factor: number) => `×${trimmed(factor, 2)}`

/** An effective weight: two decimals under 10 (3 × 0.65 is 1.95), one above. */
export const weightText = (weight: number) => trimmed(weight, weight >= 10 ? 1 : 2)

/** The colour of a factor: red under 0.5, amber under 0.85, quiet above. */
export const factorTone = (factor: number) => (factor < 0.5 ? 'text-danger' : factor < 0.85 ? 'text-warning' : 'text-fg-muted')

/** Whether a factor lowers the weight at all: any value under 1. */
export const lowered = (factor: number) => factor < 1

/**
 * A lowered factor's background, deeper at ×0.85, ×0.5 and ×0.25; none at ×1. The deepest stays light enough for the
 * theme's text colour in every theme.
 */
export const factorTint = (factor: number) =>
  factor < 0.25 ? 'bg-danger/55' : factor < 0.5 ? 'bg-danger/35 dark:bg-danger/40' : factor < 0.85 ? 'bg-danger/20 dark:bg-danger/25' : factor < 1 ? 'bg-danger/10' : ''

/** Diagonal stripes: reported on, but with too little evidence yet to judge. Fainter in dark themes, so text on them reads. */
export const HATCHED = 'bg-[repeating-linear-gradient(135deg,var(--color-line-strong)_0_2px,transparent_2px_5px)] dark:bg-[repeating-linear-gradient(135deg,color-mix(in_srgb,var(--color-line-strong)_45%,transparent)_0_2px,transparent_2px_5px)]'

/**
 * How each state of a reading looks in a cell, and what it says: its own factor tinted by factorTint, a coarser level's
 * dashed (the place has too little evidence of its own), reported on but not judged yet hatched, never reported on
 * plain, and a dash for none of the place's requests going to it.
 */
export const STATE_LOOKS: Record<PlaceState, { box: string; text: string; title: string }> = {
  own: { box: '', text: 'text-fg', title: 'Its own factor at this place' },
  inherited: { box: 'border border-dashed border-fg-subtle', text: 'text-fg', title: 'From a coarser level: this place has too little evidence of its own' },
  not_judged: { box: HATCHED, text: 'text-fg-muted', title: 'Not judged yet: too little evidence' },
  no_reports: { box: '', text: 'text-fg-subtle', title: 'No reports' },
  takes_none: { box: '', text: 'text-fg-subtle', title: 'Takes none of these requests' },
}

/** A requested place's levels, finest first: "US/NY/new_york" is that, "US/NY" and "US"; "US//austin" that and "US". */
export function placeLevels(place: string): string[] {
  if (!place) return []
  const [country, state = '', city = ''] = place.split('/')
  const levels = [place]
  if (state && city) levels.push(`${country}/${state}`)
  if (state || city) levels.push(country)
  return levels
}

/** A place as people read it: the label the API gave it, else its parts ("US, NY, new_york"). */
export function placeLabel(place: string, places: ReputationPlace[] = []): string {
  return places.find((p) => p.place === place)?.label ?? place.split('/').filter(Boolean).join(', ')
}

/** What a level adds to its parent: "NY" for "US/NY", "new_york" for "US/NY/new_york", the country for a country. */
export const levelName = (level: string) => level.split('/').filter(Boolean).pop() ?? level

/**
 * Why a dynamic connector's factor came from a coarser level than the place asked for: "from US: NY has too little
 * evidence yet". Empty when it came from the place itself, when no level has one (the reason says so), or nothing was
 * asked for.
 */
export function levelNote(facts: ReputationFacts, place: string, places: ReputationPlace[] = []): string {
  if (!place || facts.reputation_kind !== 'block_rate' || facts.reputation_place == null) return ''
  const levels = placeLevels(place)
  const used = levels.indexOf(facts.reputation_place)
  if (used <= 0) return ''
  const skipped = levels.slice(0, used).map(levelName)
  return `from ${placeLabel(facts.reputation_place, places)}: ${skipped.join(' and ')} ${plural(skipped.length, 'has', 'have')} too little evidence yet`
}

/**
 * The reason behind a reading, as a clause: "35% of its requests to www.google.com were reported blocked (1,204 of
 * 3,440)", "12 of 20 IPs usable for www.google.com". ``place`` is the place asked for; a dynamic connector's figures are
 * those of the level its factor came from.
 */
export function reputationReason(facts: ReputationFacts, site: string, place: string, places: ReputationPlace[] = []): string {
  const level = facts.reputation_place ?? place
  const from = level ? ` from ${placeLabel(level, places)}` : ''
  if (facts.reputation_kind === 'usable_ips' && facts.usable_ips != null && facts.eligible_ips != null) {
    if (facts.eligible_ips === 0) return `no eligible IPs for ${site}`
    return `${facts.usable_ips.toLocaleString()} of ${facts.eligible_ips.toLocaleString()} IPs usable for ${site}`
  }
  if (facts.reputation_kind === 'block_rate' && facts.blocked != null && facts.requests != null) {
    const counts = `${facts.blocked.toLocaleString()} of ${facts.requests.toLocaleString()}`
    // No level with a factor: too few recent requests to judge by. The counts are then the finest level's that has a
    // record, so a place of several levels is not named as theirs.
    if (facts.reputation_place == null) {
      const evidence = placeLevels(place).length > 1
        ? `no level of ${placeLabel(place, places)} has enough evidence yet for ${site}`
        : `too little evidence yet for ${site}${from}`
      return `${evidence}, so its weight is unchanged (${counts} requests reported blocked)`
    }
    const rate = facts.requests > 0 ? Math.min(100, Math.round((facts.blocked / facts.requests) * 100)) : 100
    const reported = `${rate}% of its requests to ${site}${from} were reported blocked (${counts})`
    return facts.reputation >= 1 ? `${reported}, too few to lower its weight` : reported
  }
  return `no reports about it for ${site}${from}, so its weight is unchanged`
}

/** A reading's sentence for a tooltip: the reason, then where its factor came from when not the place asked for. */
export function reputationTitle(name: string, facts: ReputationFacts, site: string, place: string, places: ReputationPlace[] = []): string {
  const note = levelNote(facts, place, places)
  return `${name}: ${reputationReason(facts, site, place, places)}.${note ? ` Reputation ${note}.` : ''}`
}

/**
 * The factor with a small bar, coloured by how far it lowers the weight, after the level it came from when that is
 * coarser than the place asked for. A connector reported on with too little evidence yet shows ×1 with a hatched "few"
 * in the bar's place, and one never reported on a muted ×1 with a dot, so neither reads as a measured clean ×1.
 */
export function ReputationFactor({ facts, places = [], className }: { facts: ReputationFacts; places?: ReputationPlace[]; className?: string }) {
  const state = facts.reputation_state ?? 'no_reports'
  // Unjudged: the weight is taken as it is, which a stacked row reads as "Weight 3 ×1 = 3". The slot after it is the
  // bar's width, so a column of factors stays in line.
  if (state === 'no_reports' || state === 'not_judged') {
    const { title } = STATE_LOOKS[state]
    return (
      <span className={cn('inline-flex items-center gap-1.5 tabular-nums whitespace-nowrap', STATE_LOOKS[state].text, className)} title={title}>
        ×1
        {state === 'not_judged'
          ? <span className={cn('w-[30px] text-center text-[10px] leading-none py-0.5 rounded-sm flex-none', HATCHED)}>few</span>
          : <span className="w-[30px] text-center leading-none flex-none" aria-hidden>·</span>}
      </span>
    )
  }
  const coarser = state === 'inherited' ? facts.reputation_place : null
  return (
    <span className={cn('inline-flex items-center gap-1.5 tabular-nums whitespace-nowrap', factorTone(facts.reputation), className)}>
      {/* Before the factor, so the bars of a column of them stay in line. */}
      {coarser && (
        <span className="text-[10px] leading-none px-1 py-0.5 rounded-sm bg-surface-raised text-fg-subtle" title={`Uses the factor from ${placeLabel(coarser, places)}`}>
          {levelName(coarser)}
        </span>
      )}
      {factorText(facts.reputation)}
      <span className="w-[30px] h-1 rounded-full bg-surface-raised overflow-hidden inline-block flex-none" aria-hidden>
        <span className="block h-full rounded-full bg-current" style={{ width: `${Math.max(0, Math.min(1, facts.reputation)) * 100}%` }} />
      </span>
    </span>
  )
}
