// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { AlertTriangle, Flag, Plus } from 'lucide-react'
import {
  fetchCountries, setCountryRoles,
  type ConnectRoute, type CountriesOverview, type CountryConnector, type CountryRole,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { useNavigationGuard } from '../contexts/NavigationGuardContext'
import { DataTable, type DataTableFeatures } from '../components/DataTable'
import { Page, EmptyState } from '../components/layout/Page'
import { RichSelect } from '../components/RichSelect'
import { Alert, Badge, Button, Card, InfoTip, Inspector, InspectorSection, QueryContent, Segmented } from '../components/ui'
import { COUNTRY_CODES, countryLabel } from '../utils/connect'
import { ConnectorLink, RouteList } from '../components/RouteList'

const ROLE_OPTIONS: { value: CountryRole; label: string }[] = [
  { value: 'off', label: 'Off' },
  { value: 'serves', label: 'Serves' },
  { value: 'reserved', label: 'Reserved' },
]

/** How a connector stands to a country, read off its settings the way the server writes them. */
function roleOf(c: CountryConnector, code: string): CountryRole {
  if (c.reserved_countries.includes(code)) return 'reserved'
  if (c.countries.length) return c.countries.includes(code) ? 'serves' : 'off'
  return c.excluded_countries.includes(code) ? 'off' : 'serves'
}

/** What a connector's country settings amount to, in a few words. */
function scopeText(c: CountryConnector): string {
  if (c.countries.length) return `Lists ${c.countries.length === 1 ? c.countries[0] : `${c.countries.length} countries`}`
  if (c.excluded_countries.length) return `All countries except ${c.excluded_countries.length}`
  return 'All countries'
}

/** One country's row: where its requests go now and who holds or excludes it. */
interface CountryRow {
  code: string
  routes: ConnectRoute[]
  reservedBy: CountryConnector[]
  excludedBy: CountryConnector[]
}

const percent = (share: number) => `${Math.round(share * 100)}%`

/** The connectors a request would reach, each with its expected share. */
function RouteChips({ routes }: { routes: ConnectRoute[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {routes.map((route) => (
        <span
          key={route.connector_id}
          className={`inline-flex items-center gap-1.5 rounded-full border bg-surface-raised px-2 py-0.5 text-xs max-w-full ${route.on_demand ? 'border-dashed border-line-strong' : 'border-line'}`}
          title={route.on_demand ? 'Creates sessions for the country on the first request' : route.dynamic ? 'Dynamic sessions: the country is rendered into each vendor session' : `${route.proxies} ${route.proxies === 1 ? 'proxy' : 'proxies'}`}
        >
          <span className="font-medium text-fg truncate">{route.connector_name}</span>
          <span className="tabular-nums text-fg-muted flex-none">{percent(route.share)}</span>
        </span>
      ))}
    </div>
  )
}

/** Why a country's requests fail now: its reservation, connectors set to serve it that cannot now, or none set to. */
function failureText(code: string, connectors: CountryConnector[]): string {
  const named = (list: CountryConnector[]) => list.map((c) => (c.enabled ? c.name : `${c.name} (disabled)`)).join(', ')
  const reservedBy = connectors.filter((c) => c.reserved_countries.includes(code))
  if (reservedBy.length) return `${code} is reserved for ${named(reservedBy)}, and none of them can serve it now, so requests for it fail.`
  const serving = connectors.filter((c) => roleOf(c, code) !== 'off')
  if (serving.length) {
    return `${named(serving)} may serve ${code}, but none of them can now: disabled, no healthy proxy or exit there, over a traffic limit, or rate limited. Requests for it fail until one can.`
  }
  return `No connector is set to serve ${code}, so requests for it fail. Set a connector to Serves or Reserved below.`
}

export default function CountriesPage() {
  const queryClient = useQueryClient()
  const { selectedProjectId } = useProject()
  const { canMutate } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [panel, setPanel] = useState<string | 'new' | null>(null)

  const query = useQuery({
    queryKey: ['countries', selectedProjectId],
    queryFn: () => fetchCountries(selectedProjectId!),
    enabled: !!selectedProjectId,
  })
  const overview = query.data

  // Deep link: ?country=<code>, UK read as GB like the server does; anything that is no country code is ignored.
  useEffect(() => {
    const code = searchParams.get('country')
    if (code) {
      const wanted = code.toUpperCase() === 'UK' ? 'GB' : code.toUpperCase()
      if (COUNTRY_CODES.includes(wanted)) setPanel(wanted)
      searchParams.delete('country')
      setSearchParams(searchParams, { replace: true })
    }
  }, [searchParams])

  const rows: CountryRow[] = useMemo(() => (overview?.countries ?? []).map(({ country, routes }) => ({
    code: country,
    routes,
    reservedBy: overview!.connectors.filter((c) => c.reserved_countries.includes(country)),
    excludedBy: overview!.connectors.filter((c) => c.excluded_countries.includes(country)),
  })), [overview])

  // Two columns, the rule under the country, so a phone keeps where requests go in view.
  const columns = useMemo<ColumnDef<DataTableFeatures, CountryRow>[]>(() => [
    {
      id: 'country',
      accessorFn: (row) => countryLabel(row.code),
      header: 'Country',
      size: 140,
      cell: ({ row }) => {
        const { code, reservedBy, excludedBy } = row.original
        return (
          <div className="min-w-0 py-1.5">
            {/* Tags wrap under the code rather than being cut off by the narrow column. */}
            <div className="flex flex-wrap items-center gap-1.5 gap-y-1">
              <span className="font-mono text-xs font-medium text-fg">{code}</span>
              {reservedBy.length > 0 && <Badge color="blue" className="py-0 text-[11px]" title={`Reserved for ${reservedBy.map((c) => c.name).join(', ')}`}>Reserved</Badge>}
              {excludedBy.length > 0 && <Badge color="gray" className="py-0 text-[11px]" title={`Excluded by ${excludedBy.map((c) => c.name).join(', ')}`}>Excluded{excludedBy.length > 1 ? ` ×${excludedBy.length}` : ''}</Badge>}
            </div>
            {/* The name wraps rather than truncates: a wider column would push the routes off a phone's screen. */}
            <div className="whitespace-normal text-[13px] text-fg-muted">{countryLabel(code)}</div>
          </div>
        )
      },
    },
    {
      id: 'routes',
      header: 'Requests go to',
      enableSorting: false,
      // Chips wrap, so less room than other columns keeps both columns on a 360 px phone without sideways scrolling.
      meta: { minWidth: 130 },
      cell: ({ row }) => row.original.routes.length
        ? <RouteChips routes={row.original.routes} />
        : <span className="inline-flex items-center gap-1.5 text-xs text-danger" title={failureText(row.original.code, overview?.connectors ?? [])}><AlertTriangle className="w-3.5 h-3.5 flex-none" /> Requests fail</span>,
    },
  ], [overview])

  const onSaved = (next: CountriesOverview) => {
    queryClient.setQueryData(['countries', selectedProjectId], next)
    queryClient.invalidateQueries({ queryKey: ['connectors', selectedProjectId] })
    queryClient.invalidateQueries({ queryKey: ['traffic-split', selectedProjectId] })
  }

  const panelNode = overview && panel ? (
    <CountryPanel
      key={panel}
      code={panel === 'new' ? null : panel}
      overview={overview}
      canMutate={canMutate}
      onClose={() => setPanel(null)}
      onPick={(code) => setPanel(code)}
      onSaved={onSaved}
    />
  ) : null

  return (
    <Page
      title="Countries"
      count={overview?.countries.length}
      subtitle="Which connectors serve each country, and where a request naming it goes now"
      actions={canMutate && overview && overview.connectors.length > 0 ? <Button size="sm" onClick={() => setPanel('new')}><Plus className="w-3.5 h-3.5" /> Add country</Button> : undefined}
      panel={panelNode}
    >
      <QueryContent query={query} what="the countries">
        {overview && overview.connectors.length === 0 ? (
          <EmptyState
            icon={<Flag />}
            title="No connectors yet"
            description="Countries are served by connectors. Add one, then come back to choose which countries each serves."
            action={<Link to={`/projects/${selectedProjectId}/connectors`}><Button size="sm">Open connectors</Button></Link>}
          />
        ) : overview && (
          <>
            <div className="grid grid-cols-1 @2xl:grid-cols-2 gap-3">
              <SummaryCard
                title="Any other country"
                hint="A country no connector lists, reserves, excludes or has an exit in."
                routes={overview.other}
                empty="Nothing serves other countries: only the countries below are served."
              />
              <SummaryCard
                title="No country named"
                hint="Requests without -cc-. They keep off the exits of reserved countries."
                routes={overview.untargeted}
                empty="Nothing serves requests that name no country now."
              />
            </div>
            <DataTable
              columns={columns}
              data={rows}
              getRowId={(row) => row.code}
              onRowClick={(row) => setPanel(row.code)}
              activeRowId={panel && panel !== 'new' ? panel : null}
              emptyMessage="No connector lists, reserves or excludes a country, or has an exit in one yet."
            />
          </>
        )}
      </QueryContent>
    </Page>
  )
}

function SummaryCard({ title, hint, routes, empty }: { title: string; hint: string; routes: ConnectRoute[]; empty: string }) {
  return (
    <Card className="p-3.5">
      <div className="flex items-center gap-1.5 mb-2">
        <h2 className="text-[13px] font-semibold text-fg">{title}</h2>
        <InfoTip label={title}>{hint}</InfoTip>
      </div>
      {routes.length ? <RouteChips routes={routes} /> : <p className="text-xs text-fg-muted">{empty}</p>}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Inspector: one country across every connector, saved in one write.

function CountryPanel({ code: given, overview, canMutate, onClose, onPick, onSaved }: {
  /** The country, or null to pick one first. */
  code: string | null
  overview: CountriesOverview
  canMutate: boolean
  onClose: () => void
  onPick: (code: string) => void
  onSaved: (next: CountriesOverview) => void
}) {
  const { selectedProjectId } = useProject()
  const toast = useToast()
  const [edits, setEdits] = useState<Record<string, CountryRole>>({})
  const [error, setError] = useState<string | null>(null)

  const listed = new Set(overview.countries.map((row) => row.country))
  const code = given
  const row = code ? overview.countries.find((r) => r.country === code) : undefined
  // A country nothing names yet routes like any other.
  const routes = row ? row.routes : overview.other
  const saved = (c: CountryConnector) => roleOf(c, code ?? '')
  const roleFor = (c: CountryConnector) => edits[c.id] ?? saved(c)
  const changed = overview.connectors.filter((c) => edits[c.id] !== undefined && edits[c.id] !== saved(c))
  const dirty = changed.length > 0
  useNavigationGuard(dirty, 'You have unsaved country changes.')

  // A connector listing only this country would list none, and so serve every country.
  const lastListed = changed.filter((c) => c.countries.length === 1 && c.countries[0] === code && roleFor(c) === 'off')
  const reservedBy = overview.connectors.filter((c) => c.reserved_countries.includes(code ?? ''))

  const mutation = useMutation({
    mutationFn: () => setCountryRoles(selectedProjectId!, code!, Object.fromEntries(changed.map((c) => [c.id, roleFor(c)]))),
    onSuccess: (next) => { onSaved(next); setEdits({}); setError(null); toast.show(`${code} saved`) },
    onError: (e: Error) => setError(e.message || 'Failed to save the country'),
  })

  if (!code) {
    const options = COUNTRY_CODES.filter((c) => !listed.has(c)).map((c) => ({ value: c, label: countryLabel(c), description: c }))
    return (
      <Inspector title="Add country" subtitle="Choose a country to set up" onClose={onClose}>
        <div className="p-4 space-y-3">
          <RichSelect options={options} value="" onChange={(c) => c && onPick(c)} placeholder="Choose a country" searchable />
          <p className="text-xs text-fg-muted">Countries already on the page are left out: open them from the table.</p>
        </div>
      </Inspector>
    )
  }

  const effect = (c: CountryConnector): string | null => {
    const from = saved(c)
    const to = roleFor(c)
    if (from === to) return null
    const parts: string[] = []
    if (c.countries.length) {
      if (from === 'off') parts.push(`Adds ${code} to its country list${c.provisions_per_country ? ', and provisions proxies there' : ''}`)
      if (to === 'off') parts.push(`Takes ${code} off its country list${c.provisions_per_country ? ', and drops its proxies there' : ''}`)
    } else {
      if (to === 'off') parts.push(`Excludes ${code}`)
      if (from === 'off') parts.push(`Stops excluding ${code}`)
    }
    if (to === 'reserved') parts.push(`Reserves ${code}`)
    if (from === 'reserved') parts.push(`Stops reserving ${code}`)
    return parts.join('. ') + '.'
  }
  const status = (c: CountryConnector): string | null => {
    const role = saved(c)
    if (role === 'off') return null
    if (!c.enabled) return 'Disabled: takes no requests'
    if (role === 'serves' && reservedBy.length) return `Held back while ${code} is reserved`
    if (!routes.some((r) => r.connector_id === c.id)) return `Nothing to serve ${code} with now`
    return null
  }

  return (
    <Inspector
      title={countryLabel(code)}
      subtitle={code}
      onClose={onClose}
      footer={canMutate ? (
        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setEdits({})} disabled={!dirty || mutation.isPending}>Reset</Button>
          <Button size="sm" onClick={() => mutation.mutate()} disabled={!dirty || lastListed.length > 0 || mutation.isPending}>{mutation.isPending ? 'Saving…' : 'Save'}</Button>
        </div>
      ) : undefined}
    >
      <div className="p-4 space-y-5">
        <InspectorSection title={`Requests for ${code} now`}>
          {routes.length ? (
            <RouteList projectId={selectedProjectId!} routes={routes} />
          ) : (
            <Alert variant="warning" className="text-xs flex gap-2">
              <AlertTriangle className="w-4 h-4 flex-none mt-0.5" />
              <span>{failureText(code, overview.connectors)}</span>
            </Alert>
          )}
          {!row && <p className="text-xs text-fg-subtle">No connector lists, reserves or excludes {code} yet, and none has an exit there, so it is served like any other country.</p>}
        </InspectorSection>

        <InspectorSection
          title={
            <span className="inline-flex items-center gap-1.5">
              Connectors
              <InfoTip label="About the roles">
                Off: the connector never serves {code}; for a connector listing countries it is off the list, for one serving all countries it is excluded. Serves: the connector may serve {code}, sharing it by weight. Reserved: requests for {code} go only to the connectors reserving it, split by weight, and fail when none of them can serve it.
              </InfoTip>
            </span>
          }
        >
          <ul className="divide-y divide-line">
            {overview.connectors.map((c) => {
              const role = roleFor(c)
              const note = effect(c) ?? status(c)
              return (
                <li key={c.id} className="py-2.5 flex flex-wrap items-center gap-x-3 gap-y-2">
                  <div className="flex-1 min-w-40">
                    <div className="flex items-center gap-2 min-w-0">
                      <ConnectorLink projectId={selectedProjectId!} connectorId={c.id} className="text-[13px]">{c.name}</ConnectorLink>
                      {!c.enabled && <Badge color="gray">Disabled</Badge>}
                    </div>
                    <div className="text-xs text-fg-muted">{scopeText(c)}{c.dynamic ? ' · dynamic sessions' : ''} · weight {c.weight}</div>
                    {note && <div className={`text-xs mt-0.5 ${effect(c) ? 'text-primary' : 'text-fg-subtle'}`}>{note}</div>}
                  </div>
                  {canMutate
                    ? <Segmented<CountryRole> size="sm" options={ROLE_OPTIONS} value={role} onChange={(next) => setEdits((prev) => ({ ...prev, [c.id]: next }))} />
                    : <Badge color={role === 'reserved' ? 'blue' : role === 'serves' ? 'green' : 'gray'}>{ROLE_OPTIONS.find((o) => o.value === role)?.label}</Badge>}
                </li>
              )
            })}
          </ul>
          {lastListed.map((c) => (
            <Alert key={c.id} variant="error" className="text-xs">
              {c.name} lists only {code}; taking it off would make it serve every country. Disable or edit the connector instead.
            </Alert>
          ))}
          {error && <Alert variant="error" className="text-xs">{error}</Alert>}
        </InspectorSection>
      </div>
    </Inspector>
  )
}
