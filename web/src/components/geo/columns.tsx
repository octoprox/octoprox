// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from 'react'
import { InfoTip } from '../ui'

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

export const TIPS = {
  source: (
    <>
      <b>discovery</b>: the provider syncer learned the IP when it created or refreshed the slot.<br />
      <b>health check</b>: the health check response echoed the exit IP.<br />
      <b>geo lookup</b>: a manually added proxy was located when it was added.<br />
      <b>manual</b>: the Detect button.<br />
      <b>preflight</b>: a request was verified before being forwarded.<br />
      <b>reattribute</b>: an offline re-run after a database changed.
    </>
  ),
  vendorSaid: 'The country the vendor promised: from its proxy list, the geo target of the slot, or a country pinned by hand. Empty when nothing was promised.',
  resolved: 'The country attribution settled on, and which source decided: a local database, the vendor claim, or the echo endpoint, in the order the source policy sets.',
  verdict: (
    <>
      <b>confirmed</b>: the vendor's claim matched.<br />
      <b>contradicted</b>: independent sources agreed on a different country (per the conflict rule).<br />
      <b>uncertain</b>: independent sources disagreed with each other, so the vendor is not judged.<br />
      <b>no claim</b>: the vendor promised nothing to check.
    </>
  ),
  claimsChecked: 'Distinct exit IPs seen in the window for which the vendor had promised a country, so there was something to verify. Each IP counts once.',
  confirmed: 'Exits whose latest verdict agrees with the vendor\'s claim, then in red the ones that contradict it. Uncertain exits (independent sources disagreed) are in neither.',
  accuracy: 'Confirmed divided by exits with a claim; uncertain exits count against it. Below 100% is normal for residential pools whose ranges databases have not caught up with; a falling rate is a vendor problem.',
  uniqueExits: 'Distinct exit IPs this connector handed out: first seen within the selected window / ever.',
  reused: 'Share of the connector\'s distinct exit IPs that were handed out more than once. High for pools that recycle a small set of exits.',
  whereWrong: 'The most frequent contradicted pairs: what the vendor claimed, then what attribution resolved, with the count.',
  sightings: 'How many times the connector handed this IP to a proxy. Re-checks of an exit a proxy already had (re-attribution, preflight, unchanged health checks) do not count.',
  latestState: 'The claim, resolution and verdict of the most recent observation of this exit, whatever its source. The observation log holds every earlier one.',
}
