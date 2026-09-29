// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import type { ObservationVerdict, PlaceLevel } from '../../api/client'
import { Badge, InfoTip } from '../ui'

/** A column header with a short explanation of the values it holds. */
export function HeaderWithTip({ label, tip }: { label: string; tip: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1">
      {label}
      {/* Clicking the icon must not toggle the column sort. */}
      <span onClick={(e) => e.stopPropagation()} className="inline-flex">
        <InfoTip label={`About ${label.toLowerCase()}`}>{tip}</InfoTip>
      </span>
    </span>
  )
}

/** A place as one line: `US · NY · new_york`, or `-` when nothing is known. */
export function placeLabel(country: string | null | undefined, state?: string | null, city?: string | null): string {
  const parts = [country, state, city].filter((p): p is string => !!p)
  return parts.length ? parts.join(' · ') : '-'
}

/** The claim and verdict fields an observation and an exit IP row share. */
export interface JudgedRow {
  claimed_country: string | null
  claimed_state: string | null
  claimed_city: string | null
  country_conflict: boolean | null
  state_conflict: boolean | null
  city_conflict: boolean | null
}

const VERDICT_LABEL: Record<ObservationVerdict, string> = { contradicted: 'contradicted', open: 'open', confirmed: 'confirmed', no_claim: 'no claim' }
const LEVEL_CLASS: Record<ObservationVerdict, string> = { contradicted: 'text-danger', open: 'text-warning', confirmed: 'text-success', no_claim: 'text-fg-subtle' }

function verdictAt(claimed: string | null, conflict: boolean | null): ObservationVerdict {
  if (conflict === true) return 'contradicted'
  if (conflict === false) return 'confirmed'
  return claimed ? 'open' : 'no_claim'
}

/** The row's headline verdict: the country's. The server's verdict filter selects on the same. */
export function countryVerdict(row: JudgedRow): ObservationVerdict {
  return verdictAt(row.claimed_country, row.country_conflict)
}

/** The verdict at each level below the country the vendor claimed, state first. */
export function lowerLevelVerdicts(row: JudgedRow): { level: PlaceLevel; verdict: ObservationVerdict }[] {
  return ([['state', row.claimed_state, row.state_conflict], ['city', row.claimed_city, row.city_conflict]] as const)
    .filter(([, claimed]) => !!claimed)
    .map(([level, claimed, conflict]) => ({ level, verdict: verdictAt(claimed, conflict) }))
}

/**
 * The verdict cell of the observation and exit IP tables: the country
 * verdict as the badge, since an exit right at the country is usable at the
 * country whatever its city turned out to be, then one short line per state
 * or city claimed ("city contradicted"), and `note` last in small type.
 */
export function VerdictCell({ row, note }: { row: JudgedRow; note?: ReactNode }) {
  const country = countryVerdict(row)
  const badge = country === 'contradicted' ? <Badge color="red" className="inline-flex items-center gap-1 whitespace-nowrap"><AlertTriangle className="w-3 h-3" /> contradicted</Badge>
    : country === 'open' ? <Badge color="yellow">open</Badge>
    : country === 'confirmed' ? <Badge color="green">confirmed</Badge>
    : <Badge color="gray" className="whitespace-nowrap">no claim</Badge>
  const lower = lowerLevelVerdicts(row)
  return (
    <div className="min-w-0">
      {badge}
      {lower.length > 0 && (
        <div className="mt-1 space-y-0.5 text-[11px] leading-tight">
          {lower.map((l) => (
            <div key={l.level} className={`flex items-center gap-1.5 whitespace-nowrap ${LEVEL_CLASS[l.verdict]}`}>
              <span className="w-1.5 h-1.5 rounded-full bg-current shrink-0" aria-hidden />
              {l.level} {VERDICT_LABEL[l.verdict]}
            </div>
          ))}
        </div>
      )}
      {note && <div className="text-[11px] text-fg-subtle mt-1 truncate">{note}</div>}
    </div>
  )
}

export const TIPS = {
  source: (
    <>
      <b>discovery</b>: the provider syncer learned the IP when it created or refreshed the slot.<br />
      <b>health check</b>: the health check response echoed the exit IP.<br />
      <b>geo lookup</b>: a manually added proxy was located when it was added.<br />
      <b>manual</b>: the Detect button.<br />
      <b>preflight</b>: a request was verified before being forwarded.
    </>
  ),
  vendorSaid: 'The place the vendor was asked for or promised: the country from its proxy list, the geo target of the slot, or a pin by hand, then the state and city a request asked for (-st-, -city-) or an operator pinned. Empty when nothing was promised.',
  resolved: 'The place attribution settled on: the country under the source policy (a local database, the vendor claim, or the echo endpoint), then the state and city from the databases, with the source of the country under it.',
  verdict: (
    <>
      The badge is the country verdict:<br />
      <b>confirmed</b>: independent sources agreed with the vendor's country.<br />
      <b>contradicted</b>: independent sources said a different country (per the conflict rule).<br />
      <b>open</b>: no verdict: no independent source knows the IP, or they disagreed with each other, so the vendor is neither cleared nor blamed.<br />
      <b>no claim</b>: the vendor promised nothing to check.<br />
      A state or city that was asked for or named is judged the same way and gets its own line under the badge (<b>city contradicted</b>, <b>state open</b>). An exit right at the country stays usable at the country whatever its city turned out to be.
    </>
  ),
  accuracy: (
    <>
      One line per level a claim was made at (the vendor's country, and the state or city a request asked for or the vendor named), each over the distinct exits seen in the window, one verdict per exit.<br />
      The bar and percentage are <b>confirmed over confirmed plus contradicted</b>; the counts after it are confirmed, contradicted and open. Open exits (no independent answer at that level, or sources that disagreed) count in neither.<br />
      Below 100% is normal for residential pools whose ranges databases have not caught up with; a falling rate is a vendor problem.
    </>
  ),
  uniqueExits: 'Distinct exit IPs this connector handed out: first seen within the selected window / ever.',
  reused: 'Share of the connector\'s distinct exit IPs that were handed out more than once. High for pools that recycle a small set of exits.',
  whereWrong: 'Contradicted exits per level, with the most frequent claimed and observed pair. Open the details for the full list.',
  sightings: 'How many times the connector handed this IP to a proxy. Re-checks of an exit a proxy already had (re-attribution, preflight, unchanged health checks) do not count.',
  latestState: (
    <>
      The verdict of the most recent observation of this exit, whatever its source: the country as the badge, then one line per state or city claimed.<br />
      The last line is how the exit was seen. "via reattribute" means the verdict was recomputed offline after a database changed, with no new sighting. The observation log holds every sighting.
    </>
  ),
}
