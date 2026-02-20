// Copyright 2025 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { ProjectUpdate, ProjectSummary } from '../api/client'
import { Button, Input, Select, Textarea, Label, Modal, ModalHeader, ModalFooter, Alert } from './ui'

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
    <Modal onClose={onClose} className="max-h-[90vh] overflow-y-auto p-6">
      <ModalHeader title="Edit Project" onClose={onClose} />
      <form onSubmit={handleSubmit}>
        <div className="space-y-4">
          <div>
            <Label>Name</Label>
            <Input
              type="text"
              required
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
            />
          </div>
          <div>
            <Label>Description</Label>
            <Textarea
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              rows={2}
            />
          </div>
          <div className="border border-gray-200 dark:border-gray-600 rounded-lg p-4 bg-gray-50 dark:bg-gray-700/50">
            <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Proxy Credentials</h3>
            <div className="space-y-3">
              <div>
                <Label className="text-xs text-gray-500 dark:text-gray-400">Username</Label>
                <Input
                  type="text"
                  required
                  value={formData.username}
                  onChange={(e) => setFormData({ ...formData, username: e.target.value })}
                  className="font-mono text-sm"
                />
              </div>
              <div>
                <Label className="text-xs text-gray-500 dark:text-gray-400">Password</Label>
                <div className="relative">
                  <Input
                    type={showPassword ? 'text' : 'password'}
                    required
                    value={formData.password}
                    onChange={(e) => setFormData({ ...formData, password: e.target.value })}
                    className="pr-10 font-mono text-sm"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                  >
                    {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                  </button>
                </div>
              </div>
            </div>
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
            {isLoading ? 'Saving...' : 'Save Changes'}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  )
}
