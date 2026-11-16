// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import {
  fetchInstallAttention, fetchProjectAttention, fetchProjectsAttention, muteAttention, unmuteAttention,
  type InstallAttentionLine, type MuteDuration,
} from '../api/client'
import { useToast } from '../contexts/ToastContext'

/** How often the lines are read again: the request lines cover the last 15 minutes. */
const ATTENTION_POLL_MS = 30_000

const MUTED_FOR: Record<MuteDuration, string> = {
  day: 'Muted for 1 day',
  week: 'Muted for 1 week',
  month: 'Muted for 1 month',
  until_clear: 'Muted until it clears',
}

/** A project's Needs attention lines; the Overview card and the sidebar count share the one query. */
export function useProjectAttention(projectId: string | undefined) {
  return useQuery({
    queryKey: ['attention', projectId],
    queryFn: () => fetchProjectAttention(projectId!),
    enabled: !!projectId,
    refetchInterval: ATTENTION_POLL_MS,
  })
}

/** After a change that may fix a line (a connector, a country, the project's settings, a mute), so it clears at once. */
export function invalidateAttention(queryClient: QueryClient, projectId: string | null | undefined): void {
  queryClient.invalidateQueries({ queryKey: ['attention', projectId] })
  queryClient.invalidateQueries({ queryKey: ['projects-attention'] })
}

/** Mute and unmute a project's lines, for editors and admins. */
export function useAttentionMutes(projectId: string) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const onError = (e: Error) => toast.show(e.message || 'Failed to change the mute', 'error')
  const muting = useMutation({
    mutationFn: ({ key, duration }: { key: string; duration: MuteDuration }) => muteAttention(projectId, key, duration),
    onSuccess: (_r, { duration }) => {
      invalidateAttention(queryClient, projectId)
      toast.show(MUTED_FOR[duration])
    },
    onError,
  })
  const unmuting = useMutation({
    mutationFn: (key: string) => unmuteAttention(projectId, key),
    onSuccess: () => {
      invalidateAttention(queryClient, projectId)
      toast.show('Unmuted')
    },
    onError,
  })
  return {
    mute: (key: string, duration: MuteDuration) => muting.mutate({ key, duration }),
    unmute: (key: string) => unmuting.mutate(key),
    pending: muting.isPending || unmuting.isPending,
  }
}

/** Every project's unmuted problem count, for the project list and the switcher. */
export function useProjectsAttention({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: ['projects-attention'],
    queryFn: fetchProjectsAttention,
    enabled,
    refetchInterval: ATTENTION_POLL_MS,
    staleTime: ATTENTION_POLL_MS,
  })
}

/** An install-wide answer with the lines it shows: those the answer before it held too. */
interface DebouncedInstall {
  keys: string[]
  shown: InstallAttentionLine[]
}

/**
 * The install-wide lines for the Projects page. A line shows only once two answers in a row hold it, so a blip
 * (a gateway restarting) never flashes; the first answer shows nothing, and a failed read keeps the last.
 */
export function useInstallAttention(): InstallAttentionLine[] {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['attention-install'],
    queryFn: async (): Promise<DebouncedInstall> => {
      const before = new Set(queryClient.getQueryData<DebouncedInstall>(['attention-install'])?.keys)
      const { lines } = await fetchInstallAttention()
      return { keys: lines.map((line) => line.key), shown: lines.filter((line) => before.has(line.key)) }
    },
    refetchInterval: ATTENTION_POLL_MS,
  })
  return data?.shown ?? []
}
