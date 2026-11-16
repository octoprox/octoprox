// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import type { ConnectorBySite, ConnectorsBySite as ConnectorsBySiteData, ReputationPlace } from '../../api/client'
import { ProviderLogo } from '../ProviderLogo'
import { formatShare } from '../TrafficSplit'
import { factorText, factorTone, measured, placeLabel, placeLevels, reputationTitle, wouldBe } from '../ConnectorReputation'
import { Card, CardHeader, InfoTip, LogoLoader, Reloading, Select } from '../ui'
import { LoadError, loadFailed } from '../ui/LoadError'
import { cn } from '../../utils/cn'

// The location picker: compact under a mouse, 16 px text and 44 px tall on touch.
const PICKER = 'w-auto min-w-0 max-w-[14rem] h-7 py-0 pl-2 pr-7 rounded-md text-[12.5px] pointer-coarse:h-11 pointer-coarse:text-base'
// Table cells; the first column stays in view while the sites scroll sideways under it.
const CELL = 'px-2 py-1.5 border-b border-line text-right whitespace-nowrap'
const FIRST = 'sticky left-0 z-[1] bg-surface pl-4 pr-3 text-left border-r border-r-line'

/** A cell picked to read its reason under the table: a tap on a phone, which shows no tooltips. */
interface Picked { connectorId: string; site: string }

/** One connector on one site: its share of the site's requests at its effective weight, its reputation under it. */
function SiteCell({ connector, site, share, place, places, picked, onPick }: {
  connector: ConnectorBySite
  site: string
  /** The connector's share of the site's requests, as routing has it or, where the project does not avoid, as Avoid would. */
  share: number
  place: string
  places: ReputationPlace[]
  picked: boolean
  onPick: () => void
}) {
  const cell = connector.cells[site]
  if (!cell) return <td className={cn(CELL, 'text-fg-subtle')}>-</td>
  const coarser = cell.reputation_place && placeLevels(place).indexOf(cell.reputation_place) > 0 ? cell.reputation_place : null
  return (
    <td className={cn(CELL, 'p-0')}>
      <button
        type="button"
        onClick={onPick}
        aria-pressed={picked}
        title={reputationTitle(connector.name, cell, site, place, places)}
        className={cn(
          'w-full grid justify-items-end content-center gap-px px-2 py-1.5 rounded-md pointer-coarse:min-h-11 hover:bg-surface-raised transition-colors',
          picked && 'bg-surface-raised',
        )}
      >
        <span className="font-medium tabular-nums">{formatShare(share)}</span>
        <span className="inline-flex items-center gap-1 text-[10.5px] leading-tight">
          {coarser && <span className="px-1 rounded-sm bg-surface-raised text-fg-subtle">{coarser.split('/').filter(Boolean).pop()}</span>}
          {!measured(cell)
            ? <span className="text-fg-subtle">no reports</span>
            : <span className={cn('tabular-nums', factorTone(cell.reputation))}>{factorText(cell.reputation)}</span>}
        </span>
      </button>
    </td>
  )
}

/**
 * Every connector against the project's busiest sites: in each cell the connector's share of the site's requests at its
 * effective weight, with its reputation under it, for requests naming the picked place (or none). The table scrolls
 * sideways inside its card when the sites do not fit, the connector column staying in view; a cell's reason shows in
 * its tooltip, and under the table once tapped.
 */
export function ConnectorsBySite({ query, place, onPlace, avoids }: {
  query: UseQueryResult<ConnectorsBySiteData>
  place: string
  onPlace: (place: string) => void
  /** Whether the project avoids, for an answer that does not say whether routing uses these weights. */
  avoids: boolean
}) {
  const [picked, setPicked] = useState<Picked | null>(null)
  const data = query.data
  const applied = data?.reputation_applied ?? avoids
  // Each site's shares: the answer's where routing uses them; else, the answer being by weight, what Avoid would make
  // of them among the connectors that take the site's requests (those with a share now).
  const shares = new Map((data?.sites ?? []).map((site) => {
    const connectors = data?.connectors ?? []
    const would = applied ? null : wouldBe(connectors.map((c) => ({ weight: c.weight, reputation: c.cells[site]?.reputation ?? 1, takes: (c.cells[site]?.effective_share ?? 0) > 0 })), (c) => c.takes)
    return [site, new Map(connectors.map((c, i) => [c.connector_id, would ? would[i].share : c.cells[site]?.effective_share ?? 0]))]
  }))
  const places = data?.places ?? []
  // A picked place that dropped out of the list stays picked, and listed.
  const placeOptions = !place || places.some((p) => p.place === place) ? places : [{ place, label: placeLabel(place), reports: 0 }, ...places]
  const pickedConnector = picked ? data?.connectors.find((c) => c.connector_id === picked.connectorId) : undefined
  const pickedCell = picked && pickedConnector ? pickedConnector.cells[picked.site] : undefined
  return (
    <Card className="px-4 py-3">
      <CardHeader
        title={
          <span className="inline-flex items-center gap-1.5">
            Connectors by site
            <InfoTip label="About connectors by site">
              Each cell is the connector's share of the site's requests at its effective weight: its weight times its reputation there. A dynamic connector's reputation is one less its block rate for the site, from the place asked for or the nearest coarser one with enough evidence; a connector with IPs has the share of its IPs not avoided for the site. No location means requests that name no place, for which a dynamic connector has a record of its own.
            </InfoTip>
          </span>
        }
        action={
          // A picker that does not fit beside its label goes under it rather than cutting its text.
          <label className="flex flex-wrap items-center gap-x-1.5 gap-y-1 min-w-0 max-w-full text-xs text-fg-muted">
            <span className="flex-none">Requests from</span>
            <Select value={place} onChange={(e) => { onPlace(e.target.value); setPicked(null) }} className={PICKER} aria-label="Requests from location">
              <option value="">No location</option>
              {placeOptions.map((p) => <option key={p.place} value={p.place}>{p.label}</option>)}
            </Select>
          </label>
        }
        className="mb-2"
      />
      {loadFailed(query) ? (
        <LoadError what="the connectors by site" query={query} />
      ) : !data ? (
        <LogoLoader size="sm" className="py-4" />
      ) : data.sites.length === 0 ? (
        // The sites are the project's busiest whatever the place, so none means no connector has a report yet.
        <p className="text-xs text-fg-subtle py-2">No connector has reports yet.</p>
      ) : (
        <Reloading active={query.isPlaceholderData}>
          <div className="overflow-x-auto -mx-4" role="region" aria-label="Connectors by site, scrolls sideways" tabIndex={0}>
            <table className="min-w-full border-separate border-spacing-0 text-[12.5px]">
              <thead>
                <tr className="text-[11px] text-fg-subtle">
                  <th scope="col" className={cn(CELL, FIRST, 'font-medium')}>Connector</th>
                  <th scope="col" className={cn(CELL, 'font-medium')}>Weight</th>
                  {data.sites.map((site) => (
                    <th key={site} scope="col" className={cn(CELL, 'font-medium max-w-[12rem] truncate')} title={site}>{site}</th>
                  ))}
                  <th aria-hidden className="w-2 border-b border-line" />
                </tr>
              </thead>
              <tbody>
                {data.connectors.map((c) => (
                  <tr key={c.connector_id}>
                    <th scope="row" className={cn(CELL, FIRST, 'font-normal')}>
                      {/* Narrow on a phone, so the sites keep most of the width beside it. */}
                      <span className="flex items-center gap-2">
                        <ProviderLogo type={c.credential_type} className="w-4 h-4 text-[16px] flex-none" />
                        <span className="min-w-0">
                          <span className="block truncate max-w-[6.5rem] sm:max-w-[11rem]" title={c.name}>{c.name}</span>
                          {c.dynamic && <span className="block text-[10.5px] leading-tight text-fg-subtle">dynamic</span>}
                        </span>
                      </span>
                    </th>
                    <td className={cn(CELL, 'tabular-nums text-fg-muted')}>{c.weight}</td>
                    {data.sites.map((site) => (
                      <SiteCell
                        key={site}
                        connector={c}
                        site={site}
                        share={shares.get(site)?.get(c.connector_id) ?? 0}
                        place={place}
                        places={places}
                        picked={picked?.connectorId === c.connector_id && picked.site === site}
                        onPick={() => setPicked(picked?.connectorId === c.connector_id && picked.site === site ? null : { connectorId: c.connector_id, site })}
                      />
                    ))}
                    <td aria-hidden className="w-2 border-b border-line" />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-fg-muted mt-2" aria-live="polite">
            {picked && pickedConnector && pickedCell
              ? reputationTitle(pickedConnector.name, pickedCell, picked.site, place, places)
              : `Each cell is the connector's expected share of that site's requests at its effective weight, with its reputation under it${applied ? '' : ': what Avoid would do, since the project does not avoid now'}. Tap or hover a cell for the reason.`}
          </p>
        </Reloading>
      )}
    </Card>
  )
}
