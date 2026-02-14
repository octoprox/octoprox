import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { ProjectUpdate, ProjectSummary } from '../api/client'

interface EditProjectModalProps {
  project: ProjectSummary
  onClose: () => void
  onSave: (data: ProjectUpdate) => void
  isLoading: boolean
  error?: string
}

export default function EditProjectModal({
  project,
  onClose,
  onSave,
  isLoading,
  error,
}: EditProjectModalProps) {
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

