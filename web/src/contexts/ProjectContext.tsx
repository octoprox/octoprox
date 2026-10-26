// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { createContext, useCallback, useContext, useRef, useState, useEffect, ReactNode } from 'react'
import { ProjectSummary, fetchProject } from '../api/client'

const PROJECT_KEY = 'octoprox_selected_project'

interface ProjectContextType {
  selectedProjectId: string | null
  selectedProject: ProjectSummary | null
  setSelectedProjectId: (id: string | null) => void
  refreshProject: () => Promise<void>
}

const ProjectContext = createContext<ProjectContextType | undefined>(undefined)

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [selectedProjectId, setSelectedProjectIdState] = useState<string | null>(() => {
    return localStorage.getItem(PROJECT_KEY)
  })
  const [selectedProject, setSelectedProject] = useState<ProjectSummary | null>(null)
  // The selection now: a load that answers after a switch belongs to the project left behind and is dropped.
  const currentId = useRef(selectedProjectId)

  // Stable, so effects that list it (AppShell's URL sync) only re-run when the
  // project changes, not on every render of this provider.
  const setSelectedProjectId = useCallback((id: string | null) => {
    currentId.current = id
    setSelectedProjectIdState(id)
    // A switch drops the previous project's details, so no page shows them under the new id while it loads.
    setSelectedProject((current) => (current && current.id === id ? current : null))
    if (id) {
      localStorage.setItem(PROJECT_KEY, id)
    } else {
      localStorage.removeItem(PROJECT_KEY)
    }
  }, [])

  const refreshProject = async () => {
    const id = selectedProjectId
    if (!id) {
      setSelectedProject(null)
      return
    }

    try {
      const project = await fetchProject(id)
      if (currentId.current === id) setSelectedProject(project)
    } catch (error) {
      console.error('Failed to fetch project:', error)
      // If project not found, clear selection
      if (currentId.current === id) setSelectedProjectId(null)
    }
  }

  // Load project when selectedProjectId changes
  useEffect(() => {
    refreshProject()
  }, [selectedProjectId])

  return (
    <ProjectContext
      value={{
        selectedProjectId,
        selectedProject,
        setSelectedProjectId,
        refreshProject,
      }}
    >
      {children}
    </ProjectContext>
  )
}

export function useProject() {
  const context = useContext(ProjectContext)
  if (context === undefined) {
    throw new Error('useProject must be used within a ProjectProvider')
  }
  return context
}

