// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from '@tanstack/react-query'
import { fetchExitIpReputation, type ExitIpReputation, type ExitReputationRecord } from '../../api/client'
import { useProject } from '../../contexts/ProjectContext'
import { Badge, Inspector, InspectorSection, KeyValue, LogoLoader, QueryContent } from '../ui'
import { formatDateTime, parseApiDate, relativeTime, timeUntil } from '../../utils/format'
import { cn } from '../../utils/cn'

/** An end as a pill or a row shows it: the time of day when it is today, else the date and time, in the viewer's zone. */
export function endLabel(value: string): string {
  const d = parseApiDate(value)
  if (!d) return '-'
  const today = d.toDateString() === new Date().toDateString()
  return d.toLocaleString(undefined, today ? { hour: 'numeric', minute: '2-digit' } : { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

/** ``text`` that a line never breaks inside, but after a comma ("Nov 16, | 2:38 AM"). */
const unbroken = (text: string) => text.replace(/ /g, ' ').replace(/, /g, ', ')

/** An end with how far off it is, or that it has passed: "until 14:30 (in 3 h)", "ended 2 d ago". */
function endText(value: string): string {
  const d = parseApiDate(value)
  if (!d) return '-'
  return d.getTime() > Date.now() ? `until ${unbroken(endLabel(value))} ${unbroken(`(${timeUntil(value)})`)}` : `ended ${relativeTime(value)}`
}

/** An end in a key-value row, wrapping on a narrow screen rather than cutting off how far away it is. */
function End({ value, title }: { value: string; title?: string }) {
  return <span className="whitespace-normal" title={title}>{endText(value)}</span>
}

/** Where a record stands now: avoided until an end, or a dash. */
export function ReputationPill({ record, className }: { record: Pick<ExitReputationRecord, 'status' | 'avoided_until'>; className?: string }) {
  if (record.status === 'avoided' && record.avoided_until) {
    return (
      <Badge color="red" className={cn('whitespace-nowrap py-0.5', className)} title={`Avoided for the site until ${formatDateTime(record.avoided_until)} (${timeUntil(record.avoided_until)})`}>
        Avoided until {endLabel(record.avoided_until)}
      </Badge>
    )
  }
  return <span className={cn('text-fg-subtle', className)}>-</span>
}

/** A decayed count as a short number: 0.97 stays readable, 12.4 drops a digit, and nothing reads 0.00. */
function recent(value: number): string {
  if (value >= 10) return value.toFixed(1).replace(/\.0$/, '')
  if (value > 0 && value < 0.01) return '<0.01'
  return value.toFixed(2).replace(/\.?0+$/, '') || '0'
}

/** A count in its colour when there is any, muted at zero. */
function Count({ value, text, tone }: { value: number; text: string; tone?: string }) {
  return <td className={cn('py-1 pl-2 text-right tabular-nums', value > 0 ? tone : 'text-fg-subtle')}>{text}</td>
}

/**
 * The counts that decide a record, in a small table that fits a phone: reports over the record's life, and the recent
 * ones, faded by "Remember for", that decide the avoid; then the recent avoids a next one doubles by.
 */
function Counts({ record }: { record: ExitReputationRecord }) {
  const life = (n: number) => n.toLocaleString()
  return (
    <div>
      <table className="w-full text-[12px]">
        <thead>
          <tr className="text-[11px] text-fg-muted">
            <th className="text-left font-normal" />
            <th className="text-right font-normal pl-2">good</th>
            <th className="text-right font-normal pl-2">bad</th>
          </tr>
        </thead>
        <tbody>
          <tr className="border-t border-line">
            <td className="py-1 text-fg-muted">All time</td>
            <Count value={record.good_reports} text={life(record.good_reports)} tone="text-success" />
            <Count value={record.bad_reports} text={life(record.bad_reports)} tone="text-danger" />
          </tr>
          <tr className="border-t border-line" title="Faded by the project's Remember for to now: these decide">
            <td className="py-1 text-fg-muted">Recent</td>
            <Count value={record.weighted_good} text={recent(record.weighted_good)} tone="text-success" />
            <Count value={record.weighted_bad} text={recent(record.weighted_bad)} tone="text-danger" />
          </tr>
        </tbody>
      </table>
      {record.weighted_avoids > 0 && (
        <p className="text-[11px] text-fg-subtle mt-1">Recent avoids: {recent(record.weighted_avoids)}; the next lasts Avoid for doubled that many times, rounded.</p>
      )}
    </div>
  )
}

/** One site's record of an IP: the status and its ends, and unless compact the latest report and the counts that decide. */
export function SiteRecord({ record, compact = false }: { record: ExitReputationRecord; compact?: boolean }) {
  return (
    <div className="rounded-lg border border-line p-3 space-y-1.5" data-site={record.site}>
      <div className="flex items-center justify-between gap-2 min-w-0">
        <span className="font-medium text-[13px] truncate" title={record.site}>{record.site}</span>
        <ReputationPill record={record} className="flex-none" />
      </div>
      <div>
        {record.avoid_until && <KeyValue label="Last avoid" value={<End value={record.avoid_until} />} />}
        {!compact && (
          <>
            <KeyValue
              label="Last report"
              value={record.last_report_at ? <span title={`${formatDateTime(record.last_report_at)}${record.last_reason ? ` · ${record.last_reason}` : ''}`}>{relativeTime(record.last_report_at)}{record.last_reason && <span className="text-fg-muted font-normal"> · {record.last_reason}</span>}</span> : <span className="text-fg-subtle">none</span>}
            />
            <KeyValue label="Seen" value={<span title={`First ${formatDateTime(record.first_seen_at)}, last ${formatDateTime(record.last_seen_at)}`}>{relativeTime(record.first_seen_at)} to {relativeTime(record.last_seen_at)}</span>} />
          </>
        )}
      </div>
      {!compact && <Counts record={record} />}
    </div>
  )
}

/** The project's connectors that handed the IP out, the one that saw it last first. */
function ServedBy({ reputation }: { reputation: ExitIpReputation }) {
  if (reputation.connectors.length === 0) return <p className="text-xs text-fg-subtle">No connector of this project has handed out this IP within the exit IP retention window.</p>
  return (
    <ul className="divide-y divide-line">
      {reputation.connectors.map((c) => (
        <li key={c.connector_id} className="flex items-center gap-2 py-1.5 text-[13px]">
          <span className="min-w-0 truncate" title={c.connector_name}>{c.connector_name}</span>
          <span className="ml-auto flex-none text-xs text-fg-muted tabular-nums" title={`First ${formatDateTime(c.first_seen)}, last ${formatDateTime(c.last_seen)}`}>
            {c.sightings.toLocaleString()} {c.sightings === 1 ? 'hand-out' : 'hand-outs'} · last {relativeTime(c.last_seen)}
          </span>
        </li>
      ))}
    </ul>
  )
}

/** Where the rest are when an IP has more sites than one answer lists (a wide crawl's exit): the Reputation tab. */
function MoreSites({ reputation }: { reputation: ExitIpReputation }) {
  if (reputation.records.length >= reputation.sites) return null
  return (
    <p className="text-xs text-fg-muted">
      Showing {reputation.records.length} of {reputation.sites.toLocaleString()} sites, those in force first, then the latest seen. The Reputation tab of Exit locations lists every one: filter it by this IP.
    </p>
  )
}

/** What a reader of a record-mode project's records is told: the decisions are shown, not acted on. */
const RECORD_MODE_NOTE = ' With the mode record these are what avoid would do; routing does not act on them.'

/** Whether the project is the selected one and its exit reputation only records. */
function useRecordOnly(projectId: string): boolean {
  const { selectedProject } = useProject()
  return selectedProject?.id === projectId && selectedProject.exit_reputation === 'record'
}

/** Every site's record of one exit IP, with its numbers and ends, and the connectors that served it. */
export function ExitIpReputationInspector({ projectId, ip, onClose }: { projectId: string; ip: string; onClose: () => void }) {
  const query = useQuery({
    queryKey: ['exit-reputation-ip', projectId, ip],
    queryFn: () => fetchExitIpReputation(projectId, ip),
    refetchInterval: 60_000,
  })
  const recordOnly = useRecordOnly(projectId)
  const sites = query.data?.sites ?? 0
  const avoided = query.data?.avoided_sites ?? 0
  return (
    <Inspector
      title={<span className="font-mono text-[14px]">{ip}</span>}
      subtitle={query.data ? `${sites.toLocaleString()} ${sites === 1 ? 'site' : 'sites'}${avoided ? `, avoided by ${avoided.toLocaleString()}` : ''}` : 'Exit reputation'}
      onClose={onClose}
    >
      <QueryContent query={query} what="the exit's records">
        {(reputation) => (
          <>
            <InspectorSection title="Sites">
              {reputation.records.length === 0
                ? <p className="text-xs text-fg-subtle">Nothing recorded for this IP.</p>
                : <div className="space-y-2"><MoreSites reputation={reputation} />{reputation.records.map((r) => <SiteRecord key={r.site} record={r} />)}</div>}
              <p className="text-xs text-fg-muted">Counts are for the record's life; recent ones fade by the project's Remember for and decide the avoid. Bad reports avoid the exit for the site wherever it serves.{recordOnly && RECORD_MODE_NOTE}</p>
            </InspectorSection>
            <InspectorSection title="Seen behind">
              <ServedBy reputation={reputation} />
            </InspectorSection>
          </>
        )}
      </QueryContent>
    </Inspector>
  )
}

/** A static row's exit reputation in the proxy inspector: each site of its exit IP with its status and ends. */
export function ProxyReputationSection({ projectId, ip }: { projectId: string; ip: string }) {
  const query = useQuery({
    queryKey: ['exit-reputation-ip', projectId, ip],
    queryFn: () => fetchExitIpReputation(projectId, ip),
    refetchInterval: 60_000,
  })
  const recordOnly = useRecordOnly(projectId)
  return (
    <InspectorSection title="Exit reputation">
      <QueryContent query={query} what="the exit's records" loader={<LogoLoader size="sm" className="py-4" />}>
        {(reputation) => reputation.records.length === 0 ? (
          <p className="text-xs text-fg-subtle">Nothing recorded for its exit <span className="font-mono">{ip}</span> yet: no exit reports.</p>
        ) : (
          <div className="space-y-2">
            <p className="text-xs text-fg-muted">
              By its exit <span className="font-mono">{ip}</span>, per site.{recordOnly && RECORD_MODE_NOTE}
            </p>
            <MoreSites reputation={reputation} />
            {reputation.records.map((r) => <SiteRecord key={r.site} record={r} compact />)}
          </div>
        )}
      </QueryContent>
    </InspectorSection>
  )
}
