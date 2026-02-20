import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Server, Activity, Trash2, FolderOpen, Settings, Moon, Sun } from 'lucide-react'
import { fetchProjects, createProject, deleteProject, updateProject, ProjectCreate, ProjectUpdate, ProjectSummary } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useTheme } from '../contexts/ThemeContext'
import EditProjectModal from './EditProjectModal'
import octoproxLogo from '../assets/logos/octoprox_horizontal.svg'
import octoproxLogoDark from '../assets/logos/octoprox_horizontal_dark.svg'
import { Button, Input, Select, Textarea, Label, Modal, ModalHeader, ModalFooter, Card, Alert } from './ui'

export default function ProjectSelection() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { setSelectedProjectId } = useProject()
  const { theme, toggleTheme } = useTheme()
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
      <div className="min-h-screen flex items-center justify-center bg-gray-100 dark:bg-gray-900">
        <div className="text-gray-500 dark:text-gray-400">Loading projects...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-100 dark:bg-gray-900">
        <div className="text-red-500">Failed to load projects</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-gray-900 p-8">
      <div className="max-w-6xl mx-auto">
        <div className="flex items-center justify-between mb-8">
          <div>
            <img src={theme === 'dark' ? octoproxLogoDark : octoproxLogo} alt="Octoprox" className="h-10 mb-1" />
            <p className="text-gray-600 dark:text-gray-400 mt-1">Select a project to manage</p>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={toggleTheme}
              className="p-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-white/60 dark:hover:bg-gray-800/60 rounded-lg transition-colors"
              title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
            >
              {theme === 'dark' ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
            </button>
            <Button
              onClick={() => {
                createMutation.reset()
                setShowCreateModal(true)
              }}
            >
              <Plus className="w-5 h-5" />
              New Project
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {data?.projects.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              onSelect={() => handleSelectProject(project)}
              onEdit={() => {
                updateMutation.reset()
                setShowEditModal(project)
              }}
              onDelete={() => setShowDeleteModal(project)}
            />
          ))}
          {data?.projects.length === 0 && (
            <div className="col-span-full text-center py-12 text-gray-500 dark:text-gray-400">
              <FolderOpen className="w-16 h-16 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
              <p>No projects yet. Create your first project to get started.</p>
            </div>
          )}
        </div>
      </div>

      {showCreateModal && (
        <CreateProjectModal
          onClose={() => setShowCreateModal(false)}
          onCreate={handleCreateProject}
          isLoading={createMutation.isPending}
          error={createMutation.error?.message}
        />
      )}

      {showEditModal && (
        <EditProjectModal
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
  onSelect,
  onEdit,
  onDelete,
}: {
  project: ProjectSummary
  onSelect: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <Card className="shadow-md p-6 hover:shadow-lg transition-shadow flex flex-col h-full">
      <div className="flex items-start justify-between mb-4">
        <h3 className="text-xl font-semibold text-gray-900 dark:text-gray-100">{project.name}</h3>
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
      </div>
      <div className="flex-grow">
        <p className="text-gray-600 dark:text-gray-400 text-sm mb-4 line-clamp-2 min-h-[2.5rem]">
          {project.description || <span className="text-gray-400 dark:text-gray-500 italic">No description</span>}
        </p>
        <div className="flex items-center gap-4 text-sm text-gray-500 dark:text-gray-400 mb-2">
          <div className="flex items-center gap-1">
            <Server className="w-4 h-4" />
            <span>{project.proxy_count} proxies</span>
          </div>
          <div className="flex items-center gap-1">
            <Activity className="w-4 h-4 text-green-500" />
            <span>{project.healthy_proxy_count} healthy</span>
          </div>
        </div>
        <div className="text-xs text-gray-400 dark:text-gray-500 mb-4">
          Strategy: {project.routing_strategy?.replace('_', ' ') || 'round robin'}
        </div>
      </div>
      <Button onClick={onSelect} className="w-full mt-auto">
        Open Project
      </Button>
    </Card>
  )
}

function CreateProjectModal({
  onClose,
  onCreate,
  isLoading,
  error,
}: {
  onClose: () => void
  onCreate: (data: ProjectCreate) => void
  isLoading: boolean
  error?: string
}) {
  const [formData, setFormData] = useState<ProjectCreate>({
    name: '',
    description: '',
    username: '',
    password: '',
    routing_strategy: 'round_robin',
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    onCreate(formData)
  }

  return (
    <Modal onClose={onClose} className="p-6">
      <ModalHeader title="Create New Project" onClose={onClose} />
      <form onSubmit={handleSubmit}>
        <div className="space-y-4">
          <div>
            <Label>Name</Label>
            <Input
              type="text"
              required
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              placeholder="My Project"
            />
          </div>
          <div>
            <Label>Description</Label>
            <Textarea
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              rows={2}
              placeholder="Optional description"
            />
          </div>
          <div>
            <Label>Proxy Username</Label>
            <Input
              type="text"
              required
              value={formData.username}
              onChange={(e) => setFormData({ ...formData, username: e.target.value })}
              placeholder="proxy_user"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Used for proxy authentication</p>
          </div>
          <div>
            <Label>Proxy Password</Label>
            <Input
              type="password"
              required
              value={formData.password}
              onChange={(e) => setFormData({ ...formData, password: e.target.value })}
              placeholder="••••••••"
            />
          </div>
          <div>
            <Label>Routing Strategy</Label>
            <Select
              value={formData.routing_strategy}
              onChange={(e) => setFormData({ ...formData, routing_strategy: e.target.value })}
            >
              <option value="round_robin">Round Robin</option>
              <option value="least_used">Least Used</option>
              <option value="random">Random</option>
              <option value="sticky">Sticky</option>
              <option value="health_based">Health Based</option>
            </Select>
          </div>
        </div>
        {error && <Alert className="mt-4">{error}</Alert>}
        <ModalFooter>
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={isLoading}>
            {isLoading ? 'Creating...' : 'Create Project'}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
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
      <p className="text-gray-600 dark:text-gray-400 mb-4">
        Are you sure you want to delete <strong>{project.name}</strong>? This will also delete
        all credentials, connectors, and proxies associated with this project.
      </p>
      <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
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
