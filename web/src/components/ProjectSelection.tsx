// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import { Plus, Server, Trash2, FolderOpen, Settings, Pencil, LogOut, Link2, KeyRound } from 'lucide-react'
import {
  fetchProjects, fetchProjectsLoad, createProject, deleteProject, updateProject, logout,
  ProjectCreate, ProjectUpdate, ProjectSummary, ProjectLoad, HISTORY_RANGES, HistoryRange,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useTimeRange } from '../contexts/TimeRangeContext'
import { useTheme } from '../contexts/ThemeContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { setSettingsOrigin } from '../utils/settingsOrigin'
import { bytesShort, compact } from '../utils/format'
import { ProjectForm } from './ProjectForm'
import { DataTable, type DataTableFeatures } from './DataTable'
import { Page } from './layout/Page'
import { NewVersionBanner } from './NewVersion'
import { Sparkline } from './charts/Sparkline'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import { Button, Input, Card, Badge, Inspector, ConfirmDialog, Segmented } from './ui'

type PanelState = { kind: 'new' } | { kind: 'edit'; id: string } | null

/** A project with its load over the chosen range, once that has loaded. */
type ProjectRow = ProjectSummary & { load?: ProjectLoad }

type ProjectsView = 'tiles' | 'table'

const VIEW_KEY = 'octoprox_projects_view'

/** Whether the projects show as tiles or as a table, remembered in this browser. */
function useProjectsView(): [ProjectsView, (view: ProjectsView) => void] {
  const [view, setView] = useState<ProjectsView>(() => {
    try { return localStorage.getItem(VIEW_KEY) === 'table' ? 'table' : 'tiles' } catch { return 'tiles' }
  })
  const choose = (next: ProjectsView) => {
    setView(next)
    try { localStorage.setItem(VIEW_KEY, next) } catch { /* ignore */ }
  }
  return [view, choose]
}

function formatStrategy(strategy: string | undefined): string {
  return (strategy || 'round_robin').split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

function healthDotClass(project: ProjectSummary): string {
  const healthy = project.healthy_proxy_count
  const total = project.proxy_count
  return total === 0 ? 'bg-fg-subtle' : healthy === total ? 'bg-success' : healthy === 0 ? 'bg-danger' : 'bg-warning'
}

// The Overview's requests colour, literal so the trend reads the same on every theme.
const REQUESTS_COLOR = '#2563eb'

/** Requests per minute: one decimal under ten, whole and compact above; a trickle reads <0.1, not 0. */
function formatRpm(rpm: number): string {
  if (rpm <= 0) return '0'
  if (rpm < 0.1) return '<0.1'
  return rpm < 10 ? rpm.toFixed(1).replace(/\.0$/, '') : compact(Math.round(rpm))
}

/** The share of the range's requests that succeeded, 0-100; null when there were none. */
function successRate(load: ProjectLoad): number | null {
  return load.request_count > 0 ? (load.success_count / load.request_count) * 100 : null
}

function trafficBytes(load: ProjectLoad): number {
  return load.bytes_sent + load.bytes_received
}

/**
 * Project picker. Uses the same top bar, page frame and docked panel as the
 * rest of the app so creating or editing a project feels like everything else.
 */
export default function ProjectSelection() {
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const { setSelectedProjectId } = useProject()
  const { isDark } = useTheme()
  const { canMutate, authStatus } = useAuth()
  const toast = useToast()
  const [panel, setPanel] = useState<PanelState>(null)
  const [pendingDelete, setPendingDelete] = useState<ProjectSummary | null>(null)
  const [deleteConfirmation, setDeleteConfirmation] = useState('')
  const [view, setView] = useProjectsView()
  const [range, setRange] = useTimeRange(HISTORY_RANGES)

  const { data, isLoading, error } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects, refetchInterval: 30000 })
  const { data: loadData } = useQuery({
    queryKey: ['projects-load', range],
    queryFn: () => fetchProjectsLoad(range),
    refetchInterval: 30000,
    // The last range's numbers stay up while a new range loads, labelled with the range they are for.
    placeholderData: (previous) => previous,
  })

  const createMutation = useMutation({
    mutationFn: createProject,
    onSuccess: (p) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setPanel(null)
      toast.show(`Project "${p.name}" created`)
    },
  })
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: ProjectUpdate }) => updateProject(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setPanel(null)
      toast.show('Project saved')
    },
  })
  const deleteMutation = useMutation({
    mutationFn: ({ id, confirmation }: { id: string; confirmation: string }) => deleteProject(id, confirmation),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setPendingDelete(null)
      setDeleteConfirmation('')
      toast.show('Project deleted')
    },
    onError: (e: Error) => toast.show(e.message || 'Failed to delete project', 'error'),
  })

  const openProject = (project: ProjectSummary) => {
    setSelectedProjectId(project.id)
    navigate(`/projects/${project.id}/overview`)
  }
  const openSettings = () => {
    setSettingsOrigin(location.pathname)
    navigate('/settings')
  }
  const newProject = () => {
    createMutation.reset()
    setPanel({ kind: 'new' })
  }
  // Stable, so the table's columns are not rebuilt on every refetch.
  const resetUpdate = updateMutation.reset
  const editProject = useCallback((project: ProjectSummary) => {
    resetUpdate()
    setPanel({ kind: 'edit', id: project.id })
  }, [resetUpdate])

  const projects = data?.projects
  const rows: ProjectRow[] = useMemo(() => {
    const loads = new Map(loadData?.projects.map((load) => [load.project_id, load]))
    return (projects ?? []).map((project) => ({ ...project, load: loads.get(project.id) }))
  }, [projects, loadData])
  const editing = panel?.kind === 'edit' ? rows.find((p) => p.id === panel.id) : undefined

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'new') {
    panelNode = (
      <Inspector
        title="New project"
        subtitle="A project is one proxy endpoint with its own pool"
        onClose={() => setPanel(null)}
        footer={
          <>
            <span className="flex-1" />
            <Button type="button" variant="outline" size="sm" onClick={() => setPanel(null)}>Cancel</Button>
            <Button type="submit" form="project-form" size="sm" disabled={createMutation.isPending}>{createMutation.isPending ? 'Creating…' : 'Create project'}</Button>
          </>
        }
      >
        <ProjectForm key="new" onSave={(d) => createMutation.mutate(d as ProjectCreate)} error={createMutation.error?.message} formId="project-form" />
      </Inspector>
    )
  } else if (panel?.kind === 'edit' && editing) {
    panelNode = (
      <Inspector
        title="Project settings"
        subtitle={editing.name}
        onClose={() => setPanel(null)}
        footer={
          <>
            <Button type="button" variant="danger-ghost" size="sm" onClick={() => setPendingDelete(editing)}><Trash2 className="w-3.5 h-3.5" /> Delete</Button>
            <span className="flex-1" />
            <Button type="button" variant="outline" size="sm" onClick={() => setPanel(null)}>Cancel</Button>
            <Button type="submit" form="project-form" size="sm" disabled={updateMutation.isPending}>{updateMutation.isPending ? 'Saving…' : 'Save changes'}</Button>
          </>
        }
      >
        <ProjectForm key={editing.id} project={editing} onSave={(d) => updateMutation.mutate({ id: editing.id, data: d })} error={updateMutation.error?.message} formId="project-form" />
      </Inspector>
    )
  }

  return (
    <div className="h-screen flex flex-col bg-bg text-fg overflow-hidden">
      <NewVersionBanner />
      <header className="h-14 flex-none flex items-center justify-between px-5 border-b border-line bg-surface">
        <img src={isDark ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-[26px] w-auto" />
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={openSettings}><Settings className="w-3.5 h-3.5" /> Settings</Button>
          <div className="flex items-center gap-2 pl-2 border-l border-line">
            <div className="w-7 h-7 rounded-full bg-primary-soft text-primary-soft-fg flex items-center justify-center text-[11px] font-semibold uppercase">{(authStatus?.username ?? '?').slice(0, 2)}</div>
            <div className="leading-tight hidden sm:block">
              <div className="text-[13px] font-medium">{authStatus?.username}</div>
              <div className="text-[11px] text-fg-subtle">{authStatus?.role}</div>
            </div>
            <button onClick={() => logout()} className="p-1.5 rounded-md text-fg-muted hover:text-fg hover:bg-surface-raised transition-colors" title="Sign out">
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </div>
      </header>

      <div className="flex-1 min-h-0 flex">
        <Page
          title="Projects"
          count={data?.total}
          subtitle="Pick a project to manage, or create a new one"
          belowTitle={rows.length > 0 ? (
            <div className="flex items-center gap-2 flex-wrap">
              <Segmented size="sm" options={[{ value: 'tiles', label: 'Tiles' }, { value: 'table', label: 'Table' }]} value={view} onChange={setView} />
              <Segmented size="sm" options={HISTORY_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} />
            </div>
          ) : undefined}
          actions={canMutate ? <Button size="sm" onClick={newProject}><Plus className="w-3.5 h-3.5" /> New project</Button> : undefined}
          panel={panelNode}
        >
          {isLoading ? (
            <div className="text-sm text-fg-muted py-10 text-center">Loading projects…</div>
          ) : error ? (
            <div className="text-sm text-danger py-10 text-center">Failed to load projects</div>
          ) : rows.length === 0 ? (
            <Card className="p-10 text-center">
              <FolderOpen className="w-10 h-10 mx-auto mb-3 text-fg-subtle" />
              <h3 className="text-base font-medium">No projects yet</h3>
              <p className="text-sm text-fg-muted mt-1">Create your first project to get a proxy endpoint.</p>
              {canMutate && <Button size="sm" className="mt-4" onClick={newProject}><Plus className="w-3.5 h-3.5" /> New project</Button>}
            </Card>
          ) : view === 'table' ? (
            <ProjectTable
              projects={rows}
              activeId={panel?.kind === 'edit' ? panel.id : null}
              panelOpen={panelNode != null}
              canMutate={canMutate}
              onOpen={openProject}
              onEdit={editProject}
              onDelete={setPendingDelete}
            />
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(0,320px))] auto-rows-fr gap-4">
              {rows.map((project) => (
                <ProjectCard
                  key={project.id}
                  project={project}
                  range={loadData?.range ?? range}
                  active={panel?.kind === 'edit' && panel.id === project.id}
                  canMutate={canMutate}
                  onOpen={() => openProject(project)}
                  onEdit={() => editProject(project)}
                  onDelete={() => setPendingDelete(project)}
                />
              ))}
            </div>
          )}
        </Page>
      </div>

      {pendingDelete && (
        <ConfirmDialog
          title="Delete project?"
          message={<>This deletes <b className="text-fg">{pendingDelete.name}</b> together with all of its credentials, connectors and proxies. Type <b className="text-fg font-mono text-xs">permanently delete</b> to confirm.</>}
          confirmLabel="Delete project"
          confirmDisabled={deleteConfirmation !== 'permanently delete'}
          onCancel={() => { setPendingDelete(null); setDeleteConfirmation('') }}
          onConfirm={() => deleteMutation.mutate({ id: pendingDelete.id, confirmation: deleteConfirmation })}
          isLoading={deleteMutation.isPending}
        >
          <Input value={deleteConfirmation} onChange={(e) => setDeleteConfirmation(e.target.value)} placeholder="permanently delete" className="font-mono text-sm" autoFocus />
        </ConfirmDialog>
      )}
    </div>
  )
}

/** The projects as one sortable, filterable row each, for installs with too many to scan as tiles. */
function ProjectTable({ projects, activeId, panelOpen, canMutate, onOpen, onEdit, onDelete }: {
  projects: ProjectRow[]
  activeId: string | null
  panelOpen: boolean
  canMutate: boolean
  onOpen: (project: ProjectSummary) => void
  onEdit: (project: ProjectSummary) => void
  onDelete: (project: ProjectSummary) => void
}) {
  const columns: ColumnDef<DataTableFeatures, ProjectRow>[] = useMemo(() => [
    {
      accessorKey: 'name',
      header: 'Project',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => <span className="font-medium truncate block" title={row.original.name}>{row.original.name}</span>,
    },
    {
      accessorKey: 'description',
      header: 'Description',
      meta: { filterVariant: 'text' as const },
      cell: ({ row }) => row.original.description
        ? <span className="text-fg-muted truncate block" title={row.original.description}>{row.original.description}</span>
        : <span className="text-fg-subtle">-</span>,
    },
    {
      id: 'rpm',
      accessorFn: (row: ProjectRow) => row.load?.requests_per_minute,
      header: 'Req/min',
      size: 100,
      sortUndefined: 'last' as const,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => {
        const rpm = getValue<number | undefined>()
        return <span title="Requests per minute over the last 5 minutes">{rpm == null ? '-' : formatRpm(rpm)}</span>
      },
    },
    {
      id: 'trend',
      header: 'Trend',
      size: 100,
      enableSorting: false,
      cell: ({ row }) => row.original.load
        ? <Sparkline values={row.original.load.trend} color={REQUESTS_COLOR} width={72} height={20} fromZero className="inline-block align-middle" />
        : null,
    },
    {
      id: 'requests',
      accessorFn: (row: ProjectRow) => row.load?.request_count,
      header: 'Requests',
      size: 105,
      sortUndefined: 'last' as const,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => {
        const requests = getValue<number | undefined>()
        return requests == null ? '-' : compact(requests)
      },
    },
    {
      id: 'success',
      accessorFn: (row: ProjectRow) => (row.load ? successRate(row.load) ?? undefined : undefined),
      header: 'Success',
      size: 100,
      sortUndefined: 'last' as const,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => {
        const rate = getValue<number | undefined>()
        return rate == null ? '-' : `${rate.toFixed(1)}%`
      },
    },
    {
      id: 'traffic',
      accessorFn: (row: ProjectRow) => (row.load ? trafficBytes(row.load) : undefined),
      header: 'Traffic',
      size: 95,
      sortUndefined: 'last' as const,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => {
        const bytes = getValue<number | undefined>()
        return <span title="Bytes sent and received">{bytes == null ? '-' : bytesShort(bytes)}</span>
      },
    },
    {
      id: 'strategy',
      accessorFn: (row: ProjectSummary) => formatStrategy(row.routing_strategy),
      header: 'Strategy',
      size: 160,
      meta: { filterVariant: 'select' as const },
      cell: ({ getValue }) => <Badge color="gray">{getValue<string>()}</Badge>,
    },
    {
      accessorKey: 'proxy_count',
      header: 'Proxies',
      size: 90,
      meta: { align: 'right' as const },
    },
    {
      accessorKey: 'healthy_proxy_count',
      header: 'Healthy',
      size: 100,
      meta: { align: 'right' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-1.5">
          <span className={`w-2 h-2 rounded-full ${healthDotClass(row.original)}`} />
          {row.original.healthy_proxy_count}
        </span>
      ),
    },
    {
      // One column for both, as the tiles show them, so the load columns fit beside them.
      id: 'held',
      header: () => (
        <span className="inline-flex items-center gap-2" title="Connectors and credentials">
          <Link2 className="w-3.5 h-3.5" /><KeyRound className="w-3.5 h-3.5" />
        </span>
      ),
      size: 100,
      enableSorting: false,
      meta: { align: 'right' as const },
      cell: ({ row }) => (
        <span className="inline-flex items-center gap-2.5 text-fg-muted">
          <span className="inline-flex items-center gap-1" title="Connectors"><Link2 className="w-3.5 h-3.5" /> {row.original.connector_count}</span>
          <span className="inline-flex items-center gap-1" title="Credentials"><KeyRound className="w-3.5 h-3.5" /> {row.original.credential_count}</span>
        </span>
      ),
    },
    ...(canMutate ? [{
      id: 'actions',
      header: '',
      size: 72,
      enableSorting: false,
      cell: ({ row }: { row: { original: ProjectRow } }) => (
        <div className="flex justify-end gap-0.5">
          <button onClick={() => onEdit(row.original)} className="p-1 rounded-sm text-fg-subtle hover:text-fg hover:bg-surface-raised" title="Project settings"><Pencil className="w-3.5 h-3.5" /></button>
          <button onClick={() => onDelete(row.original)} className="p-1 rounded-sm text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Delete project"><Trash2 className="w-3.5 h-3.5" /></button>
        </div>
      ),
    }] as ColumnDef<DataTableFeatures, ProjectRow>[] : []),
  ], [canMutate, onEdit, onDelete])

  return (
    <DataTable
      columns={columns}
      data={projects}
      getRowId={(row) => row.id}
      onRowClick={onOpen}
      activeRowId={activeId}
      columnVisibility={panelOpen ? { description: false, trend: false, held: false } : {}}
      enableColumnFilters
      defaultPageSize={50}
      emptyMessage="No projects match these filters."
    />
  )
}

function ProjectCard({ project, range, active, canMutate, onOpen, onEdit, onDelete }: {
  project: ProjectRow
  range: HistoryRange
  active: boolean
  canMutate: boolean
  onOpen: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  const healthy = project.healthy_proxy_count
  const total = project.proxy_count
  return (
    <Card
      className={`p-4 flex flex-col gap-3 cursor-pointer transition-colors hover:border-line-strong ${active ? 'border-primary ring-[3px] ring-primary-soft' : ''}`}
      onClick={onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' && e.target === e.currentTarget) onOpen() }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-[15px] font-semibold text-fg truncate" title={project.name}>{project.name}</h3>
          <p className="text-xs text-fg-muted mt-0.5 line-clamp-2 h-8 leading-4" title={project.description || undefined}>
            {project.description || <span className="text-fg-subtle italic">No description</span>}
          </p>
        </div>
        {canMutate && (
          <div className="flex items-center gap-0.5 flex-none -mr-1 -mt-1">
            <button onClick={(e) => { e.stopPropagation(); onEdit() }} className="p-1.5 rounded-md text-fg-subtle hover:text-fg hover:bg-surface-raised" title="Project settings"><Pencil className="w-3.5 h-3.5" /></button>
            <button onClick={(e) => { e.stopPropagation(); onDelete() }} className="p-1.5 rounded-md text-fg-subtle hover:text-danger hover:bg-danger-soft" title="Delete project"><Trash2 className="w-3.5 h-3.5" /></button>
          </div>
        )}
      </div>
      <div className="flex items-center gap-2 text-xs text-fg-muted tabular-nums">
        <span className="inline-flex items-center gap-1"><Server className="w-3.5 h-3.5" /> {total} prox{total === 1 ? 'y' : 'ies'}</span>
        <span className="text-line-strong">·</span>
        <span className="inline-flex items-center gap-1.5"><span className={`w-2 h-2 rounded-full ${healthDotClass(project)}`} />{healthy} healthy</span>
        <span className="text-line-strong">·</span>
        <span className="inline-flex items-center gap-1" title="Connectors"><Link2 className="w-3.5 h-3.5" /> {project.connector_count}</span>
        <span className="inline-flex items-center gap-1" title="Credentials"><KeyRound className="w-3.5 h-3.5" /> {project.credential_count}</span>
      </div>
      <LoadSummary load={project.load} range={range} />
      <div className="flex items-center justify-between mt-auto pt-1">
        <Badge color="gray">{formatStrategy(project.routing_strategy)}</Badge>
        <span className="text-xs font-medium text-primary">Open →</span>
      </div>
    </Card>
  )
}

/** A tile's load: the request rate now beside the range's trend, and the range's requests, success rate and bytes. */
function LoadSummary({ load, range }: { load: ProjectLoad | undefined; range: HistoryRange }) {
  const rate = load ? successRate(load) : null
  return (
    <div className="pt-3 border-t border-line flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-3">
        <span className="text-lg font-semibold leading-none tabular-nums" title="Requests per minute over the last 5 minutes">
          {load ? formatRpm(load.requests_per_minute) : '-'} <span className="text-xs font-normal text-fg-muted">req/min</span>
        </span>
        {load && <Sparkline values={load.trend} color={REQUESTS_COLOR} width={96} height={24} fromZero />}
      </div>
      <div className="text-xs text-fg-muted tabular-nums truncate">
        {!load ? '\u00a0' : load.request_count === 0 ? `No requests in ${range}` : (
          <>{compact(load.request_count)} req · {rate == null ? '-' : `${rate.toFixed(1)}%`} success · {bytesShort(trafficBytes(load))} · {range}</>
        )}
      </div>
    </div>
  )
}
