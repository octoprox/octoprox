// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchProviders, ProviderListResponse, ProviderOption, ProviderSummary } from '../api/client'
import { providerLogo } from '../utils/credentials'

export interface ProviderCatalog {
  providers: ProviderSummary[]
  byId: Record<string, ProviderSummary>
  presets: Record<string, ProviderOption[]>
  isLoading: boolean
  /** Display name for a credential type, falling back to the raw id. */
  labelFor: (type: string | null | undefined) => string
  /** Logo URL (bundled SVG for shipped types, data URI for custom ones) or null. */
  logoFor: (type: string | null | undefined, isDark: boolean) => string | null
  get: (type: string | null | undefined) => ProviderSummary | undefined
}

const EMPTY: ProviderListResponse = { total: 0, providers: [], presets: {}, countries: [] }

/**
 * The provider catalog served by /providers. Every credential/connector form
 * renders from these schemas, so the list is cached aggressively; admin edits
 * invalidate the ['providers'] query.
 */
export function useProviders(): ProviderCatalog {
  const { data, isLoading } = useQuery({ queryKey: ['providers'], queryFn: fetchProviders, staleTime: 5 * 60 * 1000 })
  const catalog = data ?? EMPTY
  return useMemo(() => {
    const byId: Record<string, ProviderSummary> = {}
    for (const p of catalog.providers) byId[p.id] = p
    const get = (type: string | null | undefined) => (type ? byId[type] : undefined)
    return {
      providers: catalog.providers,
      byId,
      presets: catalog.presets,
      isLoading,
      get,
      labelFor: (type) => get(type)?.name ?? (type || 'Unknown'),
      logoFor: (type, isDark) => providerLogo(get(type), type, isDark),
    }
  }, [catalog, isLoading])
}
