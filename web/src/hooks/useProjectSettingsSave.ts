// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { updateAlertSettings, updateProject, type AlertSettingsOverrides, type ProjectSummary, type ProjectUpdate } from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useToast } from '../contexts/ToastContext'
import { invalidateAttention } from './useProjectAttention'

/** A save from the project settings: the project's fields when one changed, the alert overrides when they did. */
export interface ProjectSettingsChange {
  data: ProjectUpdate | null
  alerts: AlertSettingsOverrides | null
}

/**
 * Saves a project's settings for both editors (the panel beside its pages and the Projects page's): only the calls that
 * change something, so thresholds alone compile no routing; then one toast and `onSaved`. Whatever a call saved is
 * read again even when a later one fails, and the failure stays on the mutation for the editor to show.
 */
export function useProjectSettingsSave(project: ProjectSummary | undefined, { onSaved }: { onSaved: () => void }) {
  const queryClient = useQueryClient()
  const { selectedProjectId, refreshProject } = useProject()
  const toast = useToast()
  return useMutation({
    mutationFn: async ({ data, alerts }: ProjectSettingsChange) => {
      const id = project!.id
      const saved = { project: false, alerts: false }
      try {
        if (data) {
          await updateProject(id, data)
          saved.project = true
        }
        if (alerts) {
          await updateAlertSettings(id, alerts)
          saved.alerts = true
        }
      } finally {
        if (saved.project) {
          queryClient.invalidateQueries({ queryKey: ['projects'] })
          queryClient.invalidateQueries({ queryKey: ['metrics', id] })
          if (selectedProjectId === id) await refreshProject()
        }
        if (saved.alerts) queryClient.invalidateQueries({ queryKey: ['alert-settings', id] })
        if (saved.project || saved.alerts) invalidateAttention(queryClient, id)
      }
    },
    onSuccess: () => {
      onSaved()
      toast.show('Project settings saved')
    },
  })
}
