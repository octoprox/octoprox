import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Server, Activity, Trash2, FolderOpen, Settings, Eye, EyeOff } from 'lucide-react'
import { fetchProjects, createProject, deleteProject, updateProject, ProjectCreate, ProjectUpdate, ProjectSummary } from '../api/client'
import { useProject } from '../contexts/ProjectContext'

export default function ProjectSelection() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { setSelectedProjectId } = useProject()
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
      <div className="min-h-screen flex items-center justify-center bg-gray-100">
        <div className="text-gray-500">Loading projects...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-100">
        <div className="text-red-500">Failed to load projects</div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-100 p-8">
      <div className="max-w-6xl mx-auto">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-3">
              <Activity className="w-10 h-10 text-blue-500" />
              Octoprox
            </h1>
            <p className="text-gray-600 mt-1">Select a project to manage</p>
          </div>
          <button
            onClick={() => {
              createMutation.reset()
              setShowCreateModal(true)
            }}
            className="flex items-center gap-2 bg-blue-500 text-white px-4 py-2 rounded-lg hover:bg-blue-600 transition-colors"
          >
            <Plus className="w-5 h-5" />
            New Project
          </button>
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
            <div className="col-span-full text-center py-12 text-gray-500">
              <FolderOpen className="w-16 h-16 mx-auto mb-4 text-gray-300" />
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
    <div className="bg-white rounded-lg shadow-md p-6 hover:shadow-lg transition-shadow flex flex-col h-full">
      <div className="flex items-start justify-between mb-4">
        <h3 className="text-xl font-semibold text-gray-900">{project.name}</h3>
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
        <p className="text-gray-600 text-sm mb-4 line-clamp-2 min-h-[2.5rem]">
          {project.description || <span className="text-gray-400 italic">No description</span>}
        </p>
        <div className="flex items-center gap-4 text-sm text-gray-500 mb-2">
          <div className="flex items-center gap-1">
            <Server className="w-4 h-4" />
            <span>{project.proxy_count} proxies</span>
          </div>
          <div className="flex items-center gap-1">
            <Activity className="w-4 h-4 text-green-500" />
            <span>{project.healthy_proxy_count} healthy</span>
          </div>
        </div>
        <div className="text-xs text-gray-400 mb-4">
          Strategy: {project.routing_strategy?.replace('_', ' ') || 'round robin'}
        </div>
      </div>
      <button
        onClick={onSelect}
        className="w-full bg-blue-500 text-white py-2 rounded-lg hover:bg-blue-600 transition-colors mt-auto"
      >
        Open Project
      </button>
    </div>
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
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-lg shadow-xl max-w-md w-full p-6">
        <h2 className="text-xl font-semibold mb-4">Create New Project</h2>
        <form onSubmit={handleSubmit}>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Name</label>
              <input
                type="text"
                required
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="w-full border rounded-lg px-3 py-2"
                placeholder="My Project"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Description</label>
              <textarea
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                className="w-full border rounded-lg px-3 py-2"
                rows={2}
                placeholder="Optional description"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Proxy Username
              </label>
              <input
                type="text"
                required
                value={formData.username}
                onChange={(e) => setFormData({ ...formData, username: e.target.value })}
                className="w-full border rounded-lg px-3 py-2"
                placeholder="proxy_user"
              />
              <p className="text-xs text-gray-500 mt-1">Used for proxy authentication</p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Proxy Password
              </label>
              <input
                type="password"
                required
                value={formData.password}
                onChange={(e) => setFormData({ ...formData, password: e.target.value })}
                className="w-full border rounded-lg px-3 py-2"
                placeholder="••••••••"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Routing Strategy
              </label>
              <select
                value={formData.routing_strategy}
                onChange={(e) => setFormData({ ...formData, routing_strategy: e.target.value })}
                className="w-full border rounded-lg px-3 py-2"
              >
                <option value="round_robin">Round Robin</option>
                <option value="least_used">Least Used</option>
                <option value="random">Random</option>
                <option value="sticky">Sticky</option>
                <option value="health_based">Health Based</option>
              </select>
            </div>
          </div>
          {error && <p className="text-red-500 text-sm mt-4">{error}</p>}
          <div className="flex justify-end gap-3 mt-6">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-gray-600 hover:text-gray-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isLoading}
              className="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50"
            >
              {isLoading ? 'Creating...' : 'Create Project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function EditProjectModal({
  project,
  onClose,
  onSave,
  isLoading,
  error,
}: {
  project: ProjectSummary
  onClose: () => void
  onSave: (data: ProjectUpdate) => void
  isLoading: boolean
  error?: string
}) {
  const [formData, setFormData] = useState<ProjectUpdate>({
    name: project.name,
    description: project.description,
    username: project.username,
    password: project.password,
    routing_strategy: project.routing_strategy,
  })
  const [showPassword, setShowPassword] = useState(false)

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    onSave(formData)
  }

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4 z-50">
      <div className="bg-white rounded-lg shadow-xl max-w-md w-full p-6 max-h-[90vh] overflow-y-auto">
        <h2 className="text-xl font-semibold mb-4">Edit Project</h2>
        <form onSubmit={handleSubmit}>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Name</label>
              <input
                type="text"
                required
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="w-full border rounded-lg px-3 py-2"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Description</label>
              <textarea
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                className="w-full border rounded-lg px-3 py-2"
                rows={2}
              />
            </div>
            <div className="border rounded-lg p-4 bg-gray-50">
              <h3 className="text-sm font-medium text-gray-700 mb-3">Proxy Credentials</h3>
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Username</label>
                  <input
                    type="text"
                    required
                    value={formData.username}
                    onChange={(e) => setFormData({ ...formData, username: e.target.value })}
                    className="w-full border rounded-lg px-3 py-2 font-mono text-sm"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Password</label>
                  <div className="relative">
                    <input
                      type={showPassword ? 'text' : 'password'}
                      required
                      value={formData.password}
                      onChange={(e) => setFormData({ ...formData, password: e.target.value })}
                      className="w-full border rounded-lg px-3 py-2 pr-10 font-mono text-sm"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword(!showPassword)}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                    >
                      {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                    </button>
                  </div>
                </div>
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Routing Strategy
              </label>
              <select
                value={formData.routing_strategy}
                onChange={(e) => setFormData({ ...formData, routing_strategy: e.target.value })}
                className="w-full border rounded-lg px-3 py-2"
              >
                <option value="round_robin">Round Robin</option>
                <option value="least_used">Least Used</option>
                <option value="random">Random</option>
                <option value="sticky">Sticky</option>
                <option value="health_based">Health Based</option>
              </select>
            </div>
          </div>
          {error && <p className="text-red-500 text-sm mt-4">{error}</p>}
          <div className="flex justify-end gap-3 mt-6">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-gray-600 hover:text-gray-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isLoading}
              className="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50"
            >
              {isLoading ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
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
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-lg shadow-xl max-w-md w-full p-6">
        <h2 className="text-xl font-semibold text-red-600 mb-4">Delete Project</h2>
        <p className="text-gray-600 mb-4">
          Are you sure you want to delete <strong>{project.name}</strong>? This will also delete
          all sources and proxies associated with this project.
        </p>
        <p className="text-sm text-gray-500 mb-4">
          Type <strong>permanently delete</strong> to confirm:
        </p>
        <input
          type="text"
          value={confirmation}
          onChange={(e) => onConfirmationChange(e.target.value)}
          className="w-full border rounded-lg px-3 py-2 mb-4"
          placeholder="permanently delete"
        />
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-gray-600 hover:text-gray-800"
          >
            Cancel
          </button>
          <button
            onClick={onDelete}
            disabled={!isValid || isLoading}
            className="px-4 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 disabled:opacity-50"
          >
            {isLoading ? 'Deleting...' : 'Delete Project'}
          </button>
        </div>
      </div>
    </div>
  )
}

