// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Server, Trash2, FolderOpen, Settings, Pencil, LogOut, Link2, Key } from 'lucide-react'
import {
  fetchProjects, createProject, deleteProject, updateProject, logout,
  ProjectCreate, ProjectUpdate, ProjectSummary,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useTheme } from '../contexts/ThemeContext'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../contexts/ToastContext'
import { setSettingsOrigin } from '../utils/settingsOrigin'
import { ProjectForm } from './ProjectForm'
import { Page } from './layout/Page'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import { Button, Input, Card, Badge, Inspector, ConfirmDialog } from './ui'

type PanelState = { kind: 'new' } | { kind: 'edit'; id: string } | null

function formatStrategy(strategy: string | undefined): string {
  return (strategy || 'round_robin').split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
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

  const { data, isLoading, error } = useQuery({ queryKey: ['projects'], queryFn: fetchProjects, refetchInterval: 30000 })

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

  const projects = data?.projects ?? []
  const editing = panel?.kind === 'edit' ? projects.find((p) => p.id === panel.id) : undefined

  let panelNode: React.ReactNode = null
  if (panel?.kind === 'new') {
    panelNode = (
      <Inspector
        title="New project"
        subtitle="A project is one proxy endpoint with its own pool"
        onClose={() => setPanel(null)}
        width={480}
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
        width={480}
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
          actions={canMutate ? <Button size="sm" onClick={() => { createMutation.reset(); setPanel({ kind: 'new' }) }}><Plus className="w-3.5 h-3.5" /> New project</Button> : undefined}
          panel={panelNode}
        >
          {isLoading ? (
            <div className="text-sm text-fg-muted py-10 text-center">Loading projects…</div>
          ) : error ? (
            <div className="text-sm text-danger py-10 text-center">Failed to load projects</div>
          ) : projects.length === 0 ? (
            <Card className="p-10 text-center">
              <FolderOpen className="w-10 h-10 mx-auto mb-3 text-fg-subtle" />
              <h3 className="text-base font-medium">No projects yet</h3>
              <p className="text-sm text-fg-muted mt-1">Create your first project to get a proxy endpoint.</p>
              {canMutate && <Button size="sm" className="mt-4" onClick={() => setPanel({ kind: 'new' })}><Plus className="w-3.5 h-3.5" /> New project</Button>}
            </Card>
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,320px)] auto-rows-fr gap-4">
              {projects.map((project) => (
                <ProjectCard
                  key={project.id}
                  project={project}
                  active={panel?.kind === 'edit' && panel.id === project.id}
                  canMutate={canMutate}
                  onOpen={() => openProject(project)}
                  onEdit={() => { updateMutation.reset(); setPanel({ kind: 'edit', id: project.id }) }}
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

function ProjectCard({ project, active, canMutate, onOpen, onEdit, onDelete }: {
  project: ProjectSummary
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
      onKeyDown={(e) => { if (e.key === 'Enter') onOpen() }}
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
        <span className="inline-flex items-center gap-1.5"><span className={`w-2 h-2 rounded-full ${total === 0 ? 'bg-fg-subtle' : healthy === total ? 'bg-success' : healthy === 0 ? 'bg-danger' : 'bg-warning'}`} />{healthy} healthy</span>
        <span className="text-line-strong">·</span>
        <span className="inline-flex items-center gap-1" title="Connectors"><Link2 className="w-3.5 h-3.5" /> {project.connector_count}</span>
        <span className="inline-flex items-center gap-1" title="Credentials"><Key className="w-3.5 h-3.5" /> {project.credential_count}</span>
      </div>
      <div className="flex items-center justify-between mt-auto pt-1">
        <Badge color="gray">{formatStrategy(project.routing_strategy)}</Badge>
        <span className="text-xs font-medium text-primary">Open →</span>
      </div>
    </Card>
  )
}
