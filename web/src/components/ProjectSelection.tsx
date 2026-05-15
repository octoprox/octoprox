// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Server, Activity, Trash2, FolderOpen, Settings } from 'lucide-react'
import { fetchProjects, createProject, deleteProject, updateProject, ProjectCreate, ProjectUpdate, ProjectSummary } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useTheme } from '../contexts/ThemeContext'
import { useAuth } from '../contexts/AuthContext'
import { setSettingsOrigin } from '../utils/settingsOrigin'
import ProjectModal from './ProjectModal'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import { Button, Input, Modal, ModalFooter, Card } from './ui'

export default function ProjectSelection() {
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const { setSelectedProjectId } = useProject()
  const { isDark } = useTheme()
  const { canMutate } = useAuth()

  const handleOpenSettings = () => {
    setSettingsOrigin(location.pathname)
    navigate('/settings')
  }
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [showEditModal, setShowEditModal] = useState<ProjectSummary | null>(null)
  const [showDeleteModal, setShowDeleteModal] = useState<ProjectSummary | null>(null)
  const [deleteConfirmation, setDeleteConfirmation] = useState('')

  const { data, isLoading, error } = useQuery({
    queryKey: ['projects'],
    queryFn: fetchProjects,
  })

  const createMutation = useMutation({
    mutationFn: createProject,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setShowCreateModal(false)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: ProjectUpdate }) =>
      updateProject(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setShowEditModal(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: ({ id, confirmation }: { id: string; confirmation: string }) =>
      deleteProject(id, confirmation),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setShowDeleteModal(null)
      setDeleteConfirmation('')
    },
  })

  const handleSelectProject = (project: ProjectSummary) => {
    setSelectedProjectId(project.id)
    navigate(`/projects/${project.id}/dashboard`)
  }

  const handleCreateProject = (data: ProjectCreate) => {
    createMutation.mutate(data)
  }

  const handleDeleteProject = () => {
    if (showDeleteModal && deleteConfirmation === 'permanently delete') {
      deleteMutation.mutate({ id: showDeleteModal.id, confirmation: deleteConfirmation })
    }
  }

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bg">
        <div className="text-fg-muted">Loading projects...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bg">
        <div className="text-red-500">Failed to load projects</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-bg p-8">
      <div className="max-w-6xl mx-auto">
        <div className="flex items-center justify-between mb-8">
          <div>
            <img src={isDark ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-10 mb-1" />
            <p className="text-fg-muted mt-1">Select a project to manage</p>
          </div>
          <div className="flex items-center gap-3">
            <Button variant="secondary" onClick={handleOpenSettings}>
              <Settings className="w-5 h-5" />
              Settings
            </Button>
            {canMutate && (
              <Button
                onClick={() => {
                  createMutation.reset()
                  setShowCreateModal(true)
                }}
              >
                <Plus className="w-5 h-5" />
                New Project
              </Button>
            )}
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {data?.projects.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              canMutate={canMutate}
              onSelect={() => handleSelectProject(project)}
              onEdit={() => {
                updateMutation.reset()
                setShowEditModal(project)
              }}
              onDelete={() => setShowDeleteModal(project)}
            />
          ))}
          {data?.projects.length === 0 && (
            <div className="col-span-full text-center py-12 text-fg-muted">
              <FolderOpen className="w-16 h-16 mx-auto mb-4 text-fg-subtle" />
              <p>No projects yet. Create your first project to get started.</p>
            </div>
          )}
        </div>
      </div>

      {showCreateModal && (
        <ProjectModal
          onClose={() => setShowCreateModal(false)}
          onSave={(data) => handleCreateProject(data as ProjectCreate)}
          isLoading={createMutation.isPending}
          error={createMutation.error?.message}
        />
      )}

      {showEditModal && (
        <ProjectModal
          project={showEditModal}
          onClose={() => setShowEditModal(null)}
          onSave={(data) => updateMutation.mutate({ id: showEditModal.id, data })}
          isLoading={updateMutation.isPending}
          error={updateMutation.error?.message}
        />
      )}

      {showDeleteModal && (
        <DeleteProjectModal
          project={showDeleteModal}
          confirmation={deleteConfirmation}
          onConfirmationChange={setDeleteConfirmation}
          onClose={() => {
            setShowDeleteModal(null)
            setDeleteConfirmation('')
          }}
          onDelete={handleDeleteProject}
          isLoading={deleteMutation.isPending}
        />
      )}
    </div>
  )
}

function ProjectCard({
  project,
  canMutate,
  onSelect,
  onEdit,
  onDelete,
}: {
  project: ProjectSummary
  canMutate: boolean
  onSelect: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <Card className="shadow-md p-6 hover:shadow-lg transition-shadow flex flex-col h-full">
      <div className="flex items-start justify-between mb-4">
        <h3 className="text-xl font-semibold text-fg">{project.name}</h3>
        {canMutate && (
          <div className="flex items-center gap-2">
            <button
              onClick={(e) => {
                e.stopPropagation()
                onEdit()
              }}
              className="text-gray-400 hover:text-blue-500 transition-colors"
              title="Edit project"
            >
              <Settings className="w-5 h-5" />
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation()
                onDelete()
              }}
              className="text-gray-400 hover:text-red-500 transition-colors"
              title="Delete project"
            >
              <Trash2 className="w-5 h-5" />
            </button>
          </div>
        )}
      </div>
      <div className="flex-grow">
        <p className="text-fg-muted text-sm mb-4 line-clamp-2 min-h-[2.5rem]">
          {project.description || <span className="text-fg-subtle italic">No description</span>}
        </p>
        <div className="flex items-center gap-4 text-sm text-fg-muted mb-2">
          <div className="flex items-center gap-1">
            <Server className="w-4 h-4" />
            <span>{project.proxy_count} proxies</span>
          </div>
          <div className="flex items-center gap-1">
            <Activity className="w-4 h-4 text-green-500" />
            <span>{project.healthy_proxy_count} healthy</span>
          </div>
        </div>
        <div className="text-xs text-fg-subtle mb-4">
          Strategy: {project.routing_strategy?.replace('_', ' ') || 'round robin'}
        </div>
      </div>
      <Button onClick={onSelect} className="w-full mt-auto">
        Open Project
      </Button>
    </Card>
  )
}

function DeleteProjectModal({
  project,
  confirmation,
  onConfirmationChange,
  onClose,
  onDelete,
  isLoading,
}: {
  project: ProjectSummary
  confirmation: string
  onConfirmationChange: (value: string) => void
  onClose: () => void
  onDelete: () => void
  isLoading: boolean
}) {
  const isValid = confirmation === 'permanently delete'

  return (
    <Modal onClose={onClose} className="p-6">
      <h2 className="text-xl font-semibold text-red-600 mb-4">Delete Project</h2>
      <p className="text-fg-muted mb-4">
        Are you sure you want to delete <strong>{project.name}</strong>? This will also delete
        all credentials, connectors, and proxies associated with this project.
      </p>
      <p className="text-sm text-fg-muted mb-4">
        Type <strong>permanently delete</strong> to confirm:
      </p>
      <Input
        type="text"
        value={confirmation}
        onChange={(e) => onConfirmationChange(e.target.value)}
        className="mb-4"
        placeholder="permanently delete"
      />
      <ModalFooter>
        <Button type="button" variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="danger"
          onClick={onDelete}
          disabled={!isValid || isLoading}
        >
          {isLoading ? 'Deleting...' : 'Delete Project'}
        </Button>
      </ModalFooter>
    </Modal>
  )
}
