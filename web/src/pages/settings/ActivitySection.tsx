// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Download, History, Lock } from 'lucide-react'
import {
  exportActivity, fetchActivity, fetchAuditRetention, fetchProjects, updateAuditRetention,
  type ActivityFilters, type AuditEntityType, type AuditEntry, type AuditRetention, type LicenseStatus,
} from '../../api/client'
import { AdminLink } from '../../components/AdminLink'
import { AuditEntryView } from '../../components/audit/EntityHistory'
import { HistoryButton } from '../../components/audit/HistoryButton'
import { DataTable, type DataTableFeatures } from '../../components/DataTable'
import { EnterpriseCard, type EnterpriseFeature } from '../../components/EnterpriseCard'
import { UntilPicker } from '../../components/geo/UntilPicker'
import { EmptyState, Page } from '../../components/layout/Page'
import { useToast } from '../../contexts/ToastContext'
import { activityReadable, isEnterprise, useEdition, useLicense } from '../../hooks/useEdition'
import { ACTION_LABELS, ENTITY_LABELS, actionLabel, actionTone, entityLabel } from '../../utils/audit'
import { cn } from '../../utils/cn'
import { formatDateTime, relativeTime } from '../../utils/format'
import {
  Alert, Badge, Button, Card, CardHeader, CopyButton, Input, Inspector, InspectorSection, KeyValue, LogoLoader, QueryContent, Select,
} from '../../components/ui'
import { LoadError, loadFailed } from '../../components/ui/LoadError'

// The most days the api keeps entries for, short of forever (0).
const MAX_RETENTION_DAYS = 36_500

// Tailwind's @lg container width: narrower, the log leaves out the project and the address.
const WIDE_MIN_PX = 512

const ACTIVITY_FEATURES: EnterpriseFeature[] = [
  { title: 'Sign-ins', text: 'Every sign-in and failed attempt, with its address and browser.' },
  { title: 'API tokens', text: 'Each token issued, first used and revoked.' },
  { title: 'One log', text: 'Every change across the install on this page, filtered by who, what, project and time.' },
  { title: 'Export', text: 'JSON lines for your SIEM.' },
  { title: 'Log lines', text: 'An audit line per entry in the server logs.' },
]

// Filters and their buttons: 16 px text and 44 px targets on touch screens, compact under a mouse.
const FILTER_CONTROL = 'py-1.5 text-base pointer-fine:text-[13px] pointer-coarse:h-11'

/**
 * Administration, Activity: how long the audit log keeps its entries, for every edition, and with an Octoprox
 * Enterprise license every entry across the install, filtered, paged and exported. Enterprise endpoints are asked
 * only once the edition says so: the free image answers them with the web app's page.
 */
export default function ActivitySection() {
  const toast = useToast()
  const editionQuery = useEdition()
  const licenseQuery = useLicense()
  const [filters, setFilters] = useState<ActivityFilters>({})
  const [selected, setSelected] = useState<AuditEntry | null>(null)
  const readable = isEnterprise(editionQuery.data) && licenseQuery.data !== undefined && activityReadable(licenseQuery.data)

  const exportMutation = useMutation({
    mutationFn: () => exportActivity(filters),
    onError: (e: Error) => toast.show(e.message || 'Export failed', 'error'),
  })

  return (
    <Page
      title="Activity"
      subtitle={readable
        ? 'Every change, sign-in and token event across the install, newest first, and how long they are kept.'
        : 'How long the audit log keeps its entries, and where to read them.'}
      actions={readable ? (
        <Button variant="outline" size="sm" onClick={() => exportMutation.mutate()} disabled={exportMutation.isPending} className="pointer-coarse:h-11" title="Every entry under the filters, oldest first, one JSON object per line">
          <Download className="w-3.5 h-3.5" /> {exportMutation.isPending ? 'Exporting…' : 'Export JSON lines'}
        </Button>
      ) : undefined}
      panel={readable && selected ? <EntryPanel key={selected.id} entry={selected} onClose={() => setSelected(null)} /> : null}
    >
      <QueryContent query={editionQuery} what="the edition">
        {(edition) => !isEnterprise(edition) ? (
          <WithoutLog aside={
            <EnterpriseCard
              title="Install-wide activity"
              intro="Octoprox Enterprise puts the whole install's audit log on this page, and records sign-ins and tokens too. It runs from its own image, with a license key entered under System."
              features={ACTIVITY_FEATURES}
            />
          } />
        ) : (
          <QueryContent query={licenseQuery} what="the license">
            {(license) => activityReadable(license)
              ? <ActivityLog filters={filters} onFiltersChange={setFilters} selectedId={selected?.id ?? null} onSelect={setSelected} />
              : <WithoutLog aside={<UnlicensedCard license={license} />} />}
          </QueryContent>
        )}
      </QueryContent>
    </Page>
  )
}

/** The page without the install-wide log: where each change shows instead, the retention, and what turns the log on. */
function WithoutLog({ aside }: { aside: ReactNode }) {
  return (
    <div className="grid gap-4 @4xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="space-y-4 min-w-0">
        <Alert variant="info" className="text-sm">
          Every change is recorded with who made it, when and the fields it changed. Projects, connectors, credentials, proxies, devices, users and custom providers show theirs on the History tab of their inspector, and install-wide settings on the History button of their card.
        </Alert>
        <RetentionCard />
      </div>
      {aside}
    </div>
  )
}

/** On the Enterprise image while no license allows the log: what is missing and where to enter a key. */
function UnlicensedCard({ license }: { license: LicenseStatus }) {
  const problem = license.state === 'invalid'
    ? `The stored license key is refused${license.refusal ? `: ${license.refusal}` : ''}.`
    : license.state === 'none' ? null : 'The license does not include the audit log.'
  return (
    <Card className="p-5 h-fit">
      <CardHeader title={<span className="inline-flex items-center gap-2"><Lock className="w-4 h-4 text-fg-subtle" /> Install-wide activity</span>} />
      {problem && <p className="text-sm text-danger mt-1 wrap-break-word">{problem}</p>}
      <p className="text-sm text-fg-muted mt-1">Enter a license key under <AdminLink page="system">System</AdminLink> to turn this page on.</p>
    </Card>
  )
}

/** How many days the audit log keeps its entries; the hourly compaction deletes older ones. */
function RetentionCard() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const retentionQuery = useQuery({ queryKey: ['audit-retention'], queryFn: fetchAuditRetention })
  const save = useMutation({
    mutationFn: (days: number) => updateAuditRetention(days),
    onSuccess: (saved) => {
      queryClient.setQueryData(['audit-retention'], saved)
      toast.show(saved.days === 0 ? 'Audit entries are kept forever' : `Audit entries are kept for ${saved.days.toLocaleString()} days`)
    },
    onError: (e: Error) => toast.show(e.message || 'Failed to save the retention', 'error'),
  })
  return (
    <Card className="p-4 @2xl:p-5">
      <CardHeader title="Retention" action={<HistoryButton entityType="system_settings" entityId="1" />} />
      <QueryContent query={retentionQuery} what="the retention" loader={<LogoLoader className="py-4" />}>
        {/* Keyed by the stored value, so a save or another admin's starts the field afresh. */}
        {(retention) => <RetentionForm key={retention.days} retention={retention} saving={save.isPending} onSave={(days) => save.mutate(days)} />}
      </QueryContent>
    </Card>
  )
}

function RetentionForm({ retention, saving, onSave }: { retention: AuditRetention; saving: boolean; onSave: (days: number) => void }) {
  const [text, setText] = useState(String(retention.days))
  const days = /^\d+$/.test(text.trim()) ? Number(text.trim()) : NaN
  const valid = days >= 0 && days <= MAX_RETENTION_DAYS
  return (
    <form className="mt-1 space-y-1.5" onSubmit={(e) => { e.preventDefault(); if (valid) onSave(days) }}>
      {/* The words stay together, and the field with its unit and Save, so a narrow card wraps between the two. */}
      <div className="flex items-center gap-x-2 gap-y-1 flex-wrap text-sm text-fg">
        <label htmlFor="audit-retention" className="whitespace-nowrap">Keep entries for</label>
        <span className="inline-flex items-center gap-2">
          <Input
            id="audit-retention"
            type="number"
            inputMode="numeric"
            min={0}
            max={MAX_RETENTION_DAYS}
            step={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            aria-invalid={!valid}
            className={cn('w-24 px-3 py-1.5 text-base pointer-fine:text-[13px] pointer-coarse:h-11 tabular-nums', !valid && 'border-danger text-danger')}
          />
          days
          <Button type="submit" size="sm" disabled={!valid || days === retention.days || saving} className="ml-1 pointer-coarse:h-11">
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </span>
      </div>
      <p className="text-[11px] text-fg-subtle">
        {valid ? '0 keeps them forever; the hourly compaction deletes older ones.' : `A whole number of days, from 0 to ${MAX_RETENTION_DAYS.toLocaleString()}.`}
      </p>
    </form>
  )
}

/** An element's width, kept current as it resizes; measured before the first paint. */
function useWidth(): [RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const read = () => setWidth(el.clientWidth)
    read()
    const observer = new ResizeObserver(read)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return [ref, width]
}

/**
 * The install-wide log, newest first, a page at a time with the exact total. Lists and selects apply as they are
 * set; the actor's name on Enter or Apply, and Until as the picker applies it.
 */
function ActivityLog({ filters, onFiltersChange, selectedId, onSelect }: {
  filters: ActivityFilters
  onFiltersChange: (filters: ActivityFilters) => void
  selectedId: string | null
  onSelect: (entry: AuditEntry) => void
}) {
  const [pageIndex, setPageIndex] = useState(0)
  const [pageSize, setPageSize] = useState(50)
  const [showRequestIds, setShowRequestIds] = useState(false)
  const [ref, width] = useWidth()
  const { data: projects } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects })
  const projectNames = useMemo(() => new Map((projects?.projects ?? []).map((p) => [p.id, p.name])), [projects])
  const activityQuery = useQuery({
    queryKey: ['activity', filters, pageIndex, pageSize],
    queryFn: () => fetchActivity(filters, pageIndex * pageSize, pageSize),
    // The page shown stays up, dimmed, while the next one loads.
    placeholderData: (previous) => previous,
  })
  const { data, isLoading, isPlaceholderData } = activityQuery

  // Any filter starts from the first page again.
  const change = (patch: ActivityFilters) => {
    setPageIndex(0)
    onFiltersChange({ ...filters, ...patch })
  }

  const columns: ColumnDef<DataTableFeatures, AuditEntry>[] = useMemo(() => [
    {
      id: 'time', accessorKey: 'created_at', header: 'Time', size: 112, enableSorting: false,
      cell: ({ getValue }) => <span title={formatDateTime(getValue<string>())}>{relativeTime(getValue<string>())}</span>,
    },
    {
      id: 'actor', accessorKey: 'actor', header: 'Actor', size: 136, enableSorting: false,
      cell: ({ getValue }) => getValue<string>() ? <span title={getValue<string>()}>{getValue<string>()}</span> : <span className="text-fg-subtle">-</span>,
    },
    {
      id: 'action', accessorKey: 'action', header: 'Action', size: 148, enableSorting: false,
      cell: ({ getValue }) => <Badge color={actionTone(getValue<string>())} className="py-0.5">{actionLabel(getValue<string>())}</Badge>,
    },
    {
      // Unsized: takes the room the others leave.
      id: 'entity', accessorKey: 'entity_name', header: 'Entity', enableSorting: false,
      cell: ({ row }) => {
        const { type, name } = entityNames(row.original)
        return (
          <span title={name ?? undefined}>
            <span className="text-fg-muted">{type}</span>
            {name && <> <span className="font-medium">{name}</span></>}
          </span>
        )
      },
    },
    {
      id: 'project', accessorKey: 'project_id', header: 'Project', size: 160, enableSorting: false,
      cell: ({ getValue }) => <ProjectName id={getValue<string | null>()} names={projectNames} />,
    },
    {
      id: 'from', accessorFn: (entry: AuditEntry) => clientIp(entry) ?? '', header: 'From', size: 136, enableSorting: false,
      cell: ({ getValue }) => getValue<string>() ? <span className="font-mono text-xs">{getValue<string>()}</span> : <span className="text-fg-subtle">-</span>,
    },
    {
      id: 'request_id', accessorKey: 'request_id', header: 'Request ID', size: 168, enableSorting: false,
      cell: ({ getValue }) => <span className="font-mono text-[11px] text-fg-muted" title={getValue<string | null>() ?? undefined}>{getValue<string | null>() ?? '-'}</span>,
    },
  ], [projectNames])

  // Narrow, or beside an open entry, the log keeps to who did what to which entity.
  const roomy = width >= WIDE_MIN_PX && selectedId === null
  const columnVisibility = { project: roomy, from: roomy, request_id: showRequestIds && selectedId === null }

  const rows = data?.entries ?? []
  const filtering = Object.values(filters).some((value) => value !== undefined)
  return (
    <>
      <RetentionCard />
      <div ref={ref} className="flex flex-col gap-3 min-w-0">
        <div className="flex flex-col @lg:flex-row @lg:flex-wrap @lg:items-center gap-2">
          <Select
            aria-label="Entity type"
            value={filters.entity_type ?? ''}
            onChange={(e) => change({ entity_type: (e.target.value || undefined) as AuditEntityType | undefined })}
            className={cn('@lg:w-48', FILTER_CONTROL)}
          >
            <option value="">All entities</option>
            {Object.entries(ENTITY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </Select>
          <Select
            aria-label="Action"
            value={filters.action ?? ''}
            onChange={(e) => change({ action: e.target.value || undefined })}
            className={cn('@lg:w-44', FILTER_CONTROL)}
          >
            <option value="">All actions</option>
            {Object.entries(ACTION_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </Select>
          <ActorFilter applied={filters.actor} onApply={(actor) => change({ actor })} />
          <Select
            aria-label="Project"
            value={filters.project_id ?? ''}
            onChange={(e) => change({ project_id: e.target.value || undefined })}
            className={cn('@lg:w-44', FILTER_CONTROL)}
          >
            <option value="">All projects</option>
            {(projects?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
          {/* The shared picker's presets are wider than a phone: they scroll sideways in their own row, not the page.
              On touch screens its buttons and fields get 44 px targets and 16 px text like the other filters. */}
          <div className="max-w-full overflow-x-auto [&_[role=radio]]:whitespace-nowrap pointer-coarse:[&_button]:min-h-11 pointer-coarse:[&_input]:h-11 pointer-coarse:[&_input]:text-base">
            <UntilPicker onChange={(until) => change({ until })} />
          </div>
          <label className="inline-flex items-center gap-2 text-[13px] text-fg-muted pointer-coarse:min-h-11 @lg:ml-auto">
            <input type="checkbox" checked={showRequestIds} onChange={(e) => setShowRequestIds(e.target.checked)} className="w-4 h-4" />
            Request IDs
          </label>
        </div>
        {/* A failed load is not an empty result: it says why, with a retry, in place of the table. */}
        {loadFailed(activityQuery) ? (
          <LoadError what="the activity" query={activityQuery} />
        ) : !isLoading && rows.length === 0 && !filtering ? (
          <EmptyState icon={<History />} reloading={isPlaceholderData} title="No activity yet" description="Each change made in Octoprox, each sign-in and each token event shows here as it happens." />
        ) : (
          <DataTable
            columns={columns}
            data={rows}
            getRowId={(entry) => entry.id}
            loading={isLoading}
            reloading={isPlaceholderData}
            emptyMessage="No entries match these filters."
            onRowClick={onSelect}
            activeRowId={selectedId}
            columnVisibility={columnVisibility}
            manualPagination={{
              pageIndex,
              pageSize,
              total: data?.total ?? 0,
              totalExact: true,
              onPageChange: setPageIndex,
              onPageSizeChange: (size) => { setPageSize(size); setPageIndex(0) },
            }}
          />
        )}
      </div>
    </>
  )
}

/** The actor's name, applied on Enter or Apply rather than on every key. */
function ActorFilter({ applied, onApply }: { applied: string | undefined; onApply: (actor: string | undefined) => void }) {
  const [text, setText] = useState(applied ?? '')
  const typed = text.trim() || undefined
  return (
    <form className="flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); onApply(typed) }}>
      <Input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Actor"
        aria-label="Actor"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        className={cn('flex-1 min-w-0 @lg:w-36 @lg:flex-none px-3', FILTER_CONTROL)}
      />
      <Button type="submit" size="sm" variant={typed !== applied ? 'primary' : 'secondary'} disabled={typed === applied} className="pointer-coarse:h-11">
        Apply
      </Button>
    </form>
  )
}

/** What an entry is about: its type, and its name unless it is only the type's (an install-wide singleton). */
function entityNames(entry: AuditEntry): { type: string; name: string | null } {
  const type = entityLabel(entry.entity_type)
  const name = entry.entity_name && entry.entity_name.toLowerCase() !== type.toLowerCase() ? entry.entity_name : null
  return { type, name }
}

/** The address a sign-in or a token's first use came from (Enterprise), when the entry has one. */
function clientIp(entry: AuditEntry): string | null {
  const ip = entry.details.client_ip
  return typeof ip === 'string' && ip ? ip : null
}

/** A project by its name, or its id once it is gone. */
function ProjectName({ id, names }: { id: string | null; names: Map<string, string> }) {
  if (!id) return <span className="text-fg-subtle">-</span>
  const name = names.get(id)
  return name ? <span title={name}>{name}</span> : <span className="font-mono text-[11px] text-fg-subtle" title={`Deleted project ${id}`}>{id.slice(0, 8)}</span>
}

/** One entry beside the log: what it changed, and the request id that finds it in the server logs. */
function EntryPanel({ entry, onClose }: { entry: AuditEntry; onClose: () => void }) {
  const { data: projects } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects })
  const project = entry.project_id ? projects?.projects.find((p) => p.id === entry.project_id) : undefined
  const { type, name } = entityNames(entry)
  const tokenId = entry.details.auth_token_id
  return (
    <Inspector
      title={name ?? type}
      subtitle={name ? `${type} · ${formatDateTime(entry.created_at)}` : formatDateTime(entry.created_at)}
      onClose={onClose}
    >
      <AuditEntryView entry={entry} />
      <InspectorSection title="Entry">
        <div>
          <KeyValue label="Entity" value={type} />
          {entry.entity_id && <IdRow label="Entity ID" id={entry.entity_id} />}
          {entry.project_id && <KeyValue label="Project" value={project?.name ?? `Deleted project ${entry.project_id.slice(0, 8)}`} />}
          {entry.request_id && <IdRow label="Request ID" id={entry.request_id} />}
          {/* Two tokens may share a name: the ID matches the one the token events name. */}
          {typeof tokenId === 'string' && tokenId && <IdRow label="Actor's token ID" id={tokenId} />}
        </div>
      </InspectorSection>
    </Inspector>
  )
}

/** A KeyValue row for an id, copyable, since ids are what the API and the server logs are searched by. */
function IdRow({ label, id }: { label: string; id: string }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5 border-b border-line last:border-b-0 text-[13px]">
      <span className="text-fg-muted flex-none">{label}</span>
      <span className="min-w-0 flex items-center gap-1">
        <span className="font-mono text-xs text-fg truncate" title={id}>{id}</span>
        <CopyButton text={id} label={`Copy ${label}`} className="pointer-coarse:p-[15px]" />
      </span>
    </div>
  )
}
