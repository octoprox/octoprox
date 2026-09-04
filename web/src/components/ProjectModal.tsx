// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { ProjectCreate, ProjectUpdate, ProjectSummary } from '../api/client'
import { Button, Modal, ModalHeader, ModalFooter } from './ui'
import { ProjectForm } from './ProjectForm'

interface ProjectModalProps {
  project?: ProjectSummary
  onClose: () => void
  onSave: (data: ProjectCreate | ProjectUpdate) => void
  isLoading: boolean
  error?: string
}

/**
 * Modal wrapper around ProjectForm. Only used on the project selection page,
 * where there is no shell to dock a panel into.
 */
export default function ProjectModal({ project, onClose, onSave, isLoading, error }: ProjectModalProps) {
  const isEdit = !!project
  return (
    <Modal onClose={onClose} className="max-w-xl max-h-[90vh] overflow-y-auto p-6">
      <ModalHeader title={isEdit ? 'Edit project' : 'Create project'} onClose={onClose} />
      <ProjectForm project={project} onSave={onSave} error={error} formId="project-modal-form" />
      <ModalFooter>
        <Button type="button" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="project-modal-form" size="sm" disabled={isLoading}>
          {isLoading ? (isEdit ? 'Saving…' : 'Creating…') : (isEdit ? 'Save changes' : 'Create project')}
        </Button>
      </ModalFooter>
    </Modal>
  )
}
