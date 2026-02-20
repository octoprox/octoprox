// Copyright 2025 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import { ProjectSummary, fetchProject } from '../api/client'

const PROJECT_KEY = 'octoprox_selected_project'

interface ProjectContextType {
  selectedProjectId: string | null
  selectedProject: ProjectSummary | null
  setSelectedProjectId: (id: string | null) => void
  isLoading: boolean
  refreshProject: () => Promise<void>
}

const ProjectContext = createContext<ProjectContextType | undefined>(undefined)

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [selectedProjectId, setSelectedProjectIdState] = useState<string | null>(() => {
    return localStorage.getItem(PROJECT_KEY)
  })
  const [selectedProject, setSelectedProject] = useState<ProjectSummary | null>(null)
  const [isLoading, setIsLoading] = useState(false)

  const setSelectedProjectId = (id: string | null) => {
    setSelectedProjectIdState(id)
    if (id) {
      localStorage.setItem(PROJECT_KEY, id)
    } else {
      localStorage.removeItem(PROJECT_KEY)
      setSelectedProject(null)
    }
  }

  const refreshProject = async () => {
    if (!selectedProjectId) {
      setSelectedProject(null)
      return
    }

    setIsLoading(true)
    try {
      const project = await fetchProject(selectedProjectId)
      setSelectedProject(project)
    } catch (error) {
      console.error('Failed to fetch project:', error)
      // If project not found, clear selection
      setSelectedProjectId(null)
    } finally {
      setIsLoading(false)
    }
  }

  // Load project when selectedProjectId changes
  useEffect(() => {
    refreshProject()
  }, [selectedProjectId])

  return (
    <ProjectContext.Provider
      value={{
        selectedProjectId,
        selectedProject,
        setSelectedProjectId,
        isLoading,
        refreshProject,
      }}
    >
      {children}
    </ProjectContext.Provider>
  )
}

export function useProject() {
  const context = useContext(ProjectContext)
  if (context === undefined) {
    throw new Error('useProject must be used within a ProjectProvider')
  }
  return context
}

