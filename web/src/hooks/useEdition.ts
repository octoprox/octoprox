// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from '@tanstack/react-query'
import { fetchEdition, fetchLicense, type Edition, type LicenseStatus } from '../api/client'
import { useAuth } from '../contexts/AuthContext'

/** The edition changes only with a deploy, which reloads the page. */
const EDITION_STALE_MS = 5 * 60_000

/** Seats in use move as users are added, so the license is read again a minute on. */
const LICENSE_STALE_MS = 60_000

/** Whether the server runs Octoprox Enterprise: only then do its endpoints exist. */
export const isEnterprise = (edition: Edition | undefined): boolean => edition?.name === 'enterprise'

/** The edition the server runs. Public, so the login page asks too. */
export function useEdition() {
  return useQuery({ queryKey: ['edition'], queryFn: fetchEdition, staleTime: EDITION_STALE_MS })
}

/**
 * The Enterprise license, asked for only by admins on the Enterprise edition: the free image has no such endpoint.
 * The cache outlives a sign-out, so whoever shows it checks that the user is an admin.
 */
export function useLicense() {
  const { isAdmin } = useAuth()
  const { data: edition } = useEdition()
  return useQuery<LicenseStatus>({ queryKey: ['license'], queryFn: fetchLicense, enabled: isAdmin && isEnterprise(edition), staleTime: LICENSE_STALE_MS })
}

/** Whether the Activity page can be read: a genuine license with the audit log, expired ones included. */
export function activityReadable(license: LicenseStatus): boolean {
  return (license.state === 'active' || license.state === 'grace' || license.state === 'expired') && license.features.includes('audit')
}
